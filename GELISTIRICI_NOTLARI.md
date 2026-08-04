# Geliştirici Notları

## Zorunlu Kurallar

### 1. Her kod değişikliğinden sonra testleri çalıştır ve botu başlat

Herhangi bir kod değişikliği (backend, frontend, utils, test, her ne olursa olsun) yapıldığında:

```bash
# 1. Ruff kontrolü (F, E, W kuralları)
ruff check

# 2. Pylint kontrolü
pylint --disable=C,R --score=n api.py bot_loop.py watchdog.py service.py

# 3. Testleri çalıştır
python -m pytest tests/ -x -q --tb=short

# 4. Botu yeniden başlat
python main.py restart
```

Bu adımlar **atlanamaz**. Değişiklik ne kadar küçük olursa olsun, testler çalıştırılmalı ve bot yeniden başlatılmalıdır.

### 2. database/db.py — KESİNLİKLE DOKUNMA

**`database/db.py` dosyasına asla dokunulmayacak.**

Bu dosya:
- Botun gerçek veritabanı bağlantısını yönetir
- Engine, SessionLocal, DB_PATH gibi kritik altyapıyı içerir
- Tüm modüller tarafından import edilir
- En ufak bir değişiklik tüm botu çökertir

Bu dosyada değişiklik yapmak yerine:
- Testler için temp DB kullanılacaksa test dosyasının içinde `importlib.reload(database.db)` yap
- Yeni bir özellik eklenmesi gerekiyorsa ayrı bir modülde yap
- Herhangi bir sorun varsa sadece kullanıcıya bildir, dokunma

### 3. Mevcut kodu yeniden yazma

Sadece hedeflenen değişikliği yap. İlgisiz kodları yeniden yazma, "temizlik" yapma.

### 4. Minimal diff

Mümkün olan en küçük değişiklikle işi çöz. Gereksiz satır ekleme/çıkarma yapma.

### 5. Gerçek DB asla değiştirilmez

- Bot çalışırken asla DB'ye direkt SQL yazma
- Tüm işlemler API/uygulama katmanından yapılır
- Testler temp DB kullanır, gerçek DB'ye dokunmaz

### 6. Her kod değişikliğinden sonra ayrı branch'e push et

Herhangi bir kod değişikliği (backend, frontend, utils, test, config, her ne olursa olsun) yapıldığında:

```bash
# 1. Yeni branch oluştur veya mevcut branch'te çalış
git checkout -b <aciklama>/<kisa-konu>

# 2. Değişiklikleri stage et ve commit yap
git add .
git commit -m "KISA: Yapılan değişikliğin özeti"

# 3. Branch'i push et
git push origin <branch-adi>

# 4. (Opsiyonel) PR oluştur
gh pr create --fill
```

**Kurallar:**
- Asla `main`, `dev` veya `feature/partial-tp` gibi ana branch'lere direkt push yapma
- Her branch sadece TEK bir konuyu/feature'ı/bug fix'ini içermeli
- Branch ismi formatı: `<tip>/<kısa-açıklama>` (örn. `fix/scan-loop-crash`, `feature/new-endpoint`, `test/calculator- coverage`)
- Commit mesajı İngilizce ve açıklayıcı olmalı
- Push etmeden önce testleri çalıştır (Kural 1)

### 7. "Pending" market ne demek?

Bot database'deki `bets` tablosunda `status="open"` olan kayıtlar **pending** olarak adlandırılır. Bunlar:
- Polymarket'te hala işlem gören (açık) marketlerdir
- Henüz kazanç/kayıp olarak sonuçlanmamıştır
- Settlement loop'u her döngüde bu marketleri Polymarket API'den sorgular
- Polymarket marketi çözünce (`resolved=yes/no`), bot ilgili bet'i günceller
- `0 won, 0 lost, N pending` = henüz hiçbiri çözülmemiş, bu NORMALDİR

**Neden çözülmez?** Polymarket marketleri genellikle target_date'den 24-48 saat sonra çözülür. Bot'un görevi sabırla beklemek ve çözülünce işlem yapmaktır.
