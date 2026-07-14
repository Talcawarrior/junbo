"""Junbo Bot Watchdog - Bot'u izler, çökerse yeniden başlatır.

Kullanım:
    python watchdog.py              # Bot'u izle ve yeniden başlat
    python watchdog.py --check      # Sadece kontrol et
    python watchdog.py --status     # Durum bilgisi
"""

import subprocess
import time
import sys
import argparse
import requests
from datetime import datetime


BOT_DIR = r"C:\Users\fdemir\Documents\New project\junbo"
BOT_URL = "http://127.0.0.1:8093"
CHECK_INTERVAL = 30  # saniye


def is_bot_running() -> bool:
    """Bot çalışıyor mu?"""
    try:
        response = requests.get(f"{BOT_URL}/api/status", timeout=5)
        return response.status_code == 200
    except:
        return False


def start_bot():
    """Bot'u başlat."""
    subprocess.Popen(
        ["python", "main.py", "bot"],
        cwd=BOT_DIR,
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"[{datetime.now()}] Bot started")


def stop_bot():
    """Bot'u durdur."""
    subprocess.run(
        ["taskkill", "/F", "/IM", "python.exe"],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    print(f"[{datetime.now()}] Bot stopped")


def check_bot() -> dict:
    """Bot durumunu kontrol et."""
    try:
        response = requests.get(f"{BOT_URL}/api/status", timeout=5)
        if response.status_code == 200:
            data = response.json()
            return {
                "running": True,
                "is_running": data.get("is_running", False),
                "last_scan": data.get("stats", {}).get("last_scan"),
                "open_bets": data.get("stats", {}).get("total_bets", 0),
                "pnl": data.get("portfolio", {}).get("total_pnl", 0),
            }
    except:
        pass
    return {"running": False}


def watchdog_loop():
    """Watchdog ana döngüsü."""
    print(f"Bot watchdog started. Checking every {CHECK_INTERVAL}s...")
    print(f"Bot URL: {BOT_URL}")

    while True:
        try:
            status = check_bot()

            if not status["running"]:
                print(f"[{datetime.now()}] Bot DOWN - restarting...")
                stop_bot()
                time.sleep(2)
                start_bot()
                time.sleep(10)  # Bot'un başlamasını bekle
            elif not status.get("is_running", False):
                print(f"[{datetime.now()}] Bot UP but not running - API'den start tetiklenmeli")
            else:
                print(f"[{datetime.now()}] Bot OK - Open: {status.get('open_bets', 0)}, PnL: ${status.get('pnl', 0):.2f}")

        except Exception as e:
            print(f"[{datetime.now()}] Watchdog error: {e}")

        time.sleep(CHECK_INTERVAL)


def main():
    parser = argparse.ArgumentParser(description="Junbo Bot Watchdog")
    parser.add_argument("--check", action="store_true", help="Sadece kontrol et")
    parser.add_argument("--status", action="store_true", help="Durum bilgisi")
    parser.add_argument("--restart", action="store_true", help="Bot'u yeniden başlat")

    args = parser.parse_args()

    if args.check:
        status = check_bot()
        if status["running"]:
            print(f"Bot is RUNNING")
            print(f"  Is Running: {status.get('is_running')}")
            print(f"  Last Scan: {status.get('last_scan')}")
            print(f"  Open Bets: {status.get('open_bets')}")
            print(f"  PnL: ${status.get('pnl', 0):.2f}")
        else:
            print("Bot is DOWN")
        sys.exit(0 if status["running"] else 1)

    if args.status:
        status = check_bot()
        print(f"Bot Status: {'RUNNING' if status['running'] else 'DOWN'}")
        if status["running"]:
            print(f"  Is Running: {status.get('is_running')}")
            print(f"  Last Scan: {status.get('last_scan')}")
            print(f"  Open Bets: {status.get('open_bets')}")
            print(f"  PnL: ${status.get('pnl', 0):.2f}")
        sys.exit(0)

    if args.restart:
        print("Stopping bot...")
        stop_bot()
        time.sleep(2)
        print("Starting bot...")
        start_bot()
        time.sleep(5)
        status = check_bot()
        print(f"Bot status: {'RUNNING' if status['running'] else 'DOWN'}")
        sys.exit(0)

    # Default: watchdog loop
    watchdog_loop()


if __name__ == "__main__":
    main()
