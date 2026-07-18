# Архитектура Audiotext

- `audiotext/app.py` — GUI, очередь, настройки, модель, трей и диагностика.
- `audiotext/engine.py` — проверенный движок Faster Whisper v1.0.5: транскрибация, VAD, атомарная запись TXT/SRT/JSON.
- `%LOCALAPPDATA%\Audiotext\queue.json` — незавершённая очередь.
- `%LOCALAPPDATA%\Audiotext\models` — модели.
- QSettings — последние папки, модель, язык, VAD и число воркеров.

EXE собирается в режиме `onedir`, потому что зависимости CUDA/Qt/AV надёжнее распространять папкой, чем одним гигантским самораспаковывающимся EXE.
