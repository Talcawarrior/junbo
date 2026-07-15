# JUNBO BOT - Windows Task Scheduler (Sleep/Wake Destekli)
# Bilgisayar uykudan uyanınca VE açıldığında otomatik başlar

$TaskName = "JunboBot"
$ServiceScript = "C:\Users\fdemir\Documents\New project\junbo\service.ps1"

# Eski task'i temizle
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Action olustur
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ServiceScript`""

# Trigger 1: Bilgisayar acildiginda
$TriggerStartup = New-ScheduledTaskTrigger -AtStartup

# Trigger 2: Her 5 dakikada bir kontrol (sleep/wake icin)
$TriggerRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 365)

# Trigger 3: Kullanici giris yaptiginda
$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn

# Settings
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 365) `
    -MultipleInstances IgnoreNew

# Principal
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# Task'i kaydet (3 trigger ile)
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger @($TriggerStartup, $TriggerRepeat, $TriggerLogon) `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Junbo Bot - Auto-restart on crash, wake, and login" `
    -Force

Write-Host "============================================"
Write-Host "  Junbo Bot Service kuruldu!"
Write-Host "============================================"
Write-Host ""
Write-Host "Trigger'lar:"
Write-Host "  1. Bilgisayar acildiginda"
Write-Host "  2. Her 5 dakikada bir kontrol"
Write-Host "  3. Kullanici giris yaptiginda"
Write-Host ""
Write-Host "Ozellikler:"
Write-Host "  - Sleep/Wake otomatik baslatma"
Write-Host "  - Crash'te 1 dk sonra yeniden baslatma"
Write-Host "  - WakeToRun: Uykudan uyaninca calisir"
