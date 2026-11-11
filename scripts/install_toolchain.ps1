# ============================================================
# likesuno - 1단계: 빌드 도구체인 설치 (자동 관리자 승격 + 로그)
#
# 주의 (torch venv 와 완전 분리):
#  - 이 설치는 C++ 빌드 전용. torch/venv 와 무관하고 PATH 공유 없음
#  - CUDA 13.x 는 Pascal(sm_61) 지원 제거 -> 12.9 고정
#  - nvcc 는 빌드 스크립트 안에서 CUDAToolkit_ROOT 로만 참조
# ============================================================
param([switch]$NoElevate)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot | Split-Path
$Log = Join-Path $root "logs\toolchain_install.log"

# --- 자동 관리자 승격 ---
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $NoElevate) {
    Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", $PSCommandPath, "-NoElevate"
    )
    exit $LASTEXITCODE
}

Start-Transcript -Path $Log -Force

try {
    $vsInstaller = "D:\VS2022BuildTools"
    $downloads   = Join-Path $root "downloads"
    New-Item -ItemType Directory -Force -Path $downloads | Out-Null

    # ---------- 1) Visual Studio 2022 Build Tools ----------
    if (-not (Test-Path "$vsInstaller\VC\Tools\MSVC")) {
        Write-Host "== VS 2022 Build Tools 설치 (다운로드 수 GB, 수십 분) =="
        $vsExe = Join-Path $downloads "vs_BuildTools.exe"
        Invoke-WebRequest -Uri "https://aka.ms/vs/17/release/vs_BuildTools.exe" -OutFile $vsExe
        $proc = Start-Process -FilePath $vsExe -Wait -PassThru -ArgumentList @(
            "--quiet", "--norestart", "--nocache",
            "--installPath", $vsInstaller,
            "--add", "Microsoft.VisualStudio.Workload.VCTools",
            "--add", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "--add", "Microsoft.VisualStudio.Component.VC.CMake.Project",
            "--add", "Microsoft.VisualStudio.Component.Windows11SDK.26100",
            "--includeRecommended"
        )
        if ($proc.ExitCode -ne 0) { throw "VS Build Tools 설치 실패 (코드 $($proc.ExitCode))" }
    }
    if (-not (Test-Path "$vsInstaller\VC\Tools\MSVC")) { throw "VS 설치 후에도 MSVC 를 찾지 못했습니다." }
    Write-Host "[OK] VS Build Tools: $vsInstaller"

    # ---------- 2) CUDA Toolkit 12.9 ----------
    $cudaDest = "D:\CUDA\v12.9"
    $haveNvcc = $false
    foreach ($c in @($cudaDest, $env:CUDA_PATH) + (Get-ChildItem "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA" -Directory -ErrorAction SilentlyContinue | ForEach-Object FullName)) {
        if ($c -and (Test-Path "$c\bin\nvcc.exe")) { $haveNvcc = $true; $cudaDest = $c; break }
    }
    if (-not $haveNvcc) {
        Write-Host "== CUDA Toolkit 12.9 (network installer) 다운로드/설치 =="
        $cudaExe = Join-Path $downloads "cuda_12.9.0_windows_network.exe"
        if (-not (Test-Path $cudaExe)) {
            Invoke-WebRequest -Uri "https://developer.download.nvidia.com/compute/cuda/12.9.0/network_installers/cuda_12.9.0_windows_network.exe" -OutFile $cudaExe
        }
        # ggml 빌드에 필요한 최소 구성: nvcc + cudart + cublas(+Lt) + curand
        $proc = Start-Process -FilePath $cudaExe -Wait -PassThru -ArgumentList @(
            "-s", "nvcc_12.9", "cudart_12.9", "cublas_12.9", "cublasLt_12.9", "curand_12.9",
            "--toolkitpath", "D:\CUDA\v12.9"
        )
        if ($proc.ExitCode -ne 0 -or -not (Test-Path "D:\CUDA\v12.9\bin\nvcc.exe")) {
            Write-Host "최소 구성 설치 실패/경로 이탈 -> 전체 toolkit 무인 설치로 재시도"
            $proc = Start-Process -FilePath $cudaExe -Wait -PassThru -ArgumentList @("-s")
            if ($proc.ExitCode -ne 0) { throw "CUDA 설치 실패 (코드 $($proc.ExitCode))" }
        }
    }
    $nvcc = @("D:\CUDA\v12.9", $env:CUDA_PATH) + (Get-ChildItem "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA" -Directory -ErrorAction SilentlyContinue | ForEach-Object FullName) |
        Where-Object { $_ -and (Test-Path "$_\bin\nvcc.exe") } | Select-Object -First 1
    if (-not $nvcc) { throw "nvcc.exe 를 찾지 못했습니다." }
    Write-Host "[OK] CUDA Toolkit: $nvcc (sm_61 지원)"

    Write-Host ""
    Write-Host "==== 도구체인 설치 완료 ====" -ForegroundColor Green
} finally {
    Stop-Transcript
}
