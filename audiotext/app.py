from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, Qt, QThread, Signal, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QAbstractSpinBox, QHeaderView, QSpinBox, QSplitter, QStyle, QSystemTrayIcon, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget
)

from . import __version__
from . import engine

APP_NAME = "Audiotext"
ORG_NAME = "Mikhail Zuev"
PRODUCT_DESCRIPTION = "Многопоточный транскрибатор"
SUPPORTED = tuple(sorted(engine.SUPPORTED_AUDIO_EXTENSIONS))

# User-facing names are kept separate from the technical model identifiers.
# Unknown Hugging Face IDs remain supported because the combo boxes are editable.
MODEL_PRESETS = [
    ("Large V3 Turbo — рекомендуется / recommended", "mobiuslabsgmbh/faster-whisper-large-v3-turbo"),
    ("Large V3 — максимальная точность / max accuracy", "Systran/faster-whisper-large-v3"),
    ("Medium — быстрее / faster", "medium"),
    ("Small — очень быстро / fastest", "small"),
    ("Другая модель… / Custom model…", "__custom__"),
]
DEFAULT_MODEL_REF = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"


def populate_model_combo(combo: QComboBox, current_ref: str) -> None:
    """Fill a model selector with friendly labels while preserving custom IDs."""
    combo.clear()
    combo.setEditable(True)
    for label, model_ref in MODEL_PRESETS:
        combo.addItem(label, model_ref)
    index = combo.findData(current_ref)
    if index >= 0:
        combo.setCurrentIndex(index)
    else:
        combo.setCurrentIndex(-1)
        combo.setEditText(current_ref)

    def handle_preset(index: int) -> None:
        if combo.itemData(index) == "__custom__":
            combo.setCurrentIndex(-1)
            combo.setEditText("")
            combo.setFocus()

    combo.activated.connect(handle_preset)


def selected_model_ref(combo: QComboBox) -> str:
    """Return the technical ID, not the friendly text shown to the user."""
    index = combo.currentIndex()
    if index >= 0:
        data = combo.itemData(index)
        if data and data != "__custom__":
            return str(data)
    return combo.currentText().strip()


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", app_root()))
    candidate = base / "resources" / name
    return candidate if candidate.exists() else app_root() / "resources" / name


def data_root() -> Path:
    """Portable application data stored next to the program/EXE."""
    p = app_root() / "data"
    p.mkdir(parents=True, exist_ok=True)
    return p


def models_root() -> Path:
    """Downloaded models live next to the program/EXE."""
    p = app_root() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def legacy_data_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / APP_NAME


def portable_settings() -> QSettings:
    ini_path = data_root() / "settings.ini"
    settings = QSettings(str(ini_path), QSettings.IniFormat)
    # One-time migration of the old registry-backed preferences. Large model
    # directories are deliberately not copied automatically. Existing absolute
    # model_path values remain valid until the user moves/reselects the model.
    if not ini_path.exists() or not settings.allKeys():
        legacy = QSettings(ORG_NAME, APP_NAME)
        for key in legacy.allKeys():
            settings.setValue(key, legacy.value(key))
        settings.sync()
    return settings


def prevent_sleep(enable: bool) -> None:
    if os.name != "nt":
        return
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_AWAYMODE_REQUIRED = 0x00000040
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED if enable else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


TEXT = {
    "ru": {
        "files": "Файлы", "folders": "Папки", "settings": "Настройки",
        "add_files": "Добавить файлы", "add_folder": "Добавить папку",
        "remove": "Удалить", "clear_done": "Очистить завершённые", "start": "Запустить",
        "stop": "Остановить после текущих", "status": "Статус", "file": "Файл",
        "output": "Результат", "progress": "Прогресс", "waiting": "Ожидает",
        "running": "Обработка", "done": "Готово", "error": "Ошибка",
        "source_folder": "Исходная папка", "choose": "Выбрать…",
        "output_mode": "Сохранение", "alongside": "Рядом с аудио",
        "mirror": "Зеркальная структура", "output_folder": "Папка результатов",
        "all_audio": "Все аудиофайлы рекурсивно", "model": "Модель",
        "language": "Язык распознавания", "workers": "Воркеры", "vad": "Использовать VAD",
        "txt": "TXT", "srt": "SRT", "json": "Служебный JSON",
        "overwrite": "Перезаписывать готовые TXT/SRT", "ui_language": "Язык интерфейса",
        "model_path": "Локальная модель", "download": "Скачать модель",
        "browse_model": "Указать папку модели", "diagnostics": "Диагностика",
        "export_report": "Экспортировать отчёт", "preview": "Предпросмотр",
        "resume_title": "Незавершённая очередь", "resume_text": "Обнаружены незавершённые задания. Продолжить?",
        "finished": "Обработка завершена", "no_gpu": "NVIDIA GPU/CUDA не обнаружены или недоступны.",
        "advanced": "Расширенные настройки", "open_output": "Открыть папку результата",
        "folder_mode_hint": "Папка сканируется рекурсивно. Существующие корректные результаты пропускаются.",
    },
    "en": {
        "files": "Files", "folders": "Folders", "settings": "Settings",
        "add_files": "Add files", "add_folder": "Add folder", "remove": "Remove",
        "clear_done": "Clear completed", "start": "Start", "stop": "Stop after current",
        "status": "Status", "file": "File", "output": "Output", "progress": "Progress",
        "waiting": "Waiting", "running": "Processing", "done": "Done", "error": "Error",
        "source_folder": "Source folder", "choose": "Browse…", "output_mode": "Save to",
        "alongside": "Next to audio", "mirror": "Mirrored structure", "output_folder": "Output folder",
        "all_audio": "All audio files recursively", "model": "Model", "language": "Recognition language",
        "workers": "Workers", "vad": "Use VAD", "txt": "TXT", "srt": "SRT",
        "json": "Service JSON", "overwrite": "Overwrite existing TXT/SRT",
        "ui_language": "Interface language", "model_path": "Local model", "download": "Download model",
        "browse_model": "Select model folder", "diagnostics": "Diagnostics",
        "export_report": "Export report", "preview": "Preview", "resume_title": "Unfinished queue",
        "resume_text": "Unfinished jobs were found. Continue?", "finished": "Processing completed",
        "no_gpu": "NVIDIA GPU/CUDA is unavailable.", "advanced": "Advanced settings",
        "open_output": "Open output folder", "folder_mode_hint": "The folder is scanned recursively. Valid existing outputs are skipped.",
    }
}


@dataclass
class QueueItem:
    source: Path
    output_base: Path
    status: str = "waiting"
    progress: int = 0
    message: str = ""


class DropTable(QTableWidget):
    filesDropped = Signal(list)
    def __init__(self):
        super().__init__(0, 4)
        self.setAcceptDrops(True)
        self.setSelectionBehavior(QTableWidget.SelectRows)
        self.setSelectionMode(QTableWidget.ExtendedSelection)
        self.setEditTriggers(QTableWidget.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        # Каждая секция — действительно интерактивная. Явная настройка по индексам
        # надёжнее общей на некоторых стилях Windows/Qt.
        for section in range(self.columnCount()):
            header.setSectionResizeMode(section, QHeaderView.Interactive)
        header.setSectionsClickable(True)
        header.setSectionsMovable(False)
        header.setCascadingSectionResizes(False)
        header.setMinimumSectionSize(70)
        header.setStretchLastSection(False)
        header.setDefaultAlignment(Qt.AlignCenter)
        self.setColumnWidth(0, 360)
        self.setColumnWidth(1, 140)
        self.setColumnWidth(2, 110)
        self.setColumnWidth(3, 430)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls(): event.acceptProposedAction()
    def dragMoveEvent(self, event): event.acceptProposedAction()
    def dropEvent(self, event):
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        self.filesDropped.emit(paths)
        event.acceptProposedAction()


class ProcessingThread(QThread):
    itemStarted = Signal(int)
    itemFinished = Signal(int, bool, str, str)
    overall = Signal(int, int)
    fatal = Signal(str)

    def __init__(self, items: list[QueueItem], config: dict[str, Any], workers: int, vad: bool,
                 outputs: dict[str, bool], force: bool):
        super().__init__()
        self.items = items
        self.config = config
        self.workers = max(1, workers)
        self.vad = vad
        self.outputs = outputs
        self.force = force
        self.stop_requested = threading.Event()

    def request_soft_stop(self): self.stop_requested.set()

    def _task(self, q: QueueItem):
        source = q.source
        base = q.output_base
        txt, srt, js = base.with_suffix(".txt"), base.with_suffix(".srt"), base.with_suffix(".segments.json")
        task = engine.Task(
            source=source, output_base=base, txt_path=txt, srt_path=srt, json_path=js,
            needs_txt=self.outputs["txt"] and (self.force or not engine.is_valid_txt(txt, True)),
            needs_srt=self.outputs["srt"] and (self.force or not engine.is_valid_srt(srt, True)),
            needs_json=self.outputs["json"] and (self.force or not engine.is_valid_json(js)),
            action="transcribe", relative_path=source.name, batch_number=None, channel=None,
        )
        if not (task.needs_txt or task.needs_srt or task.needs_json):
            return engine.TaskResult(source, base, "success", "skip", 0.0, "Already complete")
        return task

    def run(self):
        try:
            runtime = engine.ModelRuntime(self.config, self.workers, self.vad)
            pending = [(i, self._task(q)) for i, q in enumerate(self.items)]
            done_count = 0
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = {}
                for i, task_or_result in pending:
                    if self.stop_requested.is_set(): break
                    self.itemStarted.emit(i)
                    if isinstance(task_or_result, engine.TaskResult):
                        self.itemFinished.emit(i, True, task_or_result.message, str(task_or_result.output_base))
                        done_count += 1; self.overall.emit(done_count, len(pending)); continue
                    futures[pool.submit(engine.process_task, task_or_result, runtime, self.config)] = i
                for future in as_completed(futures):
                    i = futures[future]
                    result = future.result()
                    ok = result.status == "success"
                    self.itemFinished.emit(i, ok, result.message, str(result.output_base))
                    done_count += 1; self.overall.emit(done_count, len(pending))
        except Exception:
            self.fatal.emit(traceback.format_exc())


class ModelDownloadThread(QThread):
    progressText = Signal(str)
    completed = Signal(str)
    failed = Signal(str)
    def __init__(self, repo_id: str, target: Path):
        super().__init__(); self.repo_id = repo_id; self.target = target
    def run(self):
        try:
            from huggingface_hub import snapshot_download
            self.progressText.emit(f"Downloading {self.repo_id}…")
            path = snapshot_download(repo_id=self.repo_id, local_dir=str(self.target))
            self.completed.emit(path)
        except Exception as exc: self.failed.emit(f"{type(exc).__name__}: {exc}")


class PreviewDialog(QDialog):
    def __init__(self, base: Path, parent=None):
        super().__init__(parent); self.setWindowTitle("Audiotext — Preview"); self.resize(900, 650)
        tabs = QTabWidget();
        for title, suffix in [("TXT", ".txt"), ("SRT", ".srt"), ("JSON", ".segments.json")]:
            edit = QPlainTextEdit(); edit.setReadOnly(True); p = base.with_suffix(suffix)
            try: edit.setPlainText(p.read_text(encoding="utf-8-sig") if p.exists() else "File not found")
            except Exception as exc: edit.setPlainText(str(exc))
            tabs.addTab(edit, title)
        lay = QVBoxLayout(self); lay.addWidget(tabs)


class SettingsDialog(QDialog):
    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent); self.settings = settings; self.setWindowTitle("Audiotext — Settings"); self.resize(650, 460)
        self.tabs = QTabWidget(); self.general = QWidget(); self.models = QWidget(); self.diag = QWidget()
        self.tabs.addTab(self.general, "Основные / General"); self.tabs.addTab(self.models, "Модели / Models"); self.tabs.addTab(self.diag, "Диагностика")
        f = QFormLayout(self.general)
        self.ui_lang = QComboBox(); self.ui_lang.addItem("Русский", "ru"); self.ui_lang.addItem("English", "en")
        idx = self.ui_lang.findData(settings.value("ui_language", "ru")); self.ui_lang.setCurrentIndex(max(0, idx)); f.addRow("Язык интерфейса", self.ui_lang)
        self.rec_lang = QComboBox(); self.rec_lang.setEditable(True); self.rec_lang.addItems(["ru", "en", "auto"]); self.rec_lang.setCurrentText(settings.value("recognition_language", "ru")); f.addRow("Язык распознавания", self.rec_lang)
        mf = QFormLayout(self.models)
        self.model_combo = QComboBox(); populate_model_combo(self.model_combo, str(settings.value("model_ref", DEFAULT_MODEL_REF))); mf.addRow("Модель", self.model_combo)
        row = QHBoxLayout(); self.model_path = QLineEdit(settings.value("model_path", "")); b = QPushButton("Указать…"); b.clicked.connect(self.browse_model); row.addWidget(self.model_path); row.addWidget(b); mf.addRow("Локальная папка", row)
        self.download_btn = QPushButton("Скачать выбранную модель"); self.download_btn.clicked.connect(self.download_model); self.download_status = QLabel(""); mf.addRow(self.download_btn); mf.addRow(self.download_status)
        df = QVBoxLayout(self.diag); self.diag_text = QPlainTextEdit(); self.diag_text.setReadOnly(True); btn = QPushButton("Запустить диагностику"); btn.clicked.connect(self.run_diag); exp = QPushButton("Экспортировать отчёт"); exp.clicked.connect(self.export_diag); df.addWidget(btn); df.addWidget(self.diag_text); df.addWidget(exp)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel); buttons.accepted.connect(self.save); buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self); lay.addWidget(self.tabs); lay.addWidget(buttons)

    def browse_model(self):
        p = QFileDialog.getExistingDirectory(self, "Model folder", self.model_path.text() or str(models_root()))
        if p: self.model_path.setText(p)
    def download_model(self):
        repo = selected_model_ref(self.model_combo)
        if not repo:
            QMessageBox.warning(self, APP_NAME, "Укажите Hugging Face ID модели или выберите готовый вариант.")
            return
        target = models_root() / repo.replace("/", "--")
        self.download_btn.setEnabled(False); self.dl = ModelDownloadThread(repo, target); self.dl.progressText.connect(self.download_status.setText)
        self.dl.completed.connect(lambda p: (self.model_path.setText(p), self.download_status.setText("Готово"), self.download_btn.setEnabled(True)))
        self.dl.failed.connect(lambda e: (self.download_status.setText(e), self.download_btn.setEnabled(True))); self.dl.start()
    def run_diag(self):
        lines = [f"Audiotext {__version__}", f"Python: {sys.version}", f"Executable: {sys.executable}", f"Data: {data_root()}", f"Models: {models_root()}", f"Legacy data: {legacy_data_root()}"]
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"], capture_output=True, text=True, timeout=15)
            lines += ["NVIDIA: " + (r.stdout.strip() or r.stderr.strip())]
        except Exception as exc: lines += ["NVIDIA: ERROR " + str(exc)]
        try:
            import ctranslate2; lines += [f"CTranslate2: {ctranslate2.__version__}", f"CUDA devices: {ctranslate2.get_cuda_device_count()}"]
        except Exception as exc: lines += ["CTranslate2: ERROR " + str(exc)]
        try:
            import faster_whisper; lines += [f"faster-whisper: {getattr(faster_whisper, '__version__', 'installed')}"]
        except Exception as exc: lines += ["faster-whisper: ERROR " + str(exc)]
        self.diag_text.setPlainText("\n".join(lines))
    def export_diag(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save report", str(Path.home()/"Audiotext_diagnostics.txt"), "Text (*.txt)")
        if p: Path(p).write_text(self.diag_text.toPlainText(), encoding="utf-8")
    def save(self):
        self.settings.setValue("ui_language", self.ui_lang.currentData()); self.settings.setValue("recognition_language", self.rec_lang.currentText().strip())
        self.settings.setValue("model_ref", selected_model_ref(self.model_combo)); self.settings.setValue("model_path", self.model_path.text().strip()); self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__(); self.settings = portable_settings(); self.lang = self.settings.value("ui_language", "ru")
        self.items: list[QueueItem] = []; self.worker_thread = None; self.setWindowTitle(f"Audiotext {__version__}"); self.resize(1180, 760)
        icon = QIcon(str(resource_path("Audiotext.ico"))); self.setWindowIcon(icon)
        self._build_ui(); self._build_tray(icon); self._apply_style(); self._restore_table_columns(); self.restore_state(); self.retranslate()

    def t(self, key): return TEXT.get(self.lang, TEXT["ru"]).get(key, key)
    def _build_ui(self):
        self.tabs = QTabWidget(); self.setCentralWidget(self.tabs); self.files_tab = QWidget(); self.folder_tab = QWidget(); self.tabs.addTab(self.files_tab, ""); self.tabs.addTab(self.folder_tab, "")
        self.settings_btn = QPushButton(); self.settings_btn.setObjectName("settingsCornerButton")
        self.settings_btn.clicked.connect(self.open_settings)
        self.tabs.setCornerWidget(self.settings_btn, Qt.TopRightCorner)
        self.table = DropTable(); self.table.filesDropped.connect(self.handle_drop); self.table.cellDoubleClicked.connect(self.preview_row)
        self.add_files_btn = QPushButton(); self.add_files_btn.clicked.connect(self.add_files); self.add_folder_files_btn = QPushButton(); self.add_folder_files_btn.clicked.connect(self.add_folder_to_queue)
        self.remove_btn = QPushButton(); self.remove_btn.clicked.connect(self.remove_selected); self.clear_done_btn = QPushButton(); self.clear_done_btn.clicked.connect(self.clear_done)
        top = QHBoxLayout(); [top.addWidget(w) for w in (self.add_files_btn, self.add_folder_files_btn, self.remove_btn, self.clear_done_btn)]; top.addStretch()
        self.quick_box = QGroupBox(); qf = QGridLayout(self.quick_box)
        self.model = QComboBox(); populate_model_combo(self.model, str(self.settings.value("model_ref", DEFAULT_MODEL_REF)))
        self.workers = QSpinBox()
        self.workers.setRange(1, 32)
        self.workers.setSingleStep(1)
        self.workers.setAccelerated(True)
        self.workers.setWrapping(False)
        self.workers.setButtonSymbols(QAbstractSpinBox.UpDownArrows)
        self.workers.setKeyboardTracking(False)
        self.workers.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.workers.setValue(int(self.settings.value("workers", 2)))
        self.vad = QCheckBox(); self.vad.setChecked(self.settings.value("vad", True, type=bool)); self.force = QCheckBox(); self.force.setChecked(False)
        self.txt = QCheckBox("TXT"); self.txt.setChecked(True); self.srt = QCheckBox("SRT"); self.srt.setChecked(True); self.js = QCheckBox("JSON"); self.js.setChecked(True)
        qf.addWidget(QLabel("Model"),0,0); qf.addWidget(self.model,0,1,1,3); qf.addWidget(QLabel("Workers"),1,0); qf.addWidget(self.workers,1,1); qf.addWidget(self.vad,1,2); qf.addWidget(self.force,1,3); qf.addWidget(self.txt,2,1); qf.addWidget(self.srt,2,2); qf.addWidget(self.js,2,3)
        self.start_btn = QPushButton(); self.start_btn.clicked.connect(self.start_processing); self.stop_btn = QPushButton(); self.stop_btn.setEnabled(False); self.stop_btn.clicked.connect(self.stop_processing)
        self.overall = QProgressBar(); self.overall.setRange(0,100); self.status_label = QLabel("")
        bottom = QHBoxLayout(); bottom.addWidget(self.start_btn); bottom.addWidget(self.stop_btn); bottom.addWidget(self.overall,1); bottom.addWidget(self.status_label)
        fl = QVBoxLayout(self.files_tab); fl.addLayout(top); fl.addWidget(self.table,1); fl.addWidget(self.quick_box); fl.addLayout(bottom)
        # folder tab
        form = QFormLayout(); self.source_edit = QLineEdit(self.settings.value("last_source", "")); sb = QPushButton(); sb.clicked.connect(self.choose_source); sr = QHBoxLayout(); sr.addWidget(self.source_edit); sr.addWidget(sb); self.source_btn = sb
        self.output_mode = QComboBox(); self.output_mode.addItem("Рядом с аудио", "alongside"); self.output_mode.addItem("Зеркальная структура", "mirror"); self.output_edit = QLineEdit(self.settings.value("last_output", "")); ob = QPushButton(); ob.clicked.connect(self.choose_output); orow = QHBoxLayout(); orow.addWidget(self.output_edit); orow.addWidget(ob); self.output_btn = ob
        form.addRow("Источник", sr); form.addRow("Сохранение", self.output_mode); form.addRow("Результаты", orow)
        self.folder_hint = QLabel(); self.scan_btn = QPushButton("Добавить найденные файлы в очередь"); self.scan_btn.clicked.connect(self.scan_folder)
        fol = QVBoxLayout(self.folder_tab); fol.addLayout(form); fol.addWidget(self.folder_hint); fol.addWidget(self.scan_btn); fol.addStretch()
        # Главное меню намеренно не используется: настройки доступны справа от вкладок.
        self.menuBar().hide()


    def _restore_table_columns(self):
        defaults = [360, 140, 110, 430]
        for index, default in enumerate(defaults):
            width = int(self.settings.value(f"table_column_{index}", default))
            self.table.setColumnWidth(index, max(70, width))

    def _save_table_columns(self):
        for index in range(self.table.columnCount()):
            self.settings.setValue(f"table_column_{index}", self.table.columnWidth(index))

    def _build_tray(self, icon):
        self.tray = QSystemTrayIcon(icon, self); m = QMenu(); show = QAction("Audiotext", self); show.triggered.connect(self.showNormal); quit_a = QAction("Exit", self); quit_a.triggered.connect(QApplication.quit); m.addAction(show); m.addAction(quit_a); self.tray.setContextMenu(m); self.tray.activated.connect(lambda r: self.showNormal() if r == QSystemTrayIcon.DoubleClick else None); self.tray.show()
    def _apply_style(self):
        up_arrow = resource_path("spin_up.svg").as_posix()
        down_arrow = resource_path("spin_down.svg").as_posix()
        self.setStyleSheet("""
        QWidget { background:#fbfbfc; color:#25262a; font-size:10pt; }
        QMainWindow, QDialog { background:#fbfbfc; }

        QPushButton {
            background:#ffffff;
            border:1px solid #bfc3cb;
            border-radius:7px;
            padding:7px 13px;
        }
        QPushButton:hover { border-color:#ef3b88; background:#fff8fb; }
        QPushButton:pressed { background:#fdeaf2; }
        QPushButton:disabled { color:#9b9ea6; background:#f3f4f6; border-color:#d8dae0; }
        QPushButton:default, QPushButton#primary {
            background:#ef3b88; color:white; border:1px solid #ef3b88; font-weight:600;
        }
        QPushButton#settingsCornerButton {
            background:#ffffff;
            border:1px solid #bfc3cb;
            border-radius:7px;
            padding:6px 14px;
            margin:4px 10px 4px 4px;
        }
        QPushButton#settingsCornerButton:hover { border-color:#ef3b88; background:#fff8fb; color:#d92472; }

        QTabWidget::pane {
            border:1px solid #d7d9df;
            border-radius:5px;
            background:#fbfbfc;
            top:-1px;
        }
        QTabBar { background:transparent; }
        QTabBar::tab {
            background:transparent;
            border:none;
            border-bottom:3px solid transparent;
            margin:0;
            padding:10px 18px;
        }
        QTabBar::tab:hover { color:#d92472; background:#fff8fb; }
        QTabBar::tab:selected {
            color:#d92472;
            border:none;
            border-bottom:3px solid #ef3b88;
            font-weight:500;
        }

        QCheckBox { spacing:7px; background:transparent; }
        QCheckBox::indicator {
            width:17px;
            height:17px;
            border:1px solid #aeb3bd;
            border-radius:4px;
            background:#ffffff;
        }
        QCheckBox::indicator:hover { border-color:#ef3b88; }
        QCheckBox::indicator:checked {
            background:#ef3b88;
            border-color:#ef3b88;
            image:none;
        }
        QCheckBox::indicator:checked:disabled { background:#d9a5bd; border-color:#d9a5bd; }

        QGroupBox {
            border:1px solid #d7d9df;
            border-radius:7px;
            margin-top:12px;
            padding-top:10px;
            background:#ffffff;
        }
        QGroupBox::title {
            subcontrol-origin:margin;
            left:10px;
            padding:0 5px;
            background:#fbfbfc;
        }

        QProgressBar { border:1px solid #bfc3cb; border-radius:6px; text-align:center; background:white; }
        QProgressBar::chunk { background:#ef3b88; border-radius:5px; }
        QLineEdit, QComboBox, QPlainTextEdit, QTableWidget {
            background:white;
            border:1px solid #bfc3cb;
            border-radius:5px;
            padding:4px;
            selection-background-color:#f7bdd6;
            selection-color:#25262a;
        }
        QSpinBox {
            background:white;
            border:1px solid #bfc3cb;
            border-radius:5px;
            padding:4px 28px 4px 7px;
            min-height:24px;
            selection-background-color:#f7bdd6;
            selection-color:#25262a;
        }
        QSpinBox::up-button {
            subcontrol-origin:border;
            subcontrol-position:top right;
            width:24px;
            border-left:1px solid #bfc3cb;
            border-bottom:1px solid #d7d9df;
            border-top-right-radius:5px;
            background:#f7f7f9;
        }
        QSpinBox::down-button {
            subcontrol-origin:border;
            subcontrol-position:bottom right;
            width:24px;
            border-left:1px solid #bfc3cb;
            border-top:0;
            border-bottom-right-radius:5px;
            background:#f7f7f9;
        }
        QSpinBox::up-button:hover, QSpinBox::down-button:hover { background:#fff0f6; }
        QSpinBox::up-button:pressed, QSpinBox::down-button:pressed { background:#f7bdd6; }
        QSpinBox::up-arrow {
            image:url("__SPIN_UP__");
            width:12px;
            height:8px;
        }
        QSpinBox::down-arrow {
            image:url("__SPIN_DOWN__");
            width:12px;
            height:8px;
        }
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus, QTableWidget:focus {
            border:1px solid #ef3b88;
        }
        QHeaderView::section {
            background:#f0f1f4;
            padding:7px;
            border:0;
            border-right:1px solid #c6c9d0;
            border-bottom:1px solid #bfc3cb;
        }
        QHeaderView::section:last { border-right:0; }
        """.replace("__SPIN_UP__", up_arrow).replace("__SPIN_DOWN__", down_arrow)); self.start_btn.setObjectName("primary")

    def retranslate(self):
        self.tabs.setTabText(0,self.t("files")); self.tabs.setTabText(1,self.t("folders")); self.table.setHorizontalHeaderLabels([self.t("file"),self.t("status"),self.t("progress"),self.t("output")])
        self.add_files_btn.setText(self.t("add_files")); self.add_folder_files_btn.setText(self.t("add_folder")); self.remove_btn.setText(self.t("remove")); self.clear_done_btn.setText(self.t("clear_done")); self.start_btn.setText(self.t("start")); self.stop_btn.setText(self.t("stop")); self.vad.setText(self.t("vad")); self.force.setText(self.t("overwrite")); self.quick_box.setTitle(self.t("advanced")); self.source_btn.setText(self.t("choose")); self.output_btn.setText(self.t("choose")); self.folder_hint.setText(self.t("folder_mode_hint")); self.settings_btn.setText(self.t("settings"))

    def add_files(self):
        filt = "Audio (" + " ".join("*"+e for e in SUPPORTED) + ")"
        files, _ = QFileDialog.getOpenFileNames(self, self.t("add_files"), self.settings.value("last_source", str(Path.home())), filt)
        self.enqueue_paths([Path(x) for x in files])
    def add_folder_to_queue(self):
        p = QFileDialog.getExistingDirectory(self, self.t("add_folder"), self.settings.value("last_source", str(Path.home())))
        if p: self.enqueue_paths([Path(p)])
    def handle_drop(self, paths): self.enqueue_paths(paths)
    def enqueue_paths(self, paths):
        existing = {q.source.resolve() for q in self.items}
        files=[]
        for p in paths:
            if p.is_dir(): files.extend(x for x in p.rglob("*") if x.is_file() and x.suffix.lower() in engine.SUPPORTED_AUDIO_EXTENSIONS)
            elif p.is_file() and p.suffix.lower() in engine.SUPPORTED_AUDIO_EXTENSIONS: files.append(p)
        for p in sorted(files, key=engine.natural_sort_key):
            if p.resolve() in existing: continue
            self.items.append(QueueItem(p, p.with_suffix(""))); existing.add(p.resolve())
        if files: self.settings.setValue("last_source", str(files[0].parent))
        self.refresh_table(); self.save_queue()
    def refresh_table(self):
        self.table.setRowCount(len(self.items))
        for r,q in enumerate(self.items):
            vals=[q.source.name,self.t(q.status),str(q.progress)+"%",str(q.output_base.parent)]
            for c,v in enumerate(vals): self.table.setItem(r,c,QTableWidgetItem(v))
        # Не пересчитываем ширину автоматически: пользователь может свободно менять её мышью.
    def remove_selected(self):
        rows=sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            if 0<=r<len(self.items) and self.items[r].status!="running": self.items.pop(r)
        self.refresh_table(); self.save_queue()
    def clear_done(self): self.items=[q for q in self.items if q.status not in ("done","error")]; self.refresh_table(); self.save_queue()
    def choose_source(self):
        p=QFileDialog.getExistingDirectory(self,"Source",self.source_edit.text() or str(Path.home()))
        if p: self.source_edit.setText(p); self.settings.setValue("last_source",p)
    def choose_output(self):
        p=QFileDialog.getExistingDirectory(self,"Output",self.output_edit.text() or str(Path.home()))
        if p: self.output_edit.setText(p); self.settings.setValue("last_output",p)
    def scan_folder(self):
        src=Path(self.source_edit.text().strip())
        if not src.is_dir(): QMessageBox.warning(self,APP_NAME,"Исходная папка не найдена"); return
        files=[x for x in src.rglob("*") if x.is_file() and x.suffix.lower() in engine.SUPPORTED_AUDIO_EXTENSIONS]
        mode=self.output_mode.currentData(); out=Path(self.output_edit.text().strip()) if mode=="mirror" else None
        existing={q.source.resolve() for q in self.items}
        for p in sorted(files,key=engine.natural_sort_key):
            if p.resolve() in existing: continue
            base=p.with_suffix("") if mode=="alongside" else out / p.relative_to(src).with_suffix("")
            self.items.append(QueueItem(p,base)); existing.add(p.resolve())
        self.refresh_table(); self.save_queue(); self.tabs.setCurrentIndex(0)
    def build_config(self):
        model_path=self.settings.value("model_path",""); model_ref=model_path if model_path and Path(model_path).is_dir() else selected_model_ref(self.model)
        return {
            "model":{"name":model_ref,"device":"cuda","device_index":0,"compute_type":"float16","download_root":str(models_root()),"search_buzz_cache":False,"local_path":model_path or ""},
            "transcription":{"language":self.settings.value("recognition_language","ru"),"task":"transcribe","beam_size":5,"best_of":5,"temperature":[0.0,0.2,0.4,0.6,0.8,1.0],"condition_on_previous_text":True,"hotwords_file":"","vad":{"parameters":{"threshold":0.5,"min_silence_duration_ms":2000,"speech_pad_ms":400}}},
            "cuda":{"extra_dll_dirs":[]},"output":{"write_txt":self.txt.isChecked(),"write_srt":self.srt.isChecked(),"write_json":self.js.isChecked()}
        }
    def start_processing(self):
        selected=[q for q in self.items if q.status in ("waiting","error")]
        if not selected: return
        self.settings.setValue("workers",self.workers.value()); self.settings.setValue("vad",self.vad.isChecked()); self.settings.setValue("model_ref",selected_model_ref(self.model))
        prevent_sleep(True); self.start_btn.setEnabled(False); self.stop_btn.setEnabled(True); self.worker_thread=ProcessingThread(selected,self.build_config(),self.workers.value(),self.vad.isChecked(),{"txt":self.txt.isChecked(),"srt":self.srt.isChecked(),"json":self.js.isChecked()},self.force.isChecked())
        mapping={id(q):self.items.index(q) for q in selected}; self._run_items=selected; self.worker_thread.itemStarted.connect(lambda i:self.mark_started(mapping[id(selected[i])]))
        self.worker_thread.itemFinished.connect(lambda i,ok,msg,out:self.mark_finished(mapping[id(selected[i])],ok,msg,out)); self.worker_thread.overall.connect(self.update_overall); self.worker_thread.fatal.connect(self.show_fatal); self.worker_thread.finished.connect(self.run_finished); self.worker_thread.start()
    def stop_processing(self):
        if self.worker_thread: self.worker_thread.request_soft_stop(); self.stop_btn.setEnabled(False)
    def mark_started(self,i): self.items[i].status="running"; self.items[i].progress=10; self.refresh_table(); self.save_queue()
    def mark_finished(self,i,ok,msg,out): self.items[i].status="done" if ok else "error"; self.items[i].progress=100 if ok else 0; self.items[i].message=msg; self.refresh_table(); self.save_queue()
    def update_overall(self,d,t): self.overall.setValue(int(d*100/max(1,t))); self.status_label.setText(f"{d}/{t}")
    def show_fatal(self,text): QMessageBox.critical(self,APP_NAME,text)
    def run_finished(self):
        prevent_sleep(False); self.start_btn.setEnabled(True); self.stop_btn.setEnabled(False); self.tray.showMessage(APP_NAME,self.t("finished"),QSystemTrayIcon.Information,5000); self.save_queue()
    def preview_row(self,row,col):
        if 0<=row<len(self.items): PreviewDialog(self.items[row].output_base,self).exec()
    def open_settings(self):
        d=SettingsDialog(self.settings,self)
        if d.exec(): self.lang=self.settings.value("ui_language","ru"); populate_model_combo(self.model, str(self.settings.value("model_ref", DEFAULT_MODEL_REF))); self.retranslate()
    def save_queue(self):
        payload=[{"source":str(q.source),"output_base":str(q.output_base),"status":q.status if q.status!="running" else "waiting","progress":q.progress if q.status!="running" else 0,"message":q.message} for q in self.items]
        (data_root()/"queue.json").write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding="utf-8")
    def restore_state(self):
        p=data_root()/"queue.json"
        if not p.exists(): return
        try:
            raw=json.loads(p.read_text(encoding="utf-8")); unfinished=any(x.get("status") in ("waiting","running","error") for x in raw)
            load=True
            if unfinished: load=QMessageBox.question(self,self.t("resume_title"),self.t("resume_text"),QMessageBox.Yes|QMessageBox.No)==QMessageBox.Yes
            if load:
                self.items=[QueueItem(Path(x["source"]),Path(x["output_base"]),x.get("status","waiting"),int(x.get("progress",0)),x.get("message","")) for x in raw if Path(x["source"]).exists()]
                self.refresh_table()
            else: p.unlink(missing_ok=True)
        except Exception: pass
    def closeEvent(self,event):
        self.save_queue(); self._save_table_columns()
        if self.settings.value("minimize_to_tray",True,type=bool) and self.tray.isVisible(): self.hide(); event.ignore(); self.tray.showMessage(APP_NAME,"Audiotext работает в трее",QSystemTrayIcon.Information,2500)
        else: event.accept()


def main():
    QApplication.setOrganizationName(ORG_NAME); QApplication.setApplicationName(APP_NAME); QApplication.setApplicationVersion(__version__)
    app=QApplication(sys.argv); app.setQuitOnLastWindowClosed(False); icon=QIcon(str(resource_path("Audiotext.ico"))); app.setWindowIcon(icon)
    w=MainWindow(); w.show(); sys.exit(app.exec())

if __name__ == "__main__": main()
