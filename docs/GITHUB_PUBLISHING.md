# Публикация Audiotext на GitHub

## 1. Проверка подготовленной папки

В корне должны быть `README.md`, `LICENSE`, `.gitignore`, исходники, документация и файлы сборки.

Не должно быть:

```text
.venv
.buildenv
models
data
build
dist
__pycache__
реальных аудиозаписей
пользовательских расшифровок
```

## 2. Создание репозитория

На GitHub создайте пустой публичный репозиторий `Audiotext`.

Не добавляйте через сайт второй README, LICENSE или `.gitignore`, потому что они уже есть в архиве.

## 3. Самый простой вариант: GitHub Desktop

1. Установите GitHub Desktop и войдите в аккаунт.
2. Выберите **File → Add local repository**.
3. Укажите распакованную папку Audiotext.
4. Если Git-репозиторий ещё не создан, GitHub Desktop предложит создать его.
5. Сделайте первый commit, например `Initial open-source release`.
6. Нажмите **Publish repository**.
7. Снимите флажок приватного репозитория.

## 4. Вариант через PowerShell

В распакованной папке:

```powershell
git init
git add .
git commit -m "Initial open-source release"
git branch -M main
git remote add origin <АДРЕС_ВАШЕГО_РЕПОЗИТОРИЯ>
git push -u origin main
```

## 5. Сборка релиза

```bat
BUILD_EXE.bat
```

После успешной проверки упакуйте всю папку:

```text
dist\Audiotext
```

в:

```text
Audiotext-1.0.6-windows-x64.zip
```

## 6. GitHub Release

1. Откройте **Releases → Draft a new release**.
2. Создайте тег `v1.0.6`.
3. Название: `Audiotext 1.0.6`.
4. Вставьте текст из `.github/RELEASE_TEMPLATE.md` и отредактируйте его.
5. Прикрепите portable ZIP.
6. Отметьте релиз как pre-release, пока он не проверен на нескольких компьютерах.

## 7. После публикации

- добавьте 1–2 скриншота в README;
- включите GitHub Issues;
- создавайте отдельный тег для каждой публичной версии;
- не заменяйте незаметно уже опубликованный ZIP: выпускайте новую версию;
- не коммитьте скачанные модели и пользовательские данные.
