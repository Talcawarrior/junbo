# TempMarket Edge — Windows Runner

Bu klasör, Windows masaüstü bilgisayarında arka planda sürekli çalışacak veri toplama işçisini ve tarayıcı kontrol panelinin yerel API’sini içerir. İşçi her döngüde istasyon bazında tahminleri, METAR gözlemlerini, kaynak isteklerinin durumunu ve Polymarket market metadata bilgisini kaydeder; SQLite içindeki tekrar eden tahmin koşularından hedef tarih kesişim raporu üretir.

## Çalışma modeli

İşçi varsayılan olarak her 60 dakikada bir çalışır ve ilk döngüyü hemen başlatır. Her kaynak isteği `source_requests` tablosunda istasyon, URL, zaman, HTTP durumu, yanıt süresi ve hata bilgisiyle tutulur. Her tahmin kaydı; istasyon, sağlayıcı, çalıştırma zamanı, hedef tarih, tahmin ufku ve maksimum sıcaklık ile birlikte `forecast_snapshots` tablosuna yazılır. Böylece aynı hedef tarihin bugün alınan üç günlük tahmin içinde ve yarın alınan üç günlük tahmin içinde tekrar görünüp görünmediği hesaplanabilir.

Bir hedef tarih en az iki farklı `run_at_utc` içinde görüldüğünde rapor `overlap: true` olur. WU veya başka bir doğrulanmış settlement sonucu `import-settlement.mjs` ile girildiğinde rapor, tahmin ortalamasını gerçekleşen maksimumla karşılaştırır; varsayılan uyum toleransı 1.0 °C’dir. Bu karşılaştırma tahmini karar değil, geriye dönük doğrulama ve model/istasyon bias ölçümüdür.

## Kurulum

Windows üzerinde Node.js 22 veya daha yeni bir sürüm kurulu olmalıdır; çünkü runner, ek native SQLite paketi gerektirmemek için Node’un `node:sqlite` API’sini kullanır. Proje klasöründe PowerShell açın ve şu dosyayı oluşturun:

```powershell
Copy-Item windows-runner/config.example.json windows-runner/config.json
```

`config.json` içinde gerçek istasyonları, ICAO kodlarını, zaman dilimlerini, settlement kaynağını ve varsa Polymarket `marketSlug` alanını düzenleyin. ABD istasyonlarında NWS saatlik tahmini kullanılır. ABD dışındaki istasyonlarda `VISUAL_CROSSING_API_KEY` ayarlanmışsa Visual Crossing üç günlük günlük tahminleri kullanılır; anahtar yoksa bu kaynak açıkça başarısız olarak loglanır ve sistem gözlem toplamaya devam eder.

İlk manuel test için:

```powershell
$env:TEMP_MARKET_CONFIG = "$PWD\windows-runner\config.json"
node --experimental-sqlite windows-runner/index.mjs
```

Windows bilgisayarında tarayıcıdan çalışan kontrol panelinin runner’a bağlanması için `http://127.0.0.1:8787` adresini kullanın. Aynı LAN’daki başka bir masaüstü tarayıcıdan erişecekseniz Windows makinesinin LAN IP adresini kullanarak `http://192.168.x.x:8787` adresini açın. Gerekirse Windows Defender Firewall’da yalnızca **Private network** profili için TCP 8787 inbound kuralı açın.

## Windows açılışında otomatik başlatma

PowerShell’i yönetici olarak açıp çalıştırın:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\windows-runner\install-task.ps1
```

Bu, `TempMarket Edge Runner` adlı Task Scheduler görevini Windows açılışına bağlar, SYSTEM hesabıyla çalıştırır ve hata sonrası yeniden başlatır. Kaldırmak için:

```powershell
.\windows-runner\uninstall-task.ps1
```

## Settlement / WU sonucu girişi

WU’nun ücretsiz web geçmişi veya marketin resmi resolution kaynağından doğrulanmış sonuç elde edildiğinde şu komutla kaydedin:

```powershell
pnpm runner:import-settlement -- --station=jfk --date=2026-08-27 --high-c=30.8 --source="WU history"
```

Bu işlem raporu otomatik olarak güncellemez; bir sonraki runner döngüsü veya yeniden başlatma sonrasında ilgili istasyon-hedef tarih raporu yeniden yazılır. Aynı istasyon, tarih ve kaynak için yeni değer gelirse önceki değer güncellenir.

## Otomatik settlement inbox

Her döngüde worker `data/inbox/settlements.jsonl` dosyasını okur. Her satıra bir doğrulanmış sonuç JSON’u yazın; aynı station/date/source anahtarı tekrar gelirse SQLite kaydı güncellenir:

```json
{"stationId":"jfk","targetDate":"2026-08-27","highC":30.8,"source":"WU history","recordedAtUtc":"2026-08-28T00:00:00.000Z"}
```

Bu yöntem WU sayfasını otomatik ve kırılgan biçimde kazımak yerine sonucu açıkça doğrulanmış bir kayıt olarak sisteme alır; kaynak, tarih ve değer raporda görünür.

## Yerel API uçları

| Uç | Amaç |
|---|---|
| `GET /health` | Worker’ın çalıştığını ve döngü aralığını kontrol eder. |
| `GET /api/stations` | Etkin istasyonları döndürür. |
| `GET /api/reports` | İstasyon-hedef tarih raporlarını döndürür. |
| `GET /api/report?stationId=jfk&targetDate=YYYY-MM-DD` | Tek bir kesişim ve settlement raporu döndürür. |
| `GET /api/sources?stationId=jfk` | Kaynak isteklerinin durum ve süre geçmişini döndürür. |

## Kaynak notları

NWS API, ABD için `points` ve `forecastHourly` uçlarıyla ücretsiz açık veri sağlar; isteklerde açıklayıcı bir `User-Agent` göndermek gerekir [1]. AviationWeather METAR/TAF ve istasyon verisini dünya çapında sağlar, ancak isteklerin kapsamı ve sıklığı sınırlıdır; API dokümanında üst sınır 100 istek/dakika olarak belirtilir [2]. Polymarket Gamma API market metadata ve event keşfi için herkese açık bir API sağlar [3]. WU tarafı için bu sürüm otomatik scraping yapmaz; settlement kaynağının market kurallarıyla doğrulanması ve güvenilir bir import yöntemiyle kaydedilmesi gerekir.

## References

[1]: https://www.weather.gov/documentation/services-web-API "National Weather Service API Web Service"
[2]: https://aviationweather.gov/data/api/ "AviationWeather.gov Data API"
[3]: https://docs.polymarket.com/market-data/discover-markets "Polymarket Discover Markets"
