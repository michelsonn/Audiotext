@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo =============================================================
echo Audiotext - Windows portable EXE build
echo =============================================================

set "BOOTSTRAP_PY="

rem Prefer the already working Audiotext development environment.
if exist ".venv\Scripts\python.exe" set "BOOTSTRAP_PY=.venv\Scripts\python.exe"

rem Otherwise try the Windows Python Launcher.
if not defined BOOTSTRAP_PY (
  where py >nul 2>nul && set "BOOTSTRAP_PY=py"
)

rem Finally try python.exe from PATH.
if not defined BOOTSTRAP_PY (
  where python >nul 2>nul && set "BOOTSTRAP_PY=python"
)

if not defined BOOTSTRAP_PY (
  echo ERROR: Python was not found.
  echo Start RUN_DEV.bat once or install Python 3.11/3.12 x64.
  pause
  exit /b 1
)

if not exist ".buildenv\Scripts\python.exe" (
  echo Creating isolated build environment...
  if /I "%BOOTSTRAP_PY%"=="py" (
    py -3.11 -m venv .buildenv 2>nul || py -3.12 -m venv .buildenv 2>nul || py -m venv .buildenv
  ) else (
    "%BOOTSTRAP_PY%" -m venv .buildenv
  )
  if errorlevel 1 goto :error
)

echo Installing build dependencies...
".buildenv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :error
".buildenv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 goto :error

echo Cleaning previous build...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul

echo Building Audiotext...
".buildenv\Scripts\python.exe" -m PyInstaller Audiotext.spec --clean --noconfirm
if errorlevel 1 goto :error

echo.
echo =============================================================
echo READY: dist\Audiotext\Audiotext.exe
echo Copy the whole dist\Audiotext folder, not only Audiotext.exe.
echo =============================================================
pause
exit /b 0

:error
echo.
echo ERROR: Audiotext EXE build failed.
echo Copy the complete error text from this window for diagnosis.
pause
exit /b 1
