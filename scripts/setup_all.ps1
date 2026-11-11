# ============================================================
# likesuno - 전체 설치 (관리자 PowerShell 권장)
# 1) 도구체인(VS BuildTools + CUDA 12.9) 2) audio.cpp sm_61 빌드
# 3) YuE2 GGUF 가중치 다운로드  4) 백엔드 venv
# ============================================================
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot | Split-Path

Write-Host "==== [1/4] 도구체인 설치 ====" -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$root\scripts\install_toolchain.ps1"
if ($LASTEXITCODE -ne 0) { throw "도구체인 설치 실패" }

Write-Host "==== [2/4] audio.cpp 빌드 (sm_61) ====" -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$root\scripts\build_runtime.ps1"
if ($LASTEXITCODE -ne 0) { throw "빌드 실패" }

Write-Host "==== [3/4] 모델 다운로드 ====" -ForegroundColor Cyan
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$root\scripts\download_models.ps1" -SkipQ4
if ($LASTEXITCODE -ne 0) { throw "모델 다운로드 실패" }

Write-Host "==== [4/4] 백엔드 venv (torch 없음, 격리 원칙) ====" -ForegroundColor Cyan
python -m venv "$root\venv"
& "$root\venv\Scripts\python.exe" -m pip install --upgrade pip -q
& "$root\venv\Scripts\pip.exe" install -q -r "$root\requirements.txt"
& "$root\venv\Scripts\python.exe" -c "import fastapi, uvicorn; print('fastapi OK')"

Write-Host ""
Write-Host "[전체 설치 완료] 실행: .\start_likesuno.ps1" -ForegroundColor Green
