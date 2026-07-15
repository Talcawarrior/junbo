@echo off
REM ================================================
REM JUNBO BOT + WATCHDOG
REM Bot'u başlatır, çökerse otomatik yeniden başlatır.
REM ================================================

cd /d "C:\Users\fdemir\Documents\New project\junbo"

REM Watchdog'u başlat (arka planda)
echo Watchdog baslatiliyor...
start /B python watchdog.py

REM Ana döngü - bot'u başlat ve izle
:START
echo [%date% %time%] Bot baslatiliyor...
python main.py bot
echo [%date% %time%] Bot durdu! 3 saniye sonra yeniden baslatilacak...
timeout /t 3 /nobreak >nul
goto START
