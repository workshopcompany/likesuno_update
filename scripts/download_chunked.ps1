# ============================================================
# likesuno - 청크 이어받기 다운로더 (느린 인터넷 대응)
# - 파일을 N조각으로 나눠 동시 다운로드 (기본 8연결)
# - 끊기면 받은 만큼 보존하고 이어받기 (부분 파일 재다운로드 없음)
# - 진행률 10초마다 스트리밍 출력
# - 타임아웃 없음, 정지 감지(60초간 1KB/s 미만) 시 자동 재시도
# 사용: .\download_chunked.ps1 -Url <url> -Out <path> [-Conn 8]
# ============================================================
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Out,
    [int]$Conn = 8
)
$ProgressPreference = "SilentlyContinue"

function Get-RemoteSize([string]$u) {
    $hdr = & curl.exe -sIL $u 2>$null
    $len = $null
    foreach ($line in $hdr) {
        if ($line -match '^content-length:\s*(\d+)') { $len = [int64]$Matches[1] }
    }
    return $len
}

function Get-StreamBytes {
    param($Src, $DstFs)
    $src = [IO.File]::OpenRead($Src)
    try { $src.CopyTo($DstFs) } finally { $src.Close() }
}

$total = Get-RemoteSize $Url
if (-not $total -or $total -lt 1MB) { throw "원격 크기 확인 실패: $Url" }

# 이미 완료된 파일이면 스킵
if ((Test-Path $Out) -and (Get-Item $Out).Length -eq $total) {
    Write-Host "[SKIP] $(Split-Path $Out -Leaf) 완료 ($([math]::Round($total/1MB,1)) MB)"
    exit 0
}

$partsDir = "$Out.parts"
New-Item -ItemType Directory -Force -Path $partsDir | Out-Null

$chunk = [math]::Ceiling($total / $Conn)
$ranges = @()
for ($i = 0; $i -lt $Conn; $i++) {
    $s = [int64]($i * $chunk)
    $e = [int64]([math]::Min($s + $chunk - 1, $total - 1))
    if ($s -gt $e) { break }
    $ranges += [pscustomobject]@{ Id = $i; Start = $s; End = $e; Size = $e - $s + 1 }
}

function Get-Done([pscustomobject]$r) {
    $p = "$partsDir\part$($r.Id)"
    if (Test-Path $p) { return (Get-Item $p).Length } else { return [int64]0 }
}

$attempt = 0
while ($true) {
    $attempt++
    $todo = @()
    foreach ($r in $ranges) {
        $done = Get-Done $r
        if ($done -lt $r.Size) { $todo += $r }
    }
    if (-not $todo) { break }

    # 미완료 조각 병렬 다운로드
    $procs = @()
    foreach ($r in $todo) {
        $done = Get-Done $r
        $from = $r.Start + $done
        $to = $r.End
        $tmp = "$partsDir\tmp$($r.Id)"
        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
        $p = Start-Process -FilePath "curl.exe" -PassThru -WindowStyle Hidden -ArgumentList @(
            "-L", "--fail", "--silent", "--show-error",
            "--connect-timeout", "30",
            "--speed-limit", "1024", "--speed-time", "60",
            "--retry", "3", "--retry-delay", "2",
            "-r", "$from-$to", "-o", $tmp, $Url
        )
        $procs += [pscustomobject]@{ R = $r; P = $p; Base = $done }
    }

    # 스트리밍 진행률 표시 (10초마다)
    while ($true) {
        Start-Sleep -Seconds 10
        $alive = 0
        $bytes = [int64]0
        foreach ($x in $ranges) {
            $bytes += (Get-Done $x)
            $t = "$partsDir\tmp$($x.Id)"
            if (Test-Path $t) { $bytes += (Get-Item $t).Length }
        }
        foreach ($x in $procs) { if (-not $x.P.HasExited) { $alive++ } }
        $pct = [math]::Round(100.0 * $bytes / $total, 1)
        Write-Host ("[{0}] {1}%  {2} / {3} MB  (running {4})" -f (Get-Date -Format 'HH:mm:ss'), $pct, [math]::Round($bytes / 1MB, 1), [math]::Round($total / 1MB, 1), $alive)
        if ($alive -eq 0) { break }
    }

    # 받은 조각 반영
    foreach ($x in $procs) {
        $tmp = "$partsDir\tmp$($x.R.Id)"
        $part = "$partsDir\part$($x.R.Id)"
        if (Test-Path $tmp) {
            $tsize = (Get-Item $tmp).Length
            if ($tsize -gt 0) {
                $fs = [IO.File]::Open($part, [IO.FileMode]::Append)
                try { Get-StreamBytes $tmp $fs } finally { $fs.Close() }
            }
            Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        }
    }
    Write-Host "[재시도 #$attempt] 미완료 조각 이어받기..."
}

# 조각 합치기
Write-Host "[조합] 부분 파일 병합 중..."
$outFs = [IO.File]::Create($Out)
try {
    foreach ($r in $ranges) {
        $src = [IO.File]::OpenRead("$partsDir\part$($r.Id)")
        try { $src.CopyTo($outFs) } finally { $src.Close() }
    }
} finally { $outFs.Close() }

$final = (Get-Item $Out).Length
if ($final -ne $total) { throw "크기 불일치: $final / $total" }
Remove-Item $partsDir -Recurse -Force
Write-Host "[완료] $(Split-Path $Out -Leaf) = $([math]::Round($total/1MB,1)) MB"
