# JUNBO BOT - Windows Task Scheduler (Son Versiyon)
# Sleep/Wake + Crash restart destekli

$TaskName = "JunboBot"
$ScriptPath = "C:\Users\fdemir\Documents\New project\junbo\start_bot.bat"

# Eski task'i temizle
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Action
$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$ScriptPath`""

# Trigger'lar
$TriggerStartup = New-ScheduledTaskTrigger -AtStartup
$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn
$TriggerRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 2) `
    -RepetitionDuration (New-TimeSpan -Days 365)

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

# Task'i kaydet
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger @($TriggerStartup, $TriggerLogon, $TriggerRepeat) `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Junbo Bot - Auto-restart on crash, sleep, wake" `
    -Force

Write-Host "============================================"
Write-Host "  Junbo Bot Service kuruldu!"
Write-Host "============================================"
Write-Host ""
Write-Host "Ozellikler:"
Write-Host "  - Bilgisayar acildiginda baslar"
Write-Host "  - Kullanici giris yaptiginda baslar"
Write-Host "  - Her 2 dakikada kontrol eder"
Write-Host "  - Sleep/Wake otomatik baslatma"
Write-Host "  - Crash'te 3 sn sonra yeniden baslatma"
