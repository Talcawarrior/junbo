# JUNBO BOT - PowerShell Service (İyileştirilmiş)
# Bot'un tam olarak başlamasını bekler

$BotDir = "C:\Users\fdemir\Documents\New project\junbo"
$LogFile = "$BotDir\logs\service.log"
$MaxRestarts = 1000
$RestartDelay = 10
$StartupWait = 30  # Bot'un başlaması için bekleme süresi

function Write-Log {
    param($Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logMessage = "[$timestamp] $Message"
    Write-Host $logMessage
    Add-Content -Path $LogFile -Value $logMessage -ErrorAction SilentlyContinue
}

function Start-Bot {
    Write-Log "Starting bot..."
    $proc = Start-Process -FilePath "python" -ArgumentList "main.py bot" `
        -WorkingDirectory $BotDir `
        -PassThru `
        -WindowStyle Hidden
    Write-Log "Bot started (PID: $($proc.Id))"
    return $proc
}

function Test-BotRunning {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:8093/api/status" -TimeoutSec 5 -UseBasicParsing
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

# Main loop
Write-Log "=== Junbo Bot Service Started ==="
$restartCount = 0

while ($restartCount -lt $MaxRestarts) {
    $proc = Start-Bot
    
    # Bot'un tam olarak başlaması için bekle
    Write-Log "Waiting ${StartupWait}s for bot to initialize..."
    Start-Sleep -Seconds $StartupWait
    
    if (Test-BotRunning) {
        Write-Log "Bot is running successfully"
        
        # Monitor loop
        $checkCount = 0
        while ($true) {
            Start-Sleep -Seconds 30
            $checkCount++
            
            if (-not (Test-BotRunning)) {
                Write-Log "Bot health check failed! Restarting..."
                break
            }
            
            # Check if process is still alive
            if ($proc.HasExited) {
                Write-Log "Bot process exited! Restarting..."
                break
            }
            
            # Her 10 kontrolde bir log yaz
            if ($checkCount % 10 -eq 0) {
                Write-Log "Bot still running (check #$checkCount)"
            }
        }
    } else {
        Write-Log "Bot failed to start after ${StartupWait}s"
        # Bot'u öldür ve yeniden dene
        try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch {}
    }
    
    $restartCount++
    Write-Log "Restart attempt $restartCount of $MaxRestarts"
    Start-Sleep -Seconds $RestartDelay
}

Write-Log "=== Service stopped (max restarts reached) ==="
