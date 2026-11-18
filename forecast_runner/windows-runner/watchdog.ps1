$ErrorActionPreference = 'SilentlyContinue'
# Ne işe yarar: RUNNER BEKÇİSİ (saatlik Windows görevi). İKİ iş yapar:
#   (1) Canlı runner yoksa (port 8787 boş VE node index.mjs süreci yoksa) "JunboForecastRunner"
#       görevini başlatır. Böylece runner ölünce en geç ~1 saatte ayağa kalkar; uykuya yatmışsa
#       StartWhenAvailable geciken koşuyu uyanır uyanmaz çalıştırır. Çalışan örneğe dokunmaz (index.mjs
#       port doluysa kendini kapatır; buradaki port+node kontrolü o anı önler).
#   (2) WEATHERNEXT 2 TAZELİK KONTROLÜ — runner SAĞ OLSA DA her saat koşar; wn_ensemble'ın son çekimi
#       ~30 saatten eskiyse [WN2-UYARI] satırı yazar (yeniden başlatmaz, yalnız uyarır).
$logDir = Join-Path $PSScriptRoot 'logs'
$logFile = Join-Path $logDir 'runner.log'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

# --- (2) WEATHERNEXT 2 TAZELİK KONTROLÜ — runner sağ olsa da her saat koşar (erken uyarı) -----------
# NEDEN BURADA: wn_ensemble'ı yalnız runner yazar; tazelik denetimi (_tazelik_denetimi.mjs) de runner
# İÇİNDE koşar -> runner ölünce denetim de susar. 2026-09-29 00:23 → 10-04 19:33 arası runner ölüydü; o
# pencerede 6 hedef gün (09-28..10-03) hiç çekilmedi ve hiçbir rapor uyarmadı. Open-Meteo WN2 üyelerini
# yalnız ~3-4 gün saklar; yani gecikme KALICI veri kaybı demek (kurtarma yalnız GCS'ten). Erken uyarı şart.
# Sinyal: LIVE'daki en yeni fetched_at_utc. Normal = günde 1 kez ~00:00-01:00 UTC çekim -> en fazla ~30 saat.
# SALT-OKUNUR. Runner'ı YENİDEN BAŞLATMAZ (çalışan örnek başka iş yapıyor olabilir) — yalnız loglar.
# Log şişmesin: STALE sürerken en fazla 6 saatte bir yazar.
$liveDb = Join-Path (Split-Path $PSScriptRoot -Parent) 'data\tempmarket-edge.sqlite'
$nodeExe = (Get-Command node -ErrorAction SilentlyContinue).Source
$nodeDur = if ($nodeExe) { 'var' } else { 'YOK' }
$wnSon = ''
if ($nodeExe -and (Test-Path $liveDb)) {
  $wnJs = @'
const { DatabaseSync } = require('node:sqlite');
try {
  const db = new DatabaseSync(process.argv[1], { readOnly: true });
  const r = db.prepare('SELECT MAX(fetched_at_utc) mx FROM wn_ensemble').get();
  db.close();
  process.stdout.write(r && r.mx ? String(r.mx) : '');
} catch (e) { process.stdout.write('ERR'); }
'@
  $wnSon = (& $nodeExe --experimental-sqlite -e $wnJs $liveDb 2>$null | Out-String).Trim()
}
$wnSaat = $null
if ($wnSon -and $wnSon -ne 'ERR') {
  try { $wnSaat = [math]::Round(((Get-Date).ToUniversalTime() - [datetime]::Parse($wnSon, $null, [System.Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()).TotalHours, 1) } catch { $wnSaat = $null }
}
$wnMsg = ''
if (-not $wnSaat) {
  $wnMsg = '[watchdog] ' + (Get-Date -Format o) + ' [WN2-UYARI] WeatherNext tazeligi OKUNAMADI (node=' + $nodeDur + ', db=' + (Test-Path $liveDb) + ')'
} elseif ($wnSaat -gt 30) {
  $wnMsg = '[watchdog] ' + (Get-Date -Format o) + " [WN2-UYARI] WeatherNext $wnSaat saattir tazelenmedi (son cekim $wnSon). Acik-Meteo uyeleri ~3-4 gun saklar -> GECIKME = KALICI KAYIP (kurtarma: GCS). Runner WN2 adimini kontrol et."
}
if ($wnMsg) {
  # Aynı uyarıyı her saat yazma: son 6 saatte [WN2-UYARI] varsa tekrar yazma.
  $sonUyari = $null
  try {
    foreach ($s in (Get-Content -Path $logFile -Tail 400 -ErrorAction Stop)) {
      if ($s -match '^\[watchdog\] (\S+) \[WN2-UYARI\]') { $sonUyari = $matches[1] }
    }
  } catch { }
  $tekrar = $false
  if ($sonUyari) {
    try { if (((Get-Date).ToUniversalTime() - [datetime]::Parse($sonUyari, $null, [System.Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()).TotalHours -lt 6) { $tekrar = $true } } catch { }
  }
  if (-not $tekrar) {
    for ($i = 0; $i -lt 2; $i++) {
      try { Add-Content -Path $logFile -Encoding utf8 -Value $wnMsg; break } catch { Start-Sleep -Milliseconds 500 }
    }
  }
  Write-Output $wnMsg
}

$listening = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue
$nodeUp = $false
if (-not $listening) {
  $nodeUp = @(Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*index.mjs*' }).Count -gt 0
}
if ($listening -or $nodeUp) { exit 0 }   # canlı runner var -> sessiz çık (her saat log doldurma)

$line = '[watchdog] ' + (Get-Date -Format o) + ' runner DOWN (port 8787 bos, node yok) -> JunboForecastRunner baslatiliyor'
# Loga yaz (ölen süreç dosyayı bir an tutabiliyor — kilit yarışında bir kez dene).
for ($i = 0; $i -lt 2; $i++) {
  try { Add-Content -Path $logFile -Encoding utf8 -Value $line; break } catch { Start-Sleep -Milliseconds 500 }
}
Write-Output $line   # elle calistirinca gorunsun (zamanlanmis gorevin ciktisi atilir)
& schtasks.exe /run /tn 'JunboForecastRunner'
exit 0
