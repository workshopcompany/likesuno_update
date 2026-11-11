# ============================================================
# likesuno - 웹앱 실행 (torch 없는 격리 venv 사용)
# 저장소 위치와 무관하게 동작합니다 (스크립트 기준 상대경로)
# ============================================================
$ErrorActionPreference = "Stop"
$root = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$venvPy = "$root\venv\Scripts\python.exe"

if (-not (Test-Path $venvPy)) {
    Write-Host "venv 없음 -> 생성 (fastapi/uvicorn 만 설치, torch 없음)"
    python -m venv "$root\venv"
    & $venvPy -m pip install -q --upgrade pip
    & "$root\venv\Scripts\pip.exe" install -q -r "$root\requirements.txt"
}

$cfg = Get-Content "$root\config\likesuno.json" -Raw | ConvertFrom-Json
$port = $cfg.port

Write-Host "likesuno 서버 시작: http://127.0.0.1:$port"
Start-Process "http://127.0.0.1:$port"
& $venvPy -m uvicorn likesuno.app:app --host $cfg.host --port $port --app-dir "$root\src"
