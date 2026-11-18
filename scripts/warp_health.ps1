# warp_health.ps1 -- WARP proxy-mode health check + automatic reconnect (2026-11-18).
#
# Runs every 5 minutes as the logged-on user (Task Scheduler: JunboWARP). The bot (POLY_PROXY) and the
# forecast runner (POLYMARKET_SOCKS5_PROXY) need WARP in PROXY mode (socks5 127.0.0.1:40000).
# This script NEVER changes the WARP mode. The old start_warp.ps1 ran `warp-cli mode warp` (full VPN),
# which closes the SOCKS port -- it must not be scheduled again.
#
# Probe: https://www.cloudflare.com/cdn-cgi/trace through the proxy must answer warp=on|plus.
# (Polymarket itself is not used as the probe: gamma-api answers HTTP 421 when resolved through the
# machine hosts pin, which is a DNS issue handled in-process by utils/poly_dns.py, not a WARP fault.)
# After $failThreshold consecutive failed ticks: warp-cli disconnect + connect, at most once per
# $cooldownMin minutes. Log: <JUNBO_DATA_DIR>\logs\warp_health.log (only state changes are logged).
$ErrorActionPreference = 'SilentlyContinue'

$cli = 'C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe'
$dataDir = if ($env:JUNBO_DATA_DIR) { $env:JUNBO_DATA_DIR } else { 'C:\JUNBO data' }
$logFile = Join-Path $dataDir 'logs\warp_health.log'
$stateFile = Join-Path $dataDir 'state\warp_health.json'
$proxy = 'socks5h://127.0.0.1:40000'
$probeUrl = 'https://www.cloudflare.com/cdn-cgi/trace'
$failThreshold = 2
$cooldownMin = 15

function Write-HealthLog([string]$msg) {
  try {
    New-Item -ItemType Directory -Force -Path (Split-Path $logFile) | Out-Null
    Add-Content -Path $logFile -Encoding utf8 -Value ((Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + ' ' + $msg)
  } catch { }
}

function Test-WarpProxy {
  $out = (& curl.exe --silent --max-time 10 --proxy $proxy $probeUrl 2>$null | Out-String)
  return ($out -match '(?m)^warp=(on|plus)\s*$')
}

$fails = 0
$lastReconnect = ''
try {
  $j = Get-Content -Raw -Path $stateFile | ConvertFrom-Json
  $fails = [int]$j.fails
  $lastReconnect = [string]$j.lastReconnect
} catch { }

$ok = Test-WarpProxy
if (-not $ok) { Start-Sleep -Seconds 5; $ok = Test-WarpProxy }

if ($ok) {
  if ($fails -gt 0) { Write-HealthLog "OK proxy recovered after $fails failed tick(s)" }
  $fails = 0
} else {
  $fails += 1
  $status = ((& $cli status 2>&1 | Out-String).Trim() -replace '\s+', ' ')
  Write-HealthLog "FAIL #$fails proxy probe failed; warp-cli: $status"
  $cooled = $true
  if ($lastReconnect) {
    try { $cooled = ((Get-Date) - [datetime]::Parse($lastReconnect)).TotalMinutes -ge $cooldownMin } catch { $cooled = $true }
  }
  if ($fails -ge $failThreshold -and $cooled) {
    Write-HealthLog 'RECONNECT warp-cli disconnect + connect (mode unchanged)'
    & $cli disconnect 2>&1 | Out-Null
    Start-Sleep -Seconds 3
    & $cli connect 2>&1 | Out-Null
    Start-Sleep -Seconds 8
    $lastReconnect = (Get-Date).ToString('o')
    if (Test-WarpProxy) {
      Write-HealthLog 'RECONNECT ok (proxy probe passes)'
      $fails = 0
    } else {
      Write-HealthLog 'RECONNECT done, probe still failing (next attempt after cooldown)'
    }
  }
}

try {
  New-Item -ItemType Directory -Force -Path (Split-Path $stateFile) | Out-Null
  (@{ fails = $fails; lastReconnect = $lastReconnect } | ConvertTo-Json -Compress) | Set-Content -Path $stateFile -Encoding utf8
} catch { }
