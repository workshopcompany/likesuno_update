# ============================================================
# likesuno - YuE2 first generation smoke test
# Output: <repo>\output\test01.wav  (+ log to stdout)
# Run detached via WMI so the terminal cannot kill it.
# ============================================================
param(
    [double]$Duration = 20,
    [int]$Seed = 1234,
    [string]$Out = ""
)
$root = $PSScriptRoot | Split-Path
if (-not $Out) { $Out = Join-Path $root "output\test01.wav" }

$ErrorActionPreference = "Continue"

$bin   = Join-Path $root "runtime\audio.cpp\build\windows-cuda-release\bin\audiocpp_cli.exe"
$model = Join-Path $root "models\Yue2-3B-GGUF"

# CUDA runtime DLLs live here; the exe folder has no CUDA dlls of its own.
# CUDA_PATH(또는 CUDA_HOME) 가 잡혀 있으면 그 경로를 앞에 둔다.
foreach ($c in @($env:CUDA_PATH, $env:CUDA_HOME, "D:\CUDA\v12.9", "D:\CUDA\v12.8")) {
    if ($c -and (Test-Path "$c\bin")) { $env:PATH = "$c\bin;$c\lib;" + $env:PATH; break }
}

New-Item -ItemType Directory -Force -Path (Split-Path $Out) | Out-Null

$lyrics = "[Verse]`nSoft morning light is touching the window.`nI hear the city waking below.`n[Chorus]`nStay with the rhythm, let it carry us home.`nSing with the sunrise, we are never alone."

$style = "English, indie pop, bright acoustic guitar, soft drums, warm lead vocal, polished demo mix"

Write-Host "=== YuE2 smoke test ==="
Write-Host "binary  : $bin"
Write-Host "model   : $model"
Write-Host "out     : $Out"
Write-Host "seed    : $Seed"
Write-Host "duration: $Duration sec"
Write-Host ""

& $bin `
    --task gen `
    --family yue2 `
    --model $model `
    --backend cuda `
    --threads 8 `
    --lyrics $lyrics `
    --request-option "style=$style" `
    --request-option "cot=off" `
    --seed $Seed `
    --duration-seconds $Duration `
    --out $Out `
    --log

$code = $LASTEXITCODE
Write-Host ""
Write-Host "exit_code=$code"

if (Test-Path $Out) {
    $f = Get-Item $Out
    Write-Host "OUTPUT_OK: $($f.FullName) = $([math]::Round($f.Length/1MB,2)) MB"
} else {
    Write-Host "OUTPUT_MISSING"
}
