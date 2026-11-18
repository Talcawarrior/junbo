$cli = "C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe"
# OBSOLETE (2026-11-18): bu script WARP'i TAM VPN moduna gecirir (`mode warp`). Bot (POLY_PROXY) ve
# forecast runner PROXY modu bekler (socks5 127.0.0.1:40000); VPN modunda SOCKS portu kapanir.
# Zamanlanmis gorevde KULLANILMAZ: JunboWARP artik scripts\warp_health.ps1 calistirir. Elle calistirmayin.
# WARP baglan (VPN mode)
& $cli connect 2>&1 | Out-Null
Start-Sleep -Seconds 2
& $cli mode warp 2>&1 | Out-Null
