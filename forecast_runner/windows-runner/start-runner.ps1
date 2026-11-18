# Junbo embedded HEAT runner launcher.
# Starts the vendored Heat Node runner from junbo/heat_runner with all data
# paths rooted at C:\JUNBO data. Provider keys come from the user environment,
# falling back to the Junbo .env file. Values are never printed.
$ErrorActionPreference = 'Stop'

$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

$secretNames = @('VISUAL_CROSSING_API_KEY', 'WEATHERAPI_KEY', 'OPENWEATHERMAP_API_KEY')
foreach ($name in $secretNames) {
  $value = [Environment]::GetEnvironmentVariable($name, 'User')
  if (-not $value) { $value = [Environment]::GetEnvironmentVariable($name, 'Machine') }
  if (-not $value) {
    $envFile = Join-Path (Split-Path $root -Parent) '.env'
    if (Test-Path $envFile) {
      foreach ($line in Get-Content $envFile -Encoding UTF8) {
        if ($line -match ('^\s*' + [regex]::Escape($name) + '\s*=\s*(.+?)\s*$')) { $value = $Matches[1]; break }
      }
    }
  }
  if ($value) { Set-Item -Path "Env:$name" -Value $value }
}

$env:TEMP_MARKET_DATA_DIR = 'C:\JUNBO data'
$env:TEMP_MARKET_DB = 'C:\JUNBO data\tempmarket-edge.sqlite'
$env:HEAT_LIVE_DB = 'C:\JUNBO data\tempmarket-edge.sqlite'
$env:HEAT_MAIN_DB = 'C:\JUNBO data\tempmarket-main.sqlite'
$env:KARDESLER_DB = 'C:\JUNBO data\kardesler.db'
$env:OM_CACHE_DB = 'C:\JUNBO data\ortak\db\om_cache.sqlite'
$env:HEAT_DAILY_BLOCK_STATE = 'C:\JUNBO data\state\last_daily_block_utc.txt'
$env:HEAT_UNMATCHED_JSON = 'C:\JUNBO data\state\unmatched_cities.json'
$env:HEAT_ONERI_JSON = 'C:\JUNBO data\state\sehir_onerileri.json'
$env:HEAT_BACKUP_DIR = 'C:\JUNBO data\backups'
$env:HEAT_MODELS_DIR = 'C:\JUNBO data\models'
$env:TEMP_MARKET_SETTLEMENT_INBOX = 'C:\JUNBO data\inbox\settlements.jsonl'
$env:HEAT_CATALOG_DIR = 'C:\JUNBO data\catalog'
$env:TEMP_MARKET_CONFIG = Join-Path $root 'windows-runner\config.json'
$env:POLYMARKET_SOCKS5_PROXY = 'socks5h://127.0.0.1:40000'

$logDir = Join-Path $root 'windows-runner\logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$logFile = Join-Path $logDir 'runner.log'
if ((Test-Path $logFile) -and ((Get-Item $logFile).Length -gt 10MB)) {
  Move-Item -Path $logFile -Destination (Join-Path $logDir 'runner.prev.log') -Force
}

$nodePath = (Get-Command node -ErrorAction Stop).Source
Add-Content -Path $logFile -Encoding utf8 -Value ('[start-runner] node start ' + (Get-Date -Format o) + ' node=' + $nodePath)
& $nodePath --experimental-sqlite (Join-Path $root 'windows-runner\index.mjs')
$code = $LASTEXITCODE
Add-Content -Path $logFile -Encoding utf8 -Value ('[start-runner] node exited code ' + $code + ' at ' + (Get-Date -Format o))
exit $code
