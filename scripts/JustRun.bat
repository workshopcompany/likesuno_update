@echo off
setlocal
chcp 65001 >nul

set "ROOT=%~dp0.."
set "VENV_PY=%ROOT%\venv\Scripts\python.exe"
set "APP_DIR=%ROOT%\src"
set "CFG=%ROOT%\config\likesuno.json"

if not exist "%VENV_PY%" (
    echo [!] venv 없음 - README 의 "설치 순서" 를 참고하세요
    pause
    exit /b 1
)

if not exist "%CFG%" (
    echo [!] 설정 없음: %CFG%
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-Content '%CFG%' -Raw | ConvertFrom-Json).port" > "%TEMP%\lk_port.txt" 2>nul
set /p PORT=<"%TEMP%\lk_port.txt" 2>nul
del "%TEMP%\lk_port.txt" 2>nul
if not defined PORT set PORT=8560

rem 이미 실행 중이면 브라우저만 열고 종료
netstat -ano | findstr "LISTENING" | findstr /C:":%PORT% " >nul
if not errorlevel 1 (
    echo.
    echo [i] 서버가 이미 실행 중입니다 - 포트 %PORT%
    echo     브라우저만 엽니다. 새 버전으로 다시 시작하려면:
    echo     scripts\やめる.bat 실행 후 JustRun.bat 을 다시 실행하세요.
    start "" "http://127.0.0.1:%PORT%"
    ping -n 6 127.0.0.1 >nul
    exit /b 0
)

echo.
echo ================================
echo  likesuno 시작
echo  http://127.0.0.1:%PORT%
echo ================================
echo.
echo 서버는 창 없이 백그라운드에서 실행됩니다.
echo.

rem --- 서버를 숨김 창으로 실행 (로그: logs\server.log, logs\server.err.log) ---
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%VENV_PY%' -ArgumentList @('-m','uvicorn','likesuno.app:app','--host','127.0.0.1','--port','%PORT%','--app-dir','%APP_DIR%','--reload') -WorkingDirectory '%APP_DIR%' -WindowStyle Hidden -RedirectStandardOutput '%ROOT%\logs\server.log' -RedirectStandardError '%ROOT%\logs\server.err.log'"

rem --- 서버 준비 대기 (최대 15초) ---
powershell -NoProfile -Command "$u='http://127.0.0.1:%PORT%/api/health'; for($i=0; $i -lt 30; $i++){ try { Invoke-WebRequest $u -UseBasicParsing -TimeoutSec 1 | Out-Null; exit 0 } catch { Start-Sleep -Milliseconds 500 } }; exit 1"
if errorlevel 1 (
    echo.
    echo [!] 서버 응답이 없습니다. 시작에 실패했을 수 있습니다.
    echo     logs\server.err.log 를 확인하거나 start_likesuno.ps1 로 실행해보세요.
    pause
    exit /b 1
)

start "" "http://127.0.0.1:%PORT%"
echo.
echo 시작 완료! 종료는 scripts\やめる.bat 을 실행하세요.
ping -n 3 127.0.0.1 >nul
exit /b 0
