@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul

REM ============================================================
REM  likesuno installer  -  double-click to set up this folder
REM  1) checks Python   2) creates venv   3) installs packages
REM  4) optional: build audio.cpp runtime + download models
REM ============================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "VENV_PY=%ROOT%\venv\Scripts\python.exe"
cd /d "%ROOT%"

echo ============================================================
echo  likesuno installer
echo  folder: %ROOT%
echo ============================================================
echo.

REM ---- 1) Python -------------------------------------------------
echo [1/5] checking Python...
where python >nul 2>nul
if errorlevel 1 (
  echo [X] Python not found in PATH.
  echo     Install Python 3.11 x64 from https://www.python.org/downloads/windows/
  echo     Tick "Add python.exe to PATH" during setup, then run this file again.
  pause
  exit /b 1
)
python -c "import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)"
if errorlevel 1 (
  echo [X] Python 3.10 or newer is required ^(3.11 recommended^).
  pause
  exit /b 1
)
echo       ok
echo.

REM ---- 2) virtual environment ------------------------------------
echo [2/5] virtual environment (venv, no torch)...
if exist "%VENV_PY%" (
  echo       already exists - reusing
) else (
  python -m venv "%ROOT%\venv"
  if errorlevel 1 (
    echo [X] failed to create venv
    pause
    exit /b 1
  )
  echo       created
)
echo.

REM ---- 3) python packages ----------------------------------------
echo [3/5] installing packages from requirements.txt ...
"%VENV_PY%" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 (
  echo [X] pip upgrade failed - check your network / proxy
  pause
  exit /b 1
)
"%VENV_PY%" -m pip install -r "%ROOT%\requirements.txt"
if errorlevel 1 (
  echo [X] package install failed - check your network / proxy
  pause
  exit /b 1
)
echo       ok
echo.

REM ---- 4) data folders -------------------------------------------
echo [4/5] preparing data folders...
for %%d in (models output input jobs logs tmp) do if not exist "%ROOT%\%%d" mkdir "%ROOT%\%%d"
if not exist "%ROOT%\my voice" mkdir "%ROOT%\my voice"
echo       ok
echo.

REM ---- 5) optional: runtime build + model download ----------------
echo [5/5] CUDA runtime + models (optional, several GB download)
set "DO_SETUP=N"
set /p DO_SETUP="      Run scripts\setup_all.ps1 now? [y/N]: "
if /i "%DO_SETUP%"=="y" (
  where powershell >nul 2>nul
  if errorlevel 1 (
    echo [X] PowerShell not found. Run scripts\setup_all.ps1 manually.
  ) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\setup_all.ps1"
    if errorlevel 1 echo [!] setup_all.ps1 reported an error - see output above.
  )
) else (
  echo       skipped - run scripts\setup_all.ps1 whenever you are ready
)
echo.

echo ============================================================
echo  Done.
echo.
echo   Run:   JustRun.bat     (opens http://127.0.0.1:8560)
echo   Stop:  stop.bat
echo.
echo  Optional - AI background images ^(SD 3.5 GGUF^):
echo   config\likesuno.json -^> "sd_venv_python": full path to a python.exe
echo   that has torch + diffusers, and "sd_model": path to the .gguf file.
echo   Leave both empty to disable image generation; music, MP4 and
echo   tags still work without them.
echo.
echo  Creator tag: put your own recordings into the "my voice" folder.
echo ============================================================
pause