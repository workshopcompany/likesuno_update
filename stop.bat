@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "CFG=%ROOT%\config\likesuno.json"
set "PIDFILE=%ROOT%\tmp\likesuno.pid"

rem --- 포트 읽기 ---
set "PORT=8560"
powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-Content '%CFG%' -Raw | ConvertFrom-Json).port" > "%TEMP%\lk_port.txt" 2>nul
set /p PORT=<"%TEMP%\lk_port.txt" 2>nul
del "%TEMP%\lk_port.txt" 2>nul
if not defined PORT set PORT=8560

set "KILLED=0"

rem --- 1) PID 파일에 적힌 프로세스 종료 ---
set "PID="
if exist "%PIDFILE%" set /p PID=<"%PIDFILE%"
if defined PID (
    taskkill /F /PID !PID! >nul 2>&1
    if !errorlevel! equ 0 (
        echo [1] PID 파일 기준 종료 - PID !PID!
        set "KILLED=1"
    )
)

rem --- 2) 포트를 잡고 있는 프로세스 종료 (PID 파일이 틀려도 확실하게) ---
for /f "tokens=5" %%P in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":%PORT% "') do (
    taskkill /F /PID %%P >nul 2>&1
    if !errorlevel! equ 0 (
        echo [2] 포트 %PORT% 사용 프로세스 종료 - PID %%P
        set "KILLED=1"
    )
)

del "%PIDFILE%" 2>nul

if "!KILLED!"=="1" (
    echo.
    echo likesuno 서버를 종료했습니다.
) else (
    echo.
    echo [i] 꺼야 할 서버가 없습니다. - 이미 꺼져 있거나 실행한 적이 없음
)
echo.
pause
