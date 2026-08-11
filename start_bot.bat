@echo off
REM ================================================
REM JUNBO BOT + WATCHDOG
REM Bot'u baslatir, cokerse otomatik yeniden baslatir.
REM ================================================
REM ONEMLI (2026-08-11): SADECE watchdog.py calistirilir.
REM watchdog.py bot'u kendisi baslatir ve olurse yeniden baslatir.
REM Eski `goto START` dongusu kaldirildi — bat hem watchdog hem kendi
REM dongusuyle bot baslatinca CIK BOT doguyordu (port cakismasi).

cd /d "C:\Users\fdemir\Documents\New project\junbo"

echo Watchdog baslatiliyor...
python watchdog.py
