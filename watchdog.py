"""Junbo Bot Watchdog - Bot'u izler, çökerse yeniden başlatır.

Kullanım:
    python watchdog.py              # Bot'u izle ve yeniden başlat
    python watchdog.py --check      # Sadece kontrol et
    python watchdog.py --status     # Durum bilgisi
"""

import subprocess
import time
import sys
import os
import platform
import argparse
import signal
from datetime import datetime

# Cross-platform imports
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_URL = "http://127.0.0.1:8093"
CHECK_INTERVAL = 30  # saniye

IS_WINDOWS = platform.system() == "Windows"


def log(msg: str):
    """Log yaz."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")


def is_bot_running() -> bool:
    """Bot çalışıyor mu?"""
    if not HAS_REQUESTS:
        # requests yoksa port kontrolü yap
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            result = sock.connect_ex(('127.0.0.1', 8093))
            sock.close()
            return result == 0
        except Exception:
            return False

    try:
        response = requests.get(f"{BOT_URL}/api/status", timeout=5)
        return response.status_code == 200
    except Exception:
        return False


def get_bot_pid() -> int | None:
    """Bot PID'ini oku."""
    pid_file = os.path.join(BOT_DIR, "bot.pid")
    if os.path.exists(pid_file):
        try:
            with open(pid_file, "r") as f:
                return int(f.read().strip())
        except (ValueError, IOError):
            pass
    return None


def save_bot_pid(pid: int):
    """Bot PID'ini kaydet."""
    pid_file = os.path.join(BOT_DIR, "bot.pid")
    with open(pid_file, "w") as f:
        f.write(str(pid))


def clear_bot_pid():
    """Bot PID dosyasını temizle."""
    pid_file = os.path.join(BOT_DIR, "bot.pid")
    if os.path.exists(pid_file):
        os.remove(pid_file)


def start_bot():
    """Bot'u başlat."""
    # Mevcut bot'u kontrol et
    old_pid = get_bot_pid()
    if old_pid:
        try:
            os.kill(old_pid, 0)  # Process var mı kontrol
            log(f"Bot zaten çalışıyor (PID: {old_pid})")
            return
        except OSError:
            pass  # Process yok, devam et

    # Bot'u başlat
    cmd = [sys.executable, "main.py", "bot"]
    kwargs = {
        "cwd": BOT_DIR,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }

    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)
    save_bot_pid(proc.pid)
    log(f"Bot started (PID: {proc.pid})")


def stop_bot():
    """Bot'u durdur."""
    pid = get_bot_pid()
    if pid:
        try:
            if IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                os.kill(pid, signal.SIGTERM)
                time.sleep(2)
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            log(f"Bot stopped (PID: {pid})")
        except OSError as e:
            log(f"Error stopping bot: {e}")
        clear_bot_pid()
    else:
        log("No bot PID found")


def check_bot() -> dict:
    """Bot durumunu kontrol et."""
    result = {"running": False}

    if not HAS_REQUESTS:
        # Port kontrolü
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            result["running"] = sock.connect_ex(('127.0.0.1', 8093)) == 0
            sock.close()
        except Exception:
            pass
        return result

    try:
        response = requests.get(f"{BOT_URL}/api/status", timeout=5)
        if response.status_code == 200:
            data = response.json()
            result = {
                "running": True,
                "is_running": data.get("is_running", False),
                "last_scan": data.get("stats", {}).get("last_scan"),
                "open_bets": data.get("stats", {}).get("total_bets", 0),
                "pnl": data.get("portfolio", {}).get("total_pnl", 0),
            }
    except Exception:
        pass

    return result


def watchdog_loop():
    """Watchdog ana döngüsü."""
    log(f"Bot watchdog started. Checking every {CHECK_INTERVAL}s...")
    log(f"Bot URL: {BOT_URL}")

    while True:
        try:
            status = check_bot()

            if not status["running"]:
                log("Bot DOWN - restarting...")
                stop_bot()
                time.sleep(2)
                start_bot()
                time.sleep(10)  # Bot'un başlamasını bekle
            elif not status.get("is_running", False):
                log("Bot UP but not running")
            else:
                log(f"Bot OK - Open: {status.get('open_bets', 0)}, PnL: ${status.get('pnl', 0):.2f}")

        except KeyboardInterrupt:
            log("Watchdog interrupted by user")
            break
        except Exception as e:
            log(f"Watchdog error: {e}")

        time.sleep(CHECK_INTERVAL)


def main():
    parser = argparse.ArgumentParser(description="Junbo Bot Watchdog")
    parser.add_argument("--check", action="store_true", help="Sadece kontrol et")
    parser.add_argument("--status", action="store_true", help="Durum bilgisi")
    parser.add_argument("--restart", action="store_true", help="Bot'u yeniden başlat")

    args = parser.parse_args()

    if args.check or args.status:
        status = check_bot()
        if status["running"]:
            print(f"Bot: RUNNING")
            if "is_running" in status:
                print(f"  Is Running: {status.get('is_running')}")
                print(f"  Last Scan: {status.get('last_scan')}")
                print(f"  Open Bets: {status.get('open_bets')}")
                print(f"  PnL: ${status.get('pnl', 0):.2f}")
        else:
            print("Bot: DOWN")
        sys.exit(0 if status["running"] else 1)

    if args.restart:
        log("Stopping bot...")
        stop_bot()
        time.sleep(2)
        log("Starting bot...")
        start_bot()
        time.sleep(5)
        status = check_bot()
        print(f"Bot status: {'RUNNING' if status['running'] else 'DOWN'}")
        sys.exit(0)

    # Default: watchdog loop
    watchdog_loop()


if __name__ == "__main__":
    main()
