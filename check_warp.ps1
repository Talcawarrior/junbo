import-module NetSecurity -ErrorAction SilentlyContinue

$warp = Get-WmiObject -Class Win32_PnPEntity -Filter "Name like '%Cloudflare WARP%'" -ErrorAction SilentlyContinue;
if ($warp) {
    Write-Host 'Cloudflare WARP is installed';
    Write-Host "Device ID: $($warp.DeviceID)";
    Write-Host "Description: $($warp.Description)";
} else {
    Write-Host 'Cloudflare WARP is NOT installed';
    # Check for any WARP-like entries
    $anywarp = Get-WmiObject -Class Win32_PnPEntity -Filter "Description like '%WARP%'" -ErrorAction SilentlyContinue;
    if ($anywarp) {
        Write-Host 'Found other WARP-like entries:'
        $anywarp | ForEach-Object { Write-Host "  - $($_.Description) \" };
    }
}