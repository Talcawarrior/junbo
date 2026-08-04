@echo off
REM ================================================
REM JUNBO BOT - KALICI SERVIS
REM Bu script bot'u kalici olarak calistirir.
REM Bot crasht ederse otomatik olarak yeniden baslatir.
REM ================================================

cd /d "C:\Users\fdemir\Documents\New project\junbo"

:START
echo [%date% %time%] Bot baslatiliyor...
python main.py bot
echo [%date% %time%] Bot durdu, 10 saniye sonra yeniden baslatilacak...
timeout /t 10 /nobreak >nul
goto START
