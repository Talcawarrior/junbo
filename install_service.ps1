# JUNBO BOT - Windows Task Scheduler Kurulumu (Güncellenmiş)
# PowerShell service ile kalıcı çalıştırma

$TaskName = "JunboBot"
$ServiceScript = "C:\Users\fdemir\Documents\New project\junbo\service.ps1"

# Eski task'i temizle
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

# Action olustur - PowerShell ile service script'ini calistir
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ServiceScript`""

# Trigger olustur (bilgisayar acildiginda)
$Trigger = New-ScheduledTaskTrigger -AtStartup

# Settings - restart on failure
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 365) `
    -MultipleInstances IgnoreNew

# Principal (SYSTEM olarak calistir)
$Principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

# Task'i kaydet
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Junbo Bot - Polymarket Weather Prediction Bot (Auto-restart on crash)" `
    -Force

Write-Host "============================================"
Write-Host "  Junbo Bot Service kuruldu!"
Write-Host "============================================"
Write-Host ""
Write-Host "Ozellikler:"
Write-Host "  - Bilgisayar acildiginda otomatik baslar"
Write-Host "  - Crasht ederse 1 dk sonra otomatik yeniden baslatilir"
Write-Host "  - 1000 kez yeniden baslatma denemesi"
Write-Host ""
Write-Host "Manuel kontrol:"
Write-Host "  Baslat: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Durdur: Stop-ScheduledTask -TaskName '$TaskName'"
Write-Host "  Durum:  Get-ScheduledTask -TaskName '$TaskName'"
