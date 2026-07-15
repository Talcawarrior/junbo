@echo off
REM ================================================
REM JUNBO BOT - KALICI BASLATICI
REM Bot crasht ederse otomatik olarak yeniden baslatir.
REM Bu dosyayi calistirarak bot'u kalici olarak baslatin.
REM ================================================

cd /d "C:\Users\fdemir\Documents\New project\junbo"

:START
echo [%date% %time%] Bot baslatiliyor...
python main.py bot
echo [%date% %time%] Bot durdu! 5 saniye sonra yeniden baslatilacak...
timeout /t 5 /nobreak >nul
goto START
