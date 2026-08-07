$S = "C:\Users\fdemir\Documents\New project\junbo\scripts"
$P = "python"
$W = "C:\Users\fdemir\Documents\New project\junbo"

$tasks = @(
    @{Name="Junbo-OrderbookCollect"; Action="$P $S\collect_orderbook.py"; Trigger="every30m"},
    @{Name="Junbo-ActualsCollect"; Action="$P $S\collect_actuals.py"; Trigger="every6h"},
    @{Name="Junbo-BackupDatabases"; Action="$P $S\backup_databases.py"; Trigger="every6h"},
    @{Name="Junbo-SyncBacktest"; Action="$P $S\sync_backtest_db.py"; Trigger="every6h"},
    @{Name="JunboBotWatchdog"; Action="$P $S\bot_watchdog.py"; Trigger="every1m"}
)

foreach ($t in $tasks) {
    try {
        $existing = Get-ScheduledTask -TaskName $t.Name -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false
        }

        $act = New-ScheduledTaskAction -Execute "python" -Argument ("`"$S\$($t.Action.Split()[-1])`"") -WorkingDirectory $W

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