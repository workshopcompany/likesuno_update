# ============================================================
# likesuno - 3단계: YuE2 GGUF 가중치 다운로드 (청크 이어받기)
# 우선순위: q8_0(필수) -> vae-f16(필수) -> sidecars(필수)
#          -> q4_0(옵션) -> vae-f32(옵션)
# -SkipQ4: 옵션 파일 생략 (약 40분 절약)
# ============================================================
param([switch]$SkipQ4, [switch]$Q4Only)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$root = $PSScriptRoot | Split-Path
$base = "https://huggingface.co/audio-cpp/Yue2-3B-GGUF/resolve/main"
$dest = "$root\models\Yue2-3B-GGUF"
$chunked = "$root\scripts\download_chunked.ps1"
New-Item -ItemType Directory -Force -Path "$dest\sidecars" | Out-Null

function Get-Big([string]$file, [string]$out) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $chunked -Url "$base/$file" -Out $out -Conn 8
    if ($LASTEXITCODE -ne 0) { throw "다운로드 실패: $file" }
}

# --- 1) 필수: Q8_0 메인 모델 ---
Get-Big "yue2-3b-q8_0.gguf" "$dest\yue2-3b-q8_0.gguf"

# --- 2) 필수: VAE F16 ---
Get-Big "yue2-vae-f16.gguf" "$dest\yue2-vae-f16.gguf"

# --- 3) 필수: sidecars (작음, 단일 연결) ---
foreach ($s in @("yue2-model-config.json", "yue2-generation-config.json", "yue2-qwen.tiktoken", "yue2-vae-config.json")) {
    $out = "$dest\sidecars\$s"
    if (-not (Test-Path $out) -or (Get-Item $out).Length -lt 1KB) {
        & curl.exe -L --fail --retry 5 -o $out "$base/sidecars/$s"
        if ($LASTEXITCODE -ne 0) { throw "sidecar 실패: $s" }
    }
    Write-Host "[OK] sidecar: $s"
}

# --- 4) 옵션: Q4_0 (OOM 대안) / F32 VAE ---
if (-not $SkipQ4 -or $Q4Only) { Get-Big "yue2-3b-q4_0.gguf" "$dest\yue2-3b-q4_0.gguf" }
if (-not $SkipQ4 -and -not $Q4Only) { Get-Big "yue2-vae-f32.gguf" "$dest\yue2-vae-f32.gguf" }

Write-Host ""
Write-Host "==== 모델 다운로드 전부 완료 ===="
Get-ChildItem $dest -Recurse -File | Format-Table @{n='File';e={$_.FullName.Replace($dest,'...')}}, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}
