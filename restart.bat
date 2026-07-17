@echo off
cd /d "%~dp0"

echo Killing old bot processes...
for /f "tokens=2" %%a in ('tasklist /fi "WindowTitle eq bot" /nh 2^>nul') do taskkill /f /pid %%a 2>nul
for /f "tokens=2" %%a in ('wmic process where "commandline like '%%main.py bot%%'" get processid 2^>nul ^| findstr /r "[0-9]"') do taskkill /f /pid %%a 2>nul

timeout /t 2 /nobreak >nul

echo Starting bot...
start "bot" cmd /c "python main.py bot >> logs\stdout.log 2>> logs\stderr.log"

echo Bot restarted. Check logs\bot.log for status.
