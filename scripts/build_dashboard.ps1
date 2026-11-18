# Dashboard build: tailwind D: junction'lari tarayamaz, once park et, sonra geri al.
# Kullanim: powershell -ExecutionPolicy Bypass -File scripts/build_dashboard.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$links = @("analysis_results", "archive", "backtest_results", "backups", "logs", "unified")
$removed = @()
foreach ($n in $links) {
    $p = Join-Path "data" $n
    if ((Test-Path $p) -and ((Get-Item $p -Force).LinkType -eq "Junction")) {
        cmd /c "rmdir $p"
        $removed += $n
    }
}
try {
    npm run build:local
} finally {
    $targets = @{
        analysis_results = "D:\JUNBO data\analysis_results"; archive = "D:\JUNBO data\archive"
        backtest_results = "D:\JUNBO data\backtest_results"; backups = "D:\JUNBO data\backups"
        logs = "D:\JUNBO data\logs"; unified = "D:\JUNBO data\unified"
    }
    foreach ($n in $removed) {
        if (-not (Test-Path "data\$n")) { cmd /c "mklink /J data\$n `"$($targets[$n])`"" }
    }
    # backup job'u build sirasinda gercek klasor yaratmissa D'ye tasi + relink
    if ((Test-Path "data\backups") -and ((Get-Item "data\backups" -Force).LinkType -ne "Junction")) {
        cmd /c "robocopy data\backups ""D:\JUNBO data\backups"" /MOVE /E /R:2 /W:5 /NFL /NDL /NJH /NJS"
        cmd /c "rmdir data\backups"
        cmd /c "mklink /J data\backups ""D:\JUNBO data\backups"""
    }
}
Write-Host "build + junction restore tamam"
