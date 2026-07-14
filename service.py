"""Junbo Service - Kalıcı bot başlatıcı.

Bu script bot'u kalıcı olarak çalıştırır.
Ctrl+C ile durdurulabilir.

Kullanım:
    python service.py              # Bot'u kalıcı çalıştır
    python service.py --daemon     # Arka planda çalıştır
    python service.py --status     # Durum kontrolü
    python service.py --stop       # Durdur
"""

import subprocess
import sys
import os
import time
import signal
import argparse
import json
from pathlib import Path
from datetime import datetime

BOT_DIR = r"C:\Users\fdemir\Documents\New project\junbo"
PID_FILE = Path(BOT_DIR) / "bot.pid"
LOG_FILE = Path(BOT_DIR) / "logs" / "service.log"
BOT_URL = "http://127.0.0.1:8093"


def log(msg: str):
    """Log yaz."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    print(line)
    LOG_FILE.parent.mkdir(exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_bot_pid() -> int | None:
    """Bot PID'ini oku."""
    if PID_FILE.exists():
        try:
            return int(PID_FILE.read_text().strip())
        except:
            pass
    return None


def save_bot_pid(pid: int):
    """Bot PID'ini kaydet."""
    PID_FILE.write_text(str(pid))


def is_bot_running() -> bool:
    """Bot çalışıyor mu?"""
    import requests
    try:
        r = requests.get(f"{BOT_URL}/api/status", timeout=3)
        return r.status_code == 200
    except:
        return False


def start_bot():
    """Bot'u başlat."""
    # Mevcut bot'u öldür (sadece kendi PID'imiz değilse)
    old_pid = get_bot_pid()
    if old_pid:
        try:
            os.kill(old_pid, 0)  # Process var mı kontrol
            log(f"Bot zaten çalışıyor (PID: {old_pid})")
            return
        except OSError:
            pass  # Process yok, devam et

    # Bot'u başlat
    proc = subprocess.Popen(
        [sys.executable, "main.py", "bot"],
        cwd=BOT_DIR,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    save_bot_pid(proc.pid)
    log(f"Bot started (PID: {proc.pid})")


def stop_bot():
    """Bot'u durdur."""
    pid = get_bot_pid()
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            log(f"Bot stopped (PID: {pid})")
        except OSError:
            log(f"Process {pid} not found")
        PID_FILE.unlink(missing_ok=True)
    else:
        log("No bot PID found")


def service_loop():
    """Servis döngüsü - bot çökerse yeniden başlat."""
    log("Junbo Service started")

    while True:
        try:
            if not is_bot_running():
                log("Bot DOWN - restarting...")
                start_bot()
                time.sleep(10)  # Başlamasını bekle
            else:
                # Bot çalışıyor, 30 saniye bekle
                time.sleep(30)
        except KeyboardInterrupt:
            log("Service interrupted by user")
            break
        except Exception as e:
            log(f"Service error: {e}")
            time.sleep(5)


def daemon_mode():
    """Arka planda çalıştır."""
    # Zaten daemon olarak mı çalışıyor?
    if PID_FILE.exists():
        log("Daemon already running")
        return

    # Fork et
    pid = os.fork() if hasattr(os, 'fork') else None

    if pid is None:
        # Windows - doğrudan çalıştır
        log("Starting daemon (Windows mode)")
        service_loop()
    elif pid > 0:
        # Parent process
        save_bot_pid(pid)
        log(f"Daemon started (PID: {pid})")
        sys.exit(0)
    else:
        # Child process
        service_loop()


def main():
    parser = argparse.ArgumentParser(description="Junbo Service")
    parser.add_argument("--daemon", action="store_true", help="Arka planda çalıştır")
    parser.add_argument("--status", action="store_true", help="Durum kontrolü")
    parser.add_argument("--stop", action="store_true", help="Durdur")

    args = parser.parse_args()

    if args.status:
        if is_bot_running():
            print("Bot: RUNNING")
            import requests
            try:
                r = requests.get(f"{BOT_URL}/api/status", timeout=3)
                data = r.json()
                print(f"  Open bets: {data.get('stats', {}).get('total_bets', 0)}")
                print(f"  PnL: ${data.get('portfolio', {}).get('total_pnl', 0):.2f}")
            except:
                pass
        else:
            print("Bot: DOWN")
        return

    if args.stop:
        stop_bot()
        return

    if args.daemon:
        daemon_mode()
    else:
        # Foreground mode
        try:
            start_bot()
            service_loop()
        except KeyboardInterrupt:
            log("Stopping...")
            stop_bot()


if __name__ == "__main__":
    main()
