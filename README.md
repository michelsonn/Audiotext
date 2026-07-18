# Audiotext

**Audiotext** — бесплатный многопоточный транскрибатор аудио для Windows на базе Faster Whisper.

Он рассчитан на локальную обработку больших наборов файлов с NVIDIA GPU: файлы не отправляются в облако, а результаты сохраняются в TXT, SRT и JSON. 

Ключевые особенности:

- возможность запуска транскрибации в параллельных потоках (воркерах) в количестве от 1 до 32 штук (например: RTX 5070 Ti 16GB при использовании 10 воркеров загружена только на 40-50%), что очень сильно ускоряет работу;
- во время транскрибации нескольких файлов локальная модель постоянно находится в памяти и не "перезагружается" при переходе к новому файлу, что значительно ускоряет весь процесс.

> Текущий статус: первая публичная версия. Программа уже используется автором в реальной пакетной обработке, но перед массовым распространением желательно дополнительное тестирование на разных компьютерах.

## Возможности

- локальная транскрибация через Faster Whisper и CTranslate2;
- Windows + NVIDIA CUDA;
- обработка отдельных файлов и целых папок;
- drag-and-drop;
- очередь заданий;
- от 1 до 32 параллельных воркеров;
- VAD для пропуска тишины;
- экспорт TXT, SRT и JSON;
- сохранение структуры исходных папок;
- пропуск уже готовых результатов;
- остановка после текущих заданий;
- восстановление незавершённой очереди;
- русский и английский интерфейс;
- portable-хранение моделей и настроек рядом с программой;
- загрузка рекомендованных моделей из интерфейса.

## Рекомендуемая модель

По умолчанию рекомендуется:

```text
mobiuslabsgmbh/faster-whisper-large-v3-turbo
```

При первом использовании модель загружается автоматически. Она не входит в репозиторий.

## Системные требования

- Windows 10/11 x64;
- современный Python 3 для запуска исходников;
- NVIDIA GPU с совместимыми CUDA-драйверами (рекомендуется RTX 3050 6GB или новее);
- достаточно свободного места для моделей и результатов;
- интернет при первой загрузке выбранной модели.

Audiotext в текущем виде не ориентирован на CPU-only, AMD GPU или macOS/Linux.

## Быстрый запуск из исходников

1. Установите Python и актуальный драйвер NVIDIA.
2. Клонируйте или скачайте репозиторий.
3. Запустите:

```bat
RUN_DEV.bat
```

Скрипт создаст локальное окружение `.venv`, установит зависимости и запустит приложение.

## Сборка portable-версии

Запустите:

```bat
BUILD_EXE.bat
```

Готовая сборка появится в:

```text
dist\Audiotext\
```

Распространять нужно **всю папку `dist\Audiotext`**, а не один файл `Audiotext.exe`.

## Portable-структура

После запуска рядом с программой создаются:

```text
Audiotext\
  Audiotext.exe
  models\
  data\
    settings.ini
    queue.json
```

Эти каталоги исключены из Git через `.gitignore`.

## Публикация релиза

1. Соберите приложение через `BUILD_EXE.bat`.
2. Проверьте запуск на чистой Windows-системе или отдельной учётной записи.
3. Упакуйте целиком `dist\Audiotext` в файл вида:

```text
Audiotext-1.0.6-windows-x64.zip
```

4. Создайте GitHub Release с тегом `v1.0.6` и приложите ZIP.
5. Не добавляйте модели, `.venv`, `.buildenv`, `build`, `dist` и пользовательские настройки в сам репозиторий.

Подробная последовательность: [`docs/GITHUB_PUBLISHING.md`](docs/GITHUB_PUBLISHING.md).

## Документация

- [`docs/SPECIFICATION.md`](docs/SPECIFICATION.md) — спецификация;
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — архитектура;
- [`docs/DEVELOPMENT_HISTORY.md`](docs/DEVELOPMENT_HISTORY.md) — история разработки;
- [`docs/CONTINUATION_GUIDE.md`](docs/CONTINUATION_GUIDE.md) — продолжение проекта;
- [`CHANGELOG.md`](CHANGELOG.md) — изменения по версиям;
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — участие в разработке;
- [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) — сторонние компоненты.

## Конфиденциальность

Распознавание выполняется локально. Однако при загрузке модели приложение обращается к хранилищу модели в интернете. Пользователь самостоятельно отвечает за законность обработки аудиозаписей и соблюдение требований к персональным данным.

## Лицензия

Исходный код Audiotext распространяется по лицензии MIT. Сторонние библиотеки и модели имеют собственные лицензии; перед распространением бинарной сборки необходимо сохранить их уведомления и выполнить условия соответствующих лицензий.

Copyright © 2026 Mikhail Zuev

---

## English

Audiotext is a free, open-source, multi-worker Windows audio transcription application powered by Faster Whisper and designed for local NVIDIA CUDA processing.

Main features include file/folder queues, drag-and-drop, 1–32 workers, VAD, TXT/SRT/JSON export, mirrored output folders, resumable queues, bilingual UI, portable settings, and in-app model download.

Run from source with `RUN_DEV.bat`; build the portable application with `BUILD_EXE.bat`. The complete output folder under `dist\Audiotext` must be distributed, not the executable alone.

Audiotext source code is licensed under MIT. Third-party libraries and models remain under their respective licenses.
