# JUNBO BOT - PowerShell Service
# Bu script bot'u kalici olarak calistirir
# ve crasht ederse otomatik olarak yeniden baslatir.

$BotDir = "C:\Users\fdemir\Documents\New project\junbo"
$LogFile = "$BotDir\logs\service.log"
$MaxRestarts = 1000
$RestartDelay = 5

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
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:8093/api/status" -TimeoutSec 3 -UseBasicParsing
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
    
    # Wait for bot to start
    Start-Sleep -Seconds 10
    
    if (Test-BotRunning) {
        Write-Log "Bot is running successfully"
        
        # Monitor loop
        while ($true) {
            Start-Sleep -Seconds 30
            
            if (-not (Test-BotRunning)) {
                Write-Log "Bot crashed! Restarting..."
                break
            }
            
            # Check if process is still alive
            if ($proc.HasExited) {
                Write-Log "Bot process exited! Restarting..."
                break
            }
        }
    } else {
        Write-Log "Bot failed to start"
    }
    
    $restartCount++
    Write-Log "Restart attempt $restartCount of $MaxRestarts"
    Start-Sleep -Seconds $RestartDelay
}

Write-Log "=== Service stopped (max restarts reached) ==="
