# run-loop.ps1 - TempMarket Edge Runner restart wrapper.
# Runs start-runner.ps1 (which loads user-env provider keys and starts node index.mjs).
#   - node exits 0  -> clean shutdown or duplicate-instance (port busy): stop, do NOT restart.
#   - node exits !=0 -> crash: log and restart after a short backoff (grows after repeated crashes).
# The scheduled task points here so an unforeseen crash never leaves the runner down until the
# next logon (the 2026-09-06 incident stayed down ~12h because the task's LogonTrigger fires once).
$ErrorActionPreference = 'SilentlyContinue'
$runnerPs1 = Join-Path $PSScriptRoot 'start-runner.ps1'
$psExe = (Get-Command powershell.exe -ErrorAction Stop).Source
$consecutive = 0

while ($true) {
  # Zaten calisan runner varsa yeni node baslatma (port 8787 veya index.mjs process).
  $existing = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
  if (-not $existing) {
    $existing = @(Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -and $_.CommandLine -like '*index.mjs*' })
  }
  if ($existing) {
    Start-Sleep -Seconds 60
    continue
  }

  $db = if ($env:TEMP_MARKET_DB) { $env:TEMP_MARKET_DB } else { 'C:\JUNBO data\tempmarket-edge.sqlite' }
  if (-not (Test-Path $db)) {
    Write-Output "[run-loop] DB missing: $db - exiting; watchdog will retry later"
    break
  }

  $ts = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
  Write-Output "[run-loop] $ts starting runner (attempt after $consecutive consecutive failures)"
  $child = Start-Process -FilePath $psExe -ArgumentList @(
    '-NoLogo','-NoProfile','-NonInteractive','-WindowStyle','Hidden',
    '-ExecutionPolicy','Bypass','-File',"`"$runnerPs1`""
  ) -WindowStyle Hidden -Wait -PassThru
  $code = $child.ExitCode
  $ts = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
  Write-Output "[run-loop] $ts runner exited code $code"
  if ($code -eq 0) { Write-Output '[run-loop] clean exit (0) - not restarting'; break }

  $consecutive += 1
  $sleepSec = if ($consecutive -ge 5) { 60 } else { 15 }
  Write-Output "[run-loop] crash (exit $code) - restarting in ${sleepSec}s"
  Start-Sleep -Seconds $sleepSec
}
