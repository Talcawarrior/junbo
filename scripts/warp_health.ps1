# warp_health.ps1 -- WARP proxy-mode health check + automatic reconnect (2026-11-18; gamma-api probe 2026-11-26).
#
# Runs every 5 minutes as the logged-on user (Task Scheduler: JunboWARP). The bot (POLY_PROXY) and the
# forecast runner (POLYMARKET_SOCKS5_PROXY) need WARP in PROXY mode (socks5 127.0.0.1:40000).
# This script NEVER changes the WARP mode. The old start_warp.ps1 ran `warp-cli mode warp` (full VPN),
# which closes the SOCKS port -- it must not be scheduled again.
#
# A tick passes only when BOTH probes pass through the proxy:
#  1. https://www.cloudflare.com/cdn-cgi/trace answers warp=on|plus.
#  2. gamma-api.polymarket.com (CloudFront) answers with any HTTP status. On 2026-11-25 WARP stopped
#     reaching non-Cloudflare hosts (CloudFront, Google, Quad9: "Host unreachable") while probe 1 kept
#     passing, so the bot could not reach gamma-api and no reconnect happened. The address comes from
#     Cloudflare DoH (1.1.1.1) and is pinned with curl --resolve over socks5:// -- the path
#     utils/poly_dns.py gives the bot. Any other resolution (system resolver, or WARP itself via
#     socks5h://) follows the machine hosts pin to a Cloudflare edge that answers HTTP 421 and stays
#     reachable in that failure mode. Any HTTP status counts as reachable: a Polymarket-side error is
#     not a WARP fault and a reconnect would not fix it.
# After $failThreshold consecutive failed ticks: warp-cli disconnect + connect, at most once per
# $cooldownMin minutes. Worst case about 2 minutes (task limit PT5M). Log:
# <JUNBO_DATA_DIR>\logs\warp_health.log (only state changes are logged).
$ErrorActionPreference = 'SilentlyContinue'

$cli = 'C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe'
$dataDir = if ($env:JUNBO_DATA_DIR) { $env:JUNBO_DATA_DIR } else { 'C:\JUNBO data' }
$logFile = Join-Path $dataDir 'logs\warp_health.log'
$stateFile = Join-Path $dataDir 'state\warp_health.json'
$proxy = 'socks5h://127.0.0.1:40000'
$proxyLocalDns = 'socks5://127.0.0.1:40000'
$probeUrl = 'https://www.cloudflare.com/cdn-cgi/trace'
$gammaHost = 'gamma-api.polymarket.com'
$gammaUrl = 'https://gamma-api.polymarket.com/markets?limit=1'
$dohUrl = 'https://1.1.1.1/dns-query?name=gamma-api.polymarket.com&type=A'
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

function Get-GammaIps {
  # A records of gamma-api from Cloudflare DoH through the proxy (same lookup as utils/poly_dns.py).
  $raw = (& curl.exe --silent --max-time 8 --proxy $proxy -H 'accept: application/dns-json' $dohUrl 2>$null | Out-String)
  try {
    return @(($raw | ConvertFrom-Json).Answer | Where-Object { $_.type -eq 1 -and $_.data } | ForEach-Object { [string]$_.data })
  } catch { return @() }
}

function Test-GammaProxy {
  # '' when gamma-api (CloudFront) answers through the proxy, otherwise the failure reason for the log.
  $ips = @(Get-GammaIps)
  if ($ips.Count -eq 0) { return 'gamma-api DoH lookup through proxy failed' }
  foreach ($ip in ($ips | Select-Object -First 2)) {
    $code = (& curl.exe --silent --max-time 10 --proxy $proxyLocalDns --resolve "${gammaHost}:443:$ip" -o NUL -w '%{http_code}' $gammaUrl 2>$null | Out-String).Trim()
    if ($code -match '^[1-5][0-9][0-9]$') { return '' }
  }
  return 'gamma-api (CloudFront) unreachable through proxy'
}

function Get-ProbeFailure {
  # '' when both probes pass, otherwise the first failure reason.
  if (-not (Test-WarpProxy)) { return 'cloudflare trace probe failed' }
  return (Test-GammaProxy)
}

$fails = 0
$lastReconnect = ''
try {
  $j = Get-Content -Raw -Path $stateFile | ConvertFrom-Json
  $fails = [int]$j.fails
  $lastReconnect = [string]$j.lastReconnect
} catch { }

$why = Get-ProbeFailure
if ($why) { Start-Sleep -Seconds 5; $why = Get-ProbeFailure }

if (-not $why) {
  if ($fails -gt 0) { Write-HealthLog "OK proxy recovered after $fails failed tick(s)" }
  $fails = 0
} else {
  $fails += 1
  $status = ((& $cli status 2>&1 | Out-String).Trim() -replace '\s+', ' ')
  Write-HealthLog "FAIL #$fails $why; warp-cli: $status"
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
    $why = Get-ProbeFailure
    if (-not $why) {
      Write-HealthLog 'RECONNECT ok (both probes pass)'
      $fails = 0
    } else {
      Write-HealthLog "RECONNECT done, still failing: $why (next attempt after cooldown)"
    }
  }
}

try {
  New-Item -ItemType Directory -Force -Path (Split-Path $stateFile) | Out-Null
  (@{ fails = $fails; lastReconnect = $lastReconnect } | ConvertTo-Json -Compress) | Set-Content -Path $stateFile -Encoding utf8
} catch { }
