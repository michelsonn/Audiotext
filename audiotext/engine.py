from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import ctypes
import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Sequence

APP_NAME = "Faster Whisper Batch Transcriber"
APP_VERSION = "1.0.5"
APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.json"
USER_CONFIG_PATH = APP_DIR / "config.user.json"

SUPPORTED_AUDIO_EXTENSIONS = {
    ".wav",
    ".wave",
    ".mp3",
    ".flac",
    ".m4a",
    ".mp4",
    ".mkv",
    ".webm",
    ".ogg",
    ".opus",
    ".aac",
    ".wma",
    ".avi",
    ".mov",
    ".m4v",
}

BATCH_RE = re.compile(r"^batch[_ -]?(\d+)$", re.IGNORECASE)
SRT_BLOCK_RE = re.compile(
    r"(?ms)^\s*(\d+)\s*\r?\n"
    r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\r?\n"
    r"(.*?)(?=\r?\n\s*\r?\n|\Z)"
)

_DLL_HANDLES: list[Any] = []
_PRINT_LOCK = threading.Lock()


@dataclass(frozen=True)
class SegmentData:
    index: int
    start: float
    end: float
    text: str
    avg_logprob: Optional[float] = None
    no_speech_prob: Optional[float] = None
    compression_ratio: Optional[float] = None
    temperature: Optional[float] = None


@dataclass
class RunOptions:
    source_root: Path
    output_mode: str
    output_root: Optional[Path]
    workers: int
    channel: str
    run_mode: str
    next_n: Optional[int] = None
    batch_start: Optional[int] = None
    batch_end: Optional[int] = None
    time_limit_minutes: Optional[int] = None
    folder_count: Optional[int] = None
    vad_enabled: bool = True
    force_reprocess: bool = False

    @property
    def project_key(self) -> str:
        payload = "|".join(
            [
                str(self.source_root.resolve()).casefold(),
                self.output_mode,
                str(self.output_root.resolve()).casefold() if self.output_root else "",
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


@dataclass
class Task:
    source: Path
    output_base: Path
    txt_path: Path
    srt_path: Path
    json_path: Path
    needs_txt: bool
    needs_srt: bool
    needs_json: bool
    action: str
    relative_path: str
    batch_number: Optional[int]
    channel: Optional[str]


@dataclass
class TaskResult:
    source: Path
    output_base: Path
    status: str
    action: str
    elapsed_seconds: float
    message: str = ""
    segments_count: int = 0
    audio_duration: Optional[float] = None
    duration_after_vad: Optional[float] = None


@dataclass
class RunStats:
    total_selected: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    regenerated: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at


class StateDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                project_key TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                options_json TEXT NOT NULL,
                status TEXT NOT NULL,
                total_selected INTEGER DEFAULT 0,
                completed INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                skipped INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS files (
                project_key TEXT NOT NULL,
                source_path TEXT NOT NULL,
                source_size INTEGER,
                source_mtime_ns INTEGER,
                output_base TEXT,
                status TEXT NOT NULL,
                action TEXT,
                attempts INTEGER DEFAULT 0,
                last_error TEXT,
                last_run_id TEXT,
                elapsed_seconds REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (project_key, source_path)
            );

            CREATE INDEX IF NOT EXISTS idx_files_project_status
            ON files(project_key, status);
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def start_run(self, run_id: str, options: RunOptions) -> None:
        payload = asdict(options)
        payload["source_root"] = str(options.source_root)
        payload["output_root"] = str(options.output_root) if options.output_root else None
        self.conn.execute(
            """
            INSERT INTO runs(run_id, project_key, started_at, options_json, status)
            VALUES(?, ?, ?, ?, 'running')
            """,
            (
                run_id,
                options.project_key,
                utc_now(),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def finish_run(self, run_id: str, stats: RunStats, status: str) -> None:
        self.conn.execute(
            """
            UPDATE runs
            SET finished_at=?, status=?, total_selected=?, completed=?, failed=?, skipped=?
            WHERE run_id=?
            """,
            (
                utc_now(),
                status,
                stats.total_selected,
                stats.completed,
                stats.failed,
                stats.skipped,
                run_id,
            ),
        )
        self.conn.commit()

    def mark_started(self, run_id: str, options: RunOptions, task: Task) -> None:
        st = task.source.stat()
        self.conn.execute(
            """
            INSERT INTO files(
                project_key, source_path, source_size, source_mtime_ns,
                output_base, status, action, attempts, last_error,
                last_run_id, elapsed_seconds, updated_at
            ) VALUES(?, ?, ?, ?, ?, 'running', ?, 1, NULL, ?, NULL, ?)
            ON CONFLICT(project_key, source_path) DO UPDATE SET
                source_size=excluded.source_size,
                source_mtime_ns=excluded.source_mtime_ns,
                output_base=excluded.output_base,
                status='running',
                action=excluded.action,
                attempts=files.attempts + 1,
                last_error=NULL,
                last_run_id=excluded.last_run_id,
                elapsed_seconds=NULL,
                updated_at=excluded.updated_at
            """,
            (
                options.project_key,
                str(task.source.resolve()),
                st.st_size,
                st.st_mtime_ns,
                str(task.output_base.resolve()),
                task.action,
                run_id,
                utc_now(),
            ),
        )
        self.conn.commit()

    def mark_result(
        self,
        run_id: str,
        options: RunOptions,
        result: TaskResult,
    ) -> None:
        error = result.message if result.status == "error" else None
        self.conn.execute(
            """
            UPDATE files
            SET status=?, action=?, last_error=?, last_run_id=?, elapsed_seconds=?, updated_at=?
            WHERE project_key=? AND source_path=?
            """,
            (
                result.status,
                result.action,
                error,
                run_id,
                result.elapsed_seconds,
                utc_now(),
                options.project_key,
                str(result.source.resolve()),
            ),
        )
        self.conn.commit()

    def error_paths(self, project_key: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT source_path FROM files WHERE project_key=? AND status='error'",
            (project_key,),
        ).fetchall()
        return {row[0] for row in rows}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_config() -> dict[str, Any]:
    if not DEFAULT_CONFIG_PATH.exists():
        raise FileNotFoundError(f"Не найден файл конфигурации: {DEFAULT_CONFIG_PATH}")
    config = load_json(DEFAULT_CONFIG_PATH)
    if USER_CONFIG_PATH.exists():
        config = deep_merge(config, load_json(USER_CONFIG_PATH))
    return config


def setup_logging(state_dir: Path, run_id: str, verbose: bool = False) -> Path:
    logs_dir = state_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"run_{run_id}.log"
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    return log_path


def safe_print(message: str = "") -> None:
    with _PRINT_LOCK:
        print(message, flush=True)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def format_srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_srt_timestamp(value: str) -> float:
    hh, mm, tail = value.split(":")
    ss, ms = tail.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def segments_to_txt(segments: Sequence[SegmentData]) -> str:
    paragraphs = [normalize_text(segment.text) for segment in segments if normalize_text(segment.text)]
    return "\r\n\r\n".join(paragraphs)


def segments_to_srt(segments: Sequence[SegmentData]) -> str:
    blocks: list[str] = []
    counter = 1
    for segment in segments:
        text = normalize_text(segment.text)
        if not text:
            continue
        blocks.append(
            "\r\n".join(
                [
                    str(counter),
                    f"{format_srt_timestamp(segment.start)} --> {format_srt_timestamp(segment.end)}",
                    text,
                ]
            )
        )
        counter += 1
    return "\r\n\r\n".join(blocks) + ("\r\n" if blocks else "")


def parse_srt(path: Path) -> list[SegmentData]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    segments: list[SegmentData] = []
    for match in SRT_BLOCK_RE.finditer(text):
        body = normalize_text(match.group(4))
        if not body:
            continue
        segments.append(
            SegmentData(
                index=len(segments) + 1,
                start=parse_srt_timestamp(match.group(2)),
                end=parse_srt_timestamp(match.group(3)),
                text=body,
            )
        )
    return segments


def segments_from_json(path: Path) -> tuple[list[SegmentData], dict[str, Any]]:
    payload = load_json(path)
    raw_segments = payload.get("segments", [])
    segments = [
        SegmentData(
            index=int(item.get("index", idx + 1)),
            start=float(item["start"]),
            end=float(item["end"]),
            text=str(item.get("text", "")),
            avg_logprob=item.get("avg_logprob"),
            no_speech_prob=item.get("no_speech_prob"),
            compression_ratio=item.get("compression_ratio"),
            temperature=item.get("temperature"),
        )
        for idx, item in enumerate(raw_segments)
    ]
    return segments, payload


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temp_path.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp_path.unlink()


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, text)


def is_valid_json(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        payload = load_json(path)
        return isinstance(payload.get("segments"), list)
    except (OSError, ValueError, TypeError):
        return False


def is_valid_txt(path: Path, allow_empty: bool = False) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if path.stat().st_size == 0:
        return allow_empty
    try:
        return bool(path.read_text(encoding="utf-8-sig", errors="strict").strip())
    except (OSError, UnicodeError):
        return False


def is_valid_srt(path: Path, allow_empty: bool = False) -> bool:
    if not path.exists() or not path.is_file():
        return False
    if path.stat().st_size == 0:
        return allow_empty
    try:
        return bool(parse_srt(path))
    except (OSError, ValueError):
        return False


def batch_and_channel(relative_path: Path) -> tuple[Optional[int], Optional[str]]:
    batch_number: Optional[int] = None
    channel: Optional[str] = None
    for part in relative_path.parts[:-1]:
        match = BATCH_RE.match(part)
        if match:
            batch_number = int(match.group(1))
        folded = part.casefold()
        if folded in {"client", "manager"}:
            channel = folded
    return batch_number, channel


def detect_named_audio_folders(
    source_root: Path, config: dict[str, Any]
) -> set[str]:
    """Return case-folded folder names that actually contain supported audio files."""
    extensions = {
        str(ext).casefold() if str(ext).startswith(".") else f".{str(ext).casefold()}"
        for ext in config.get("processing", {}).get(
            "audio_extensions", sorted(SUPPORTED_AUDIO_EXTENSIONS)
        )
    }
    names: set[str] = set()
    for path in source_root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in extensions:
            continue
        for parent in path.relative_to(source_root).parents:
            if parent == Path("."):
                continue
            names.add(parent.name.casefold())
    return names




def detect_top_level_audio_groups(
    source_root: Path, config: dict[str, Any]
) -> list[Path]:
    """Return immediate child folders that contain supported audio recursively."""
    extensions = {
        str(ext).casefold() if str(ext).startswith(".") else f".{str(ext).casefold()}"
        for ext in config.get("processing", {}).get(
            "audio_extensions", sorted(SUPPORTED_AUDIO_EXTENSIONS)
        )
    }
    groups: list[Path] = []
    try:
        children = [path for path in source_root.iterdir() if path.is_dir()]
    except OSError:
        return groups
    for child in children:
        contains_audio = any(
            path.is_file() and path.suffix.casefold() in extensions
            for path in child.rglob("*")
        )
        if contains_audio:
            groups.append(child)
    return sorted(groups, key=natural_sort_key)

def natural_sort_key(path: Path) -> list[Any]:
    parts = re.split(r"(\d+)", str(path).casefold())
    return [int(part) if part.isdigit() else part for part in parts]


def make_output_base(source: Path, options: RunOptions) -> Path:
    if options.output_mode == "same":
        return source.with_suffix("")
    if not options.output_root:
        raise ValueError("Для зеркального режима не указана выходная папка")
    relative = source.relative_to(options.source_root)
    return (options.output_root / relative).with_suffix("")


def build_task(source: Path, options: RunOptions, force: bool = False) -> Optional[Task]:
    output_base = make_output_base(source, options)
    txt_path = Path(str(output_base) + ".txt")
    srt_path = Path(str(output_base) + ".srt")
    json_path = Path(str(output_base) + ".segments.json")

    json_ok = is_valid_json(json_path)
    allow_empty = False
    if json_ok:
        try:
            segments, _ = segments_from_json(json_path)
            allow_empty = len(segments) == 0
        except Exception:
            json_ok = False

    txt_ok = is_valid_txt(txt_path, allow_empty=allow_empty)
    srt_ok = is_valid_srt(srt_path, allow_empty=allow_empty)

    if force:
        txt_ok = srt_ok = json_ok = False

    if txt_ok and srt_ok and json_ok:
        return None

    if json_ok:
        action = "regenerate_from_json"
    elif srt_ok:
        action = "regenerate_from_srt"
    else:
        action = "transcribe"

    relative = source.relative_to(options.source_root)
    batch_number, channel = batch_and_channel(relative)
    return Task(
        source=source,
        output_base=output_base,
        txt_path=txt_path,
        srt_path=srt_path,
        json_path=json_path,
        needs_txt=not txt_ok,
        needs_srt=not srt_ok,
        needs_json=not json_ok,
        action=action,
        relative_path=str(relative),
        batch_number=batch_number,
        channel=channel,
    )


def scan_sources(options: RunOptions, config: dict[str, Any]) -> list[Path]:
    extensions = {
        str(ext).casefold() if str(ext).startswith(".") else f".{str(ext).casefold()}"
        for ext in config.get("processing", {}).get(
            "audio_extensions", sorted(SUPPORTED_AUDIO_EXTENSIONS)
        )
    }
    output_root_resolved = options.output_root.resolve() if options.output_root else None
    sources: list[Path] = []
    for path in options.source_root.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in extensions:
            continue
        if output_root_resolved:
            with contextlib.suppress(ValueError):
                path.resolve().relative_to(output_root_resolved)
                continue
        relative = path.relative_to(options.source_root)
        batch_number, channel = batch_and_channel(relative)
        if options.channel == "both":
            if channel not in {"client", "manager"}:
                continue
        elif options.channel != "all":
            if options.channel.startswith("folder:"):
                wanted = options.channel.split(":", 1)[1].casefold()
                parent_names = {part.casefold() for part in relative.parts[:-1]}
                if wanted not in parent_names:
                    continue
            elif channel != options.channel:
                continue
        if options.run_mode == "batch_range":
            if batch_number is None:
                continue
            if options.batch_start is not None and batch_number < options.batch_start:
                continue
            if options.batch_end is not None and batch_number > options.batch_end:
                continue
        sources.append(path)
    return sorted(sources, key=natural_sort_key)


def find_collisions(tasks: Sequence[Task]) -> dict[str, list[Task]]:
    by_base: dict[str, list[Task]] = {}
    for task in tasks:
        key = str(task.output_base.resolve()).casefold()
        by_base.setdefault(key, []).append(task)
    return {key: group for key, group in by_base.items() if len(group) > 1}


def determine_state_dir(options: RunOptions) -> Path:
    root = options.output_root if options.output_mode == "mirror" else options.source_root
    assert root is not None
    return root / "__transcriber_state__"


def build_json_payload(
    task: Task,
    segments: Sequence[SegmentData],
    config: dict[str, Any],
    source_kind: str,
    audio_duration: Optional[float] = None,
    duration_after_vad: Optional[float] = None,
    model_path: Optional[str] = None,
) -> dict[str, Any]:
    st = task.source.stat()
    transcription_cfg = config.get("transcription", {})
    return {
        "schema_version": 1,
        "created_at": utc_now(),
        "generator": {"name": APP_NAME, "version": APP_VERSION},
        "source_kind": source_kind,
        "source": {
            "path": str(task.source.resolve()),
            "relative_path": task.relative_path,
            "size_bytes": st.st_size,
            "mtime_ns": st.st_mtime_ns,
        },
        "model": {
            "name": config.get("model", {}).get("name", "large-v3-turbo"),
            "resolved_path": model_path,
            "device": config.get("model", {}).get("device", "cuda"),
            "compute_type": config.get("model", {}).get("compute_type", "float16"),
        },
        "transcription": {
            "language": transcription_cfg.get("language", "ru"),
            "task": transcription_cfg.get("task", "transcribe"),
            "beam_size": transcription_cfg.get("beam_size", 5),
            "condition_on_previous_text": transcription_cfg.get(
                "condition_on_previous_text", True
            ),
            "vad_enabled": transcription_cfg.get("vad", {}).get("enabled", True),
            "vad_parameters": transcription_cfg.get("vad", {}).get("parameters", {}),
        },
        "audio": {
            "duration_seconds": audio_duration,
            "duration_after_vad_seconds": duration_after_vad,
        },
        "segments": [asdict(segment) for segment in segments],
    }


def write_requested_outputs(
    task: Task,
    segments: Sequence[SegmentData],
    payload: dict[str, Any],
) -> None:
    if task.needs_txt:
        atomic_write_text(task.txt_path, segments_to_txt(segments))
    if task.needs_srt:
        atomic_write_text(task.srt_path, segments_to_srt(segments))
    if task.needs_json:
        write_json(task.json_path, payload)


def common_buzz_model_roots() -> list[Path]:
    roots: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    appdata = os.environ.get("APPDATA")
    if local:
        roots.extend(
            [
                Path(local) / "Buzz" / "Buzz" / "Cache" / "models",
                Path(local) / "Buzz" / "Cache" / "models",
                Path(local) / "Programs" / "Buzz",
            ]
        )
    if appdata:
        roots.append(Path(appdata) / "Buzz")
    return roots


def is_model_directory(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "model.bin").is_file()
        and (path / "config.json").is_file()
    )


def scan_for_model(root: Path, model_hint: str) -> list[Path]:
    if not root.exists():
        return []
    candidates: list[Path] = []
    hint = model_hint.casefold().replace("_", "-")
    try:
        for model_file in root.rglob("model.bin"):
            parent = model_file.parent
            normalized = str(parent).casefold().replace("_", "-")
            if hint in normalized and is_model_directory(parent):
                candidates.append(parent)
    except (OSError, PermissionError):
        pass
    return candidates


def resolve_model(config: dict[str, Any]) -> tuple[str, str]:
    model_cfg = config.get("model", {})
    name = str(model_cfg.get("name", "large-v3-turbo"))
    custom_path = str(model_cfg.get("custom_path", "")).strip()
    source = str(model_cfg.get("source", "auto")).casefold()

    if custom_path:
        path = Path(os.path.expandvars(os.path.expanduser(custom_path))).resolve()
        if not is_model_directory(path):
            raise FileNotFoundError(
                f"Папка custom_path не похожа на модель CTranslate2: {path}"
            )
        return str(path), "custom"

    candidates: list[Path] = []
    if source in {"auto", "buzz"}:
        for root in common_buzz_model_roots():
            candidates.extend(scan_for_model(root, name))

    if source in {"auto", "local"}:
        local_root = APP_DIR / str(model_cfg.get("download_root", "models"))
        candidates.extend(scan_for_model(local_root, name))

    if candidates:
        candidates = sorted(
            {candidate.resolve() for candidate in candidates},
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return str(candidates[0]), "local-cache"

    if source == "buzz":
        raise FileNotFoundError(
            "В кэше Buzz не найдена совместимая модель large-v3-turbo. "
            "Поменяйте model.source на auto/download или задайте custom_path."
        )

    return name, "download"


def candidate_cuda_dirs(config: dict[str, Any]) -> list[Path]:
    dirs: list[Path] = []
    for raw in config.get("runtime", {}).get("cuda_dll_dirs", []):
        expanded = os.path.expandvars(os.path.expanduser(str(raw)))
        dirs.append(Path(expanded))

    dirs.append(APP_DIR / "runtime" / "cuda")

    cuda_path = os.environ.get("CUDA_PATH")
    if cuda_path:
        dirs.append(Path(cuda_path) / "bin")

    local = os.environ.get("LOCALAPPDATA")
    program_files = os.environ.get("PROGRAMFILES")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)")
    roots: list[Path] = []
    if local:
        roots.extend([Path(local) / "Programs" / "Buzz", Path(local) / "Buzz"])
    if program_files:
        roots.append(Path(program_files) / "Buzz")
    if program_files_x86:
        roots.append(Path(program_files_x86) / "Buzz")

    dll_patterns = ("cublas64_12.dll", "cudnn64_9.dll", "cublasLt64_12.dll")
    for root in roots:
        if not root.exists():
            continue
        found_dirs: set[Path] = set()
        for pattern in dll_patterns:
            try:
                for dll in root.rglob(pattern):
                    found_dirs.add(dll.parent)
            except (OSError, PermissionError):
                continue
        dirs.extend(sorted(found_dirs))

    path_dirs = [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    dirs.extend(path_dirs)

    unique: list[Path] = []
    seen: set[str] = set()
    for directory in dirs:
        try:
            resolved = directory.resolve()
        except OSError:
            resolved = directory
        key = str(resolved).casefold()
        if key not in seen and directory.exists():
            seen.add(key)
            unique.append(directory)
    return unique


def configure_cuda_dll_search(config: dict[str, Any]) -> list[Path]:
    if os.name != "nt":
        return []
    configured: list[Path] = []
    for directory in candidate_cuda_dirs(config):
        try:
            handle = os.add_dll_directory(str(directory))
            _DLL_HANDLES.append(handle)
            configured.append(directory)
        except (OSError, AttributeError):
            continue
    if configured:
        current = os.environ.get("PATH", "")
        prefix = os.pathsep.join(str(path) for path in configured)
        os.environ["PATH"] = prefix + os.pathsep + current
    return configured


class ModelRuntime:
    def __init__(self, config: dict[str, Any], workers: int, vad_enabled: bool):
        self.config = config
        self.workers = workers
        self.vad_enabled = vad_enabled
        self.model: Any = None
        self.model_reference: Optional[str] = None
        self.model_origin: Optional[str] = None
        self.load_lock = threading.Lock()

    def load(self) -> None:
        if self.model is not None:
            return
        with self.load_lock:
            if self.model is not None:
                return
            configured_dirs = configure_cuda_dll_search(self.config)
            logging.info("CUDA DLL search paths added: %d", len(configured_dirs))
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError(
                    "Не установлен faster-whisper. Сначала запустите INSTALL.bat."
                ) from exc

            model_ref, origin = resolve_model(self.config)
            self.model_reference = model_ref
            self.model_origin = origin
            model_cfg = self.config.get("model", {})
            download_root = APP_DIR / str(model_cfg.get("download_root", "models"))
            download_root.mkdir(parents=True, exist_ok=True)
            safe_print(f"\nЗагрузка модели: {model_ref}")
            safe_print(f"Источник модели: {origin}")
            if origin == "download":
                safe_print(
                    "Модель не найдена локально. При первом запуске она будет скачана "
                    "в папку models."
                )
            self.model = WhisperModel(
                model_ref,
                device=str(model_cfg.get("device", "cuda")),
                device_index=int(model_cfg.get("device_index", 0)),
                compute_type=str(model_cfg.get("compute_type", "float16")),
                num_workers=self.workers,
                download_root=str(download_root),
                local_files_only=False,
            )
            safe_print("Модель загружена.\n")

    def transcribe(self, task: Task) -> TaskResult:
        started = time.monotonic()
        try:
            self.load()
            transcription_cfg = self.config.get("transcription", {})
            vad_cfg = transcription_cfg.get("vad", {})
            hotwords = load_hotwords(self.config)
            segments_gen, info = self.model.transcribe(
                str(task.source),
                language=str(transcription_cfg.get("language", "ru")),
                task=str(transcription_cfg.get("task", "transcribe")),
                beam_size=int(transcription_cfg.get("beam_size", 5)),
                best_of=int(transcription_cfg.get("best_of", 5)),
                temperature=transcription_cfg.get(
                    "temperature", [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
                ),
                condition_on_previous_text=bool(
                    transcription_cfg.get("condition_on_previous_text", True)
                ),
                word_timestamps=False,
                vad_filter=self.vad_enabled,
                vad_parameters=(
                    dict(vad_cfg.get("parameters", {}))
                    if self.vad_enabled
                    else None
                ),
                hotwords=hotwords or None,
                log_progress=False,
            )
            raw_segments = list(segments_gen)
            segments: list[SegmentData] = []
            for item in raw_segments:
                text = normalize_text(item.text)
                if not text:
                    continue
                segments.append(
                    SegmentData(
                        index=len(segments) + 1,
                        start=float(item.start),
                        end=float(item.end),
                        text=text,
                        avg_logprob=getattr(item, "avg_logprob", None),
                        no_speech_prob=getattr(item, "no_speech_prob", None),
                        compression_ratio=getattr(item, "compression_ratio", None),
                        temperature=getattr(item, "temperature", None),
                    )
                )
            audio_duration = getattr(info, "duration", None)
            duration_after_vad = getattr(info, "duration_after_vad", None)
            payload = build_json_payload(
                task,
                segments,
                self.config,
                source_kind="transcription",
                audio_duration=audio_duration,
                duration_after_vad=duration_after_vad,
                model_path=self.model_reference,
            )
            write_requested_outputs(task, segments, payload)
            return TaskResult(
                source=task.source,
                output_base=task.output_base,
                status="success",
                action="transcribe",
                elapsed_seconds=time.monotonic() - started,
                message="",
                segments_count=len(segments),
                audio_duration=audio_duration,
                duration_after_vad=duration_after_vad,
            )
        except Exception as exc:
            logging.exception("Ошибка транскрибации %s", task.source)
            return TaskResult(
                source=task.source,
                output_base=task.output_base,
                status="error",
                action="transcribe",
                elapsed_seconds=time.monotonic() - started,
                message=f"{type(exc).__name__}: {exc}",
            )


def load_hotwords(config: dict[str, Any]) -> str:
    raw = str(config.get("transcription", {}).get("hotwords_file", "")).strip()
    if not raw:
        return ""
    path = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not path.is_absolute():
        path = APP_DIR / path
    if not path.exists():
        logging.warning("Файл hotwords не найден: %s", path)
        return ""
    words = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()]
    return ", ".join(word for word in words if word and not word.startswith("#"))


def process_task(task: Task, runtime: ModelRuntime, config: dict[str, Any]) -> TaskResult:
    started = time.monotonic()
    try:
        if task.action == "regenerate_from_json":
            segments, payload = segments_from_json(task.json_path)
            write_requested_outputs(task, segments, payload)
            return TaskResult(
                source=task.source,
                output_base=task.output_base,
                status="success",
                action="regenerate_from_json",
                elapsed_seconds=time.monotonic() - started,
                segments_count=len(segments),
                message="Недостающие файлы восстановлены из JSON",
            )
        if task.action == "regenerate_from_srt":
            segments = parse_srt(task.srt_path)
            if not segments and task.srt_path.stat().st_size > 0:
                raise ValueError("Не удалось разобрать существующий SRT")
            payload = build_json_payload(
                task,
                segments,
                config,
                source_kind="reconstructed_from_srt",
            )
            write_requested_outputs(task, segments, payload)
            return TaskResult(
                source=task.source,
                output_base=task.output_base,
                status="success",
                action="regenerate_from_srt",
                elapsed_seconds=time.monotonic() - started,
                segments_count=len(segments),
                message="TXT/JSON восстановлены из существующего SRT",
            )
        return runtime.transcribe(task)
    except Exception as exc:
        logging.exception("Ошибка обработки %s", task.source)
        return TaskResult(
            source=task.source,
            output_base=task.output_base,
            status="error",
            action=task.action,
            elapsed_seconds=time.monotonic() - started,
            message=f"{type(exc).__name__}: {exc}",
        )


def choose_directory(title: str, initial: Optional[Path] = None) -> Optional[Path]:
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            title=title,
            initialdir=str(initial) if initial and initial.exists() else None,
            mustexist=True,
        )
        root.destroy()
        return Path(selected) if selected else None
    except Exception:
        return None


def prompt_path(label: str, must_exist: bool = True, initial: Optional[Path] = None) -> Path:
    while True:
        safe_print(f"\n{label}")
        safe_print("Нажмите Enter, чтобы выбрать папку в окне, или вставьте путь вручную.")
        raw = input("> ").strip().strip('"')
        if raw:
            path = Path(os.path.expandvars(os.path.expanduser(raw)))
        else:
            selected = choose_directory(label, initial)
            if not selected:
                safe_print("Папка не выбрана.")
                continue
            path = selected
        if must_exist and not path.exists():
            safe_print(f"Папка не существует: {path}")
            continue
        if not must_exist:
            path.mkdir(parents=True, exist_ok=True)
        return path.resolve()


def prompt_choice(label: str, choices: Sequence[tuple[str, str]], default: str) -> str:
    safe_print(f"\n{label}")
    for key, description in choices:
        marker = " [по умолчанию]" if key == default else ""
        safe_print(f"  {key} — {description}{marker}")
    valid = {key for key, _ in choices}
    while True:
        raw = input("> ").strip() or default
        if raw in valid:
            return raw
        safe_print("Введите один из предложенных вариантов.")


def prompt_int(
    label: str,
    default: int,
    minimum: int = 1,
    maximum: Optional[int] = None,
) -> int:
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        if not raw:
            return default
        try:
            value = int(raw)
            if value < minimum or (maximum is not None and value > maximum):
                raise ValueError
            return value
        except ValueError:
            suffix = f"–{maximum}" if maximum is not None else " и больше"
            safe_print(f"Введите целое число {minimum}{suffix}.")


def interactive_wizard(config: dict[str, Any]) -> RunOptions:
    safe_print("=" * 72)
    safe_print(f"{APP_NAME} v{APP_VERSION}")
    safe_print("=" * 72)
    safe_print(
        "Программа рекурсивно найдёт аудиофайлы, создаст TXT, SRT и служебный "
        "JSON с сегментами."
    )

    source_root = prompt_path("Выберите корневую папку с аудиофайлами")
    output_choice = prompt_choice(
        "Куда сохранять результаты?",
        [("1", "рядом с аудиофайлами"), ("2", "в зеркальную структуру")],
        "2",
    )
    output_mode = "same" if output_choice == "1" else "mirror"
    output_root: Optional[Path] = None
    if output_mode == "mirror":
        output_root = prompt_path(
            "Выберите или создайте корневую папку для транскрипций",
            must_exist=False,
            initial=source_root.parent,
        )
        if output_root == source_root:
            raise ValueError("Исходная и выходная папки не должны совпадать в зеркальном режиме")

    detected_folders = detect_named_audio_folders(source_root, config)
    has_client = "client" in detected_folders
    has_manager = "manager" in detected_folders

    if has_client or has_manager:
        choices: list[tuple[str, str]] = [("1", "все аудиофайлы независимо от структуры папок")]
        mapping: dict[str, str] = {"1": "all"}
        next_key = 2
        if has_client and has_manager:
            choices.append((str(next_key), "client и manager"))
            mapping[str(next_key)] = "both"
            next_key += 1
        if has_client:
            choices.append((str(next_key), "только client"))
            mapping[str(next_key)] = "client"
            next_key += 1
        if has_manager:
            choices.append((str(next_key), "только manager"))
            mapping[str(next_key)] = "manager"
            next_key += 1
        choices.append((str(next_key), "указать другое имя папки вручную"))
        custom_key = str(next_key)

        channel_choice = prompt_choice("Что обрабатывать?", choices, "1")
        if channel_choice == custom_key:
            while True:
                custom_folder = input("Имя папки для фильтрации: ").strip()
                if custom_folder:
                    channel = f"folder:{custom_folder}"
                    break
                safe_print("Имя папки не должно быть пустым.")
        else:
            channel = mapping[channel_choice]
    else:
        safe_print(
            "Папки client/manager не найдены. Будут обработаны все аудиофайлы "
            "во всех вложенных папках."
        )
        channel = "all"

    top_level_groups = detect_top_level_audio_groups(source_root, config)
    batch_groups = [group for group in top_level_groups if BATCH_RE.match(group.name)]

    run_choices: list[tuple[str, str]] = [
        ("1", "обработать всё оставшееся"),
        ("2", "обработать следующие N файлов"),
    ]
    run_mapping: dict[str, str] = {"1": "all", "2": "next_n"}
    next_key = 3
    if batch_groups:
        run_choices.append((str(next_key), "обработать диапазон batch_XXX"))
        run_mapping[str(next_key)] = "batch_range"
        next_key += 1
    elif len(top_level_groups) >= 2:
        run_choices.append((str(next_key), "обработать следующие N папок"))
        run_mapping[str(next_key)] = "folder_next_n"
        next_key += 1
    run_choices.append((str(next_key), "повторить только прошлые ошибки"))
    run_mapping[str(next_key)] = "retry_errors"
    next_key += 1
    run_choices.append((str(next_key), "работать заданное время и корректно остановиться"))
    run_mapping[str(next_key)] = "time_limit"

    run_choice = prompt_choice("Режим работы", run_choices, "1")
    run_mode = run_mapping[run_choice]

    next_n = None
    batch_start = None
    batch_end = None
    time_limit_minutes = None
    folder_count = None
    if run_mode == "next_n":
        next_n = prompt_int("Сколько файлов обработать", 100, 1)
    elif run_mode == "batch_range":
        # Подсказки берутся из реально найденных папок batch_XXX, а не из
        # ранее введённого значения. Это не меняет фильтрацию — только
        # корректно показывает минимальный и максимальный доступные номера.
        batch_numbers = []
        for group in batch_groups:
            match = BATCH_RE.match(group.name)
            if match:
                batch_numbers.append(int(match.group(1)))
        default_batch_start = min(batch_numbers) if batch_numbers else 1
        default_batch_end = max(batch_numbers) if batch_numbers else default_batch_start
        batch_start = prompt_int(
            "Начальный номер batch", default_batch_start, 0, default_batch_end
        )
        batch_end = prompt_int(
            "Конечный номер batch", default_batch_end, batch_start, default_batch_end
        )
    elif run_mode == "folder_next_n":
        folder_count = prompt_int(
            "Сколько следующих папок обработать",
            min(10, len(top_level_groups)),
            1,
            len(top_level_groups),
        )
    elif run_mode == "time_limit":
        time_limit_minutes = prompt_int("Сколько минут работать", 120, 1)

    default_workers = int(config.get("processing", {}).get("default_workers", 2))
    workers = prompt_int("Количество параллельных воркеров", default_workers, 1, 8)

    default_vad = bool(config.get("transcription", {}).get("vad", {}).get("enabled", True))
    vad_choice = prompt_choice(
        "Использовать VAD (удаление длинной тишины)?",
        [("1", "да"), ("2", "нет")],
        "1" if default_vad else "2",
    )
    vad_enabled = vad_choice == "1"

    force_choice = prompt_choice(
        "Перезаписывать уже готовые TXT/SRT?",
        [("1", "нет, продолжить с недостающих"), ("2", "да, обработать заново")],
        "1",
    )

    return RunOptions(
        source_root=source_root,
        output_mode=output_mode,
        output_root=output_root,
        workers=workers,
        channel=channel,
        run_mode=run_mode,
        next_n=next_n,
        batch_start=batch_start,
        batch_end=batch_end,
        time_limit_minutes=time_limit_minutes,
        folder_count=folder_count,
        vad_enabled=vad_enabled,
        force_reprocess=force_choice == "2",
    )


def should_stop_from_keyboard() -> bool:
    if os.name != "nt":
        return False
    try:
        import msvcrt

        if msvcrt.kbhit():
            char = msvcrt.getwch().casefold()
            return char in {"s", "ы", "q", "й"}
    except Exception:
        return False
    return False


def print_plan(options: RunOptions, tasks: Sequence[Task], model_preview: tuple[str, str]) -> None:
    safe_print("\nПлан запуска")
    safe_print("-" * 72)
    safe_print(f"Источник:      {options.source_root}")
    if options.output_mode == "same":
        safe_print("Результаты:    рядом с аудио")
    else:
        safe_print(f"Результаты:    {options.output_root} (зеркальная структура)")
    safe_print(f"Канал:         {options.channel}")
    safe_print(f"Режим:         {options.run_mode}")
    safe_print(f"Воркеры:       {options.workers}")
    safe_print(f"VAD:           {'включён' if options.vad_enabled else 'выключен'}")
    safe_print(f"Модель:        {model_preview[0]} ({model_preview[1]})")
    safe_print(f"Файлов:        {len(tasks)}")
    safe_print("-" * 72)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}ч {minutes:02d}м {secs:02d}с"
    return f"{minutes}м {secs:02d}с"


def print_progress(stats: RunStats, result: TaskResult) -> None:
    done = stats.completed + stats.failed
    rate = done / stats.elapsed if stats.elapsed > 0 else 0
    remaining = max(0, stats.total_selected - done)
    eta = remaining / rate if rate > 0 else 0
    marker = "OK" if result.status == "success" else "ОШИБКА"
    safe_print(
        f"[{done}/{stats.total_selected}] {marker} | "
        f"{result.source.name} | {result.elapsed_seconds:.1f}с | "
        f"ETA {format_duration(eta)}"
    )
    if result.message:
        safe_print(f"    {result.message}")


def prepare_tasks(
    options: RunOptions,
    config: dict[str, Any],
    db: StateDB,
) -> tuple[list[Task], int]:
    sources = scan_sources(options, config)
    error_paths = db.error_paths(options.project_key) if options.run_mode == "retry_errors" else None
    tasks: list[Task] = []
    skipped = 0
    for source in sources:
        if error_paths is not None and str(source.resolve()) not in error_paths:
            continue
        task = build_task(source, options, force=options.force_reprocess)
        if task is None:
            skipped += 1
            continue
        tasks.append(task)

    collisions = find_collisions(tasks)
    if collisions:
        collision_sources = {task.source for group in collisions.values() for task in group}
        for group in collisions.values():
            safe_print("\nКОЛЛИЗИЯ ИМЁН ВЫХОДНЫХ ФАЙЛОВ:")
            for task in group:
                safe_print(f"  {task.source}")
            safe_print(f"  -> общий выход: {group[0].output_base}")
        tasks = [task for task in tasks if task.source not in collision_sources]
        safe_print(
            f"\nПропущено файлов с конфликтующими именами: {len(collision_sources)}"
        )

    if options.run_mode == "next_n" and options.next_n is not None:
        tasks = tasks[: options.next_n]
    elif options.run_mode == "folder_next_n" and options.folder_count is not None:
        selected_groups: list[str] = []
        for task in tasks:
            relative = Path(task.relative_path)
            if len(relative.parts) < 2:
                continue
            group_name = relative.parts[0]
            if group_name not in selected_groups:
                selected_groups.append(group_name)
            if len(selected_groups) >= options.folder_count:
                break
        selected = set(selected_groups)
        tasks = [
            task
            for task in tasks
            if len(Path(task.relative_path).parts) >= 2
            and Path(task.relative_path).parts[0] in selected
        ]

    return tasks, skipped


def run_transcription(options: RunOptions, config: dict[str, Any], verbose: bool = False) -> int:
    state_dir = determine_state_dir(options)
    state_dir.mkdir(parents=True, exist_ok=True)
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    log_path = setup_logging(state_dir, run_id, verbose=verbose)
    db = StateDB(state_dir / "state.sqlite3")
    stats = RunStats()
    status = "completed"
    try:
        db.start_run(run_id, options)
        tasks, already_complete = prepare_tasks(options, config, db)
        stats.skipped = already_complete
        stats.total_selected = len(tasks)
        try:
            model_preview = resolve_model(config)
        except Exception as exc:
            model_preview = (f"ошибка определения: {exc}", "unknown")
        print_plan(options, tasks, model_preview)
        if already_complete:
            safe_print(f"Уже готовы и пропущены: {already_complete}")
        if not tasks:
            safe_print("\nНет файлов для обработки.")
            db.finish_run(run_id, stats, "completed")
            return 0

        confirm = prompt_choice(
            "Запустить обработку?",
            [("1", "запустить"), ("2", "отмена")],
            "1",
        )
        if confirm != "1":
            db.finish_run(run_id, stats, "cancelled")
            return 0

        runtime = ModelRuntime(config, options.workers, options.vad_enabled)
        deadline = (
            time.monotonic() + options.time_limit_minutes * 60
            if options.time_limit_minutes
            else None
        )
        stop_requested = False
        soft_stop_event = threading.Event()
        interrupt_count = 0
        previous_sigint = signal.getsignal(signal.SIGINT)

        def handle_sigint(signum: int, frame: Any) -> None:
            nonlocal interrupt_count
            interrupt_count += 1
            if interrupt_count == 1:
                soft_stop_event.set()
                safe_print(
                    "\nЗапрошена мягкая остановка Ctrl+C. "
                    "Повторный Ctrl+C завершит программу немедленно."
                )
                return
            signal.signal(signal.SIGINT, previous_sigint)
            raise KeyboardInterrupt

        signal.signal(signal.SIGINT, handle_sigint)
        task_iter = iter(tasks)
        pending: dict[concurrent.futures.Future[TaskResult], Task] = {}

        safe_print("\nВо время работы нажмите S, чтобы остановиться после текущих файлов.")
        safe_print("Ctrl+C делает то же самое; повторный Ctrl+C аварийно завершает программу.\n")

        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=options.workers,
                thread_name_prefix="worker",
            ) as executor:
                while len(pending) < options.workers:
                    try:
                        task = next(task_iter)
                    except StopIteration:
                        break
                    db.mark_started(run_id, options, task)
                    pending[executor.submit(process_task, task, runtime, config)] = task

                while pending:
                    if not stop_requested and should_stop_from_keyboard():
                        soft_stop_event.set()
                    if not stop_requested and soft_stop_event.is_set():
                        stop_requested = True
                        status = "stopped"
                        safe_print(
                            "\nЗапрошена мягкая остановка. Новые файлы не запускаются."
                        )
                    if not stop_requested and deadline and time.monotonic() >= deadline:
                        stop_requested = True
                        status = "time_limit"
                        safe_print(
                            "\nДостигнут лимит времени. Завершаются текущие файлы."
                        )

                    done, _ = concurrent.futures.wait(
                        pending,
                        timeout=0.25,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in done:
                        task = pending.pop(future)
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = TaskResult(
                                source=task.source,
                                output_base=task.output_base,
                                status="error",
                                action=task.action,
                                elapsed_seconds=0,
                                message=f"Необработанное исключение: {exc}",
                            )
                            logging.error(
                                "Future failure: %s\n%s", exc, traceback.format_exc()
                            )
                        db.mark_result(run_id, options, result)
                        if result.status == "success":
                            stats.completed += 1
                            if result.action.startswith("regenerate"):
                                stats.regenerated += 1
                        else:
                            stats.failed += 1
                        print_progress(stats, result)

                    while not stop_requested and len(pending) < options.workers:
                        try:
                            task = next(task_iter)
                        except StopIteration:
                            break
                        db.mark_started(run_id, options, task)
                        pending[executor.submit(process_task, task, runtime, config)] = task
        finally:
            signal.signal(signal.SIGINT, previous_sigint)

        if stop_requested:
            unstarted = stats.total_selected - stats.completed - stats.failed
            if unstarted > 0:
                safe_print(f"Не запущено файлов: {unstarted}. Они будут подхвачены в следующий раз.")
        safe_print("\nИтог")
        safe_print("-" * 72)
        safe_print(f"Успешно:             {stats.completed}")
        safe_print(f"Из них восстановлено:{stats.regenerated}")
        safe_print(f"Ошибки:              {stats.failed}")
        safe_print(f"Ранее готово:        {stats.skipped}")
        safe_print(f"Время:               {format_duration(stats.elapsed)}")
        safe_print(f"Журнал:              {log_path}")
        safe_print(f"Состояние:           {state_dir / 'state.sqlite3'}")
        safe_print("-" * 72)
        db.finish_run(run_id, stats, status)
        return 1 if stats.failed else 0
    except KeyboardInterrupt:
        status = "interrupted"
        safe_print("\nПолучено прерывание. Текущие операции могут завершиться некорректно.")
        db.finish_run(run_id, stats, status)
        return 130
    except Exception:
        logging.exception("Критическая ошибка")
        safe_print("\nКритическая ошибка. Подробности записаны в журнал.")
        db.finish_run(run_id, stats, "fatal_error")
        return 2
    finally:
        db.close()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--verbose", action="store_true", help="подробный журнал")
    parser.add_argument("--version", action="version", version=APP_VERSION)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config()
        options = interactive_wizard(config)
        return run_transcription(options, config, verbose=args.verbose)
    except KeyboardInterrupt:
        safe_print("\nОтменено пользователем.")
        return 130
    except Exception as exc:
        safe_print(f"\nОшибка: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
