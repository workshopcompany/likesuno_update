# ============================================================
# likesuno - 2단계: audio.cpp 빌드 (GTX 1080 Ti sm_61 용)
# prebuilt 바이너리는 CUDA 13 (CC 7.5+) 만 지원하므로 직접 빌드
# ============================================================
$ErrorActionPreference = "Stop"

$root      = $PSScriptRoot | Split-Path
$repo      = Join-Path $root "runtime\audio.cpp"
$vsInstall = if ($env:VSINSTALLDIR) { $env:VSINSTALLDIR } else { "D:\VS2022BuildTools" }

# CUDA Toolkit 위치 자동 탐색 (D: 격리 우선, Program Files 차선)
$cudaRoot = $env:CUDA_PATH
if (-not $cudaRoot -or -not (Test-Path "$cudaRoot\bin\nvcc.exe")) {
    $cudaRoot = @(
        "D:\CUDA\v12.9",
        (Get-ChildItem "D:\CUDA" -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 1 -ExpandProperty FullName)
    ) + (Get-ChildItem "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA" -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | ForEach-Object FullName) |
        Where-Object { $_ -and (Test-Path "$_\bin\nvcc.exe") } | Select-Object -First 1
}
if (-not $cudaRoot) { throw "CUDA Toolkit(nvcc)을 찾지 못했습니다. install_toolchain.ps1 을 먼저 실행하세요." }
Write-Host "CUDA root : $cudaRoot"
Write-Host "VS root   : $vsInstall"
if (-not (Test-Path "$vsInstall\VC\Tools\MSVC")) { throw "VS Build Tools 가 없습니다: $vsInstall" }

# --- nvcc 실행 환경 (conda 유래 nvcc 는 cudafe++/cicc 를 PATH 에서 찾음) ---
$env:CUDA_PATH = $cudaRoot
$env:CUDAToolkit_ROOT = $cudaRoot
$env:PATH = "$cudaRoot\bin;$cudaRoot\nvvm\bin;" + $env:PATH

# --- 빌드 (Pascal sm_61 명시 지정이 핵심) ---
# -CudaArchitectures 61-real : 1080 Ti 네이티브 커널만 빌드해 빌드 시간 최소화
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$repo\scripts\build_windows.ps1" `
    -Preset "windows-cuda-release" `
    -Target "audiocpp_cli" `
    -CudaArchitectures "61-real" `
    -CpuArch "avx2" `
    -VsInstall $vsInstall

if ($LASTEXITCODE -ne 0) { throw "audio.cpp CLI 빌드 실패" }

# 서버/WebUI 도 함께 빌드
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$repo\scripts\build_windows.ps1" `
    -Preset "windows-cuda-release" `
    -Target "audiocpp_server" `
    -CudaArchitectures "61-real" `
    -CpuArch "avx2" `
    -VsInstall $vsInstall

if ($LASTEXITCODE -ne 0) { throw "audio.cpp server 빌드 실패" }

# --- 산출물 확인 ---
$bin = Get-ChildItem "$repo\build" -Recurse -Filter "audiocpp_cli.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $bin) { throw "audiocpp_cli.exe 를 찾지 못했습니다." }
Write-Host ""
Write-Host "[완료] 빌드 결과: $($bin.DirectoryName)"
Get-ChildItem $bin.DirectoryName -Filter "audiocpp_*.exe" | Format-Table Name, Length

# 빌드 디렉토리 경로를 설정에 반영할 수 있게 기록
$binDir = $bin.DirectoryName
@"
BIN_DIR=$binDir
"@ | Set-Content (Join-Path $root "config\build_info.txt")
Write-Host "다음 단계: .\scripts\download_models.ps1"
