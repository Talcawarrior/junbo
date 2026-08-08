$S = "C:\Users\fdemir\Documents\New project\junbo\scripts"
$P = "C:\Users\fdemir\AppData\Local\Programs\Python\Python312\python.exe"
$W = "C:\Users\fdemir\Documents\New project\junbo"

$tasks = @(
    @{Name="Junbo-OrderbookCollect"; Script="collect_orderbook.py"; Trigger="every30m"},
    @{Name="Junbo-ActualsCollect"; Script="collect_actuals.py"; Trigger="every6h"},
    @{Name="Junbo-BackupDatabases"; Script="backup_databases.py"; Trigger="every6h"},
    @{Name="Junbo-SyncBacktest"; Script="sync_backtest_db.py"; Trigger="every6h"},
    @{Name="JunboBotWatchdog"; Script="bot_watchdog.py"; Trigger="every1m"},
    @{Name="JunboSnapshot"; Script="..\snapshot_task.bat"; Trigger="every30m"; IsBat=$true}
)

foreach ($t in $tasks) {
    try {
        $existing = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
        }

        $act = if ($t.IsBat) {
            New-ScheduledTaskAction -Execute "cmd.exe" -Argument ("/c `"$W\$($t.Script)`"") -WorkingDirectory $W
        } else {
            New-ScheduledTaskAction -Execute $P -Argument ("`"$S\$($t.Script)`"") -WorkingDirectory $W
        }

        switch ($t.Trigger) {
            "every1m" {
                $trig = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
            }
            "every30m" {
                $trig = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 30)
            }
            "hourly" {
                $trig = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours 1)
            }
            "every6h" {
                $trig = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Hours 6)
            }
            default {
                $trig = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1)
            }
        }

        $set = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries `
            -StartWhenAvailable `
            -WakeToRun `
            -RestartCount 3 `
            -RestartInterval (New-TimeSpan -Minutes 1) `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
            -MultipleInstances IgnoreNew

        $prin = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

        Register-ScheduledTask -TaskName $t.Name -Action $act -Trigger $trig -Settings $set -Principal $prin -Description $t.Name -Force
        Write-Host "OK: $($t.Name)" -ForegroundColor Green
    } catch {
        Write-Host "FAIL: $($t.Name) - $_" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "Registered tasks:"
Get-ScheduledTask | Where-Object {$_.TaskName -match "^Junbo-"} | Select-Object TaskName,State | Format-Table -AutoSize