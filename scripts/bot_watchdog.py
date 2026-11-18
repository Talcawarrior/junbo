"""JunboBot watchdog — keeps the JunboBot Windows service alive.

Runs from Task Scheduler every 1 minute (SYSTEM). Guarantees the bot stays
up across reboots, wake-from-sleep, clean exits and accidental disables:

  1. If the service is not RUNNING (stopped/disabled), re-enable (AUTO) and
     start it. This also covers the "someone/something disabled it" case.
  2. Heartbeat check: if logs/bot.log has not been written for
     HEARTBEAT_TIMEOUT seconds while the service is RUNNING, the process is
     assumed frozen and the service is restarted.

Logs to logs/watchdog.log (append). Safe to run on every tick.
"""

import os
import subprocess
import time
from datetime import datetime

SERVICE = "JunboBot"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(REPO, "logs", "watchdog.log")
BOT_LOG = os.path.join(REPO, "logs", "bot.log")
HEARTBEAT_TIMEOUT = 15 * 60  # seconds
DATA_ROOT = os.getenv("JUNBO_DATA_DIR") or r"C:\JUNBO data"
DATA_WATCHDOG_LOG = os.path.join(DATA_ROOT, "logs", "data_watchdog.log")
DATA_WATCHDOG_TASK = "JunboDataWatchdog"
DATA_WATCHDOG_STALE = 15 * 60  # seconds
# 2026-11-18: tek bir data-watchdog tick'i log yazmadan uzun surebilir (or.
# 17:59'daki collect_orderbook 19 dk). Gorev Running iken log yasi bu esige
# kadar normaldir (gorev ExecutionTimeLimit 30 dk + 6 dk aralik + pay).
DATA_WATCHDOG_HUNG = 40 * 60  # seconds


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {msg}"
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).returncode
    except Exception as e:  # noqa: BLE001
        log(f"ERR running {cmd}: {e}")
        return -1


def service_state() -> str:
    try:
        out = subprocess.run(["sc", "query", SERVICE], capture_output=True, text=True, timeout=30).stdout
    except Exception as e:  # noqa: BLE001
        return f"QUERY_FAILED {e}"
    for line in out.splitlines():
        if "STATE" in line:
            return line.split("STATE", 1)[1].strip()
    return "UNKNOWN"


def ensure_running() -> None:
    state = service_state()
    if "RUNNING" in state:
        if os.path.exists(BOT_LOG):
            age = time.time() - os.path.getmtime(BOT_LOG)
            if age > HEARTBEAT_TIMEOUT:
                log(f"HEARTBEAT stale ({age:.0f}s > {HEARTBEAT_TIMEOUT}s) - restarting frozen service")
                _run(["net", "stop", SERVICE])
                time.sleep(3)
                _run(["sc", "config", SERVICE, "start=", "auto"])
                _run(["net", "start", SERVICE])
            else:
                log(f"OK running (log age {age:.0f}s)")
        else:
            log("OK running (bot.log absent)")
        return

    log(f"NOT running (state={state}) - enable(AUTO) + start")
    _run(["sc", "config", SERVICE, "start=", "auto"])
    _run(["net", "start", SERVICE])


def ensure_data_watchdog() -> None:
    """Data watchdog 15 dk'dir log yazmadiysa gorevi enable + run et.

    2026-11-18: data watchdog 19 Eyl-11 Kas arasi durdu ve kimse fark
    etmedi; orderbook/snapshot/backup gorevleri de onunla durdu. Bu kontrol
    data watchdog'un kendi bekcisidir.
    """
    age = None
    if os.path.exists(DATA_WATCHDOG_LOG):
        age = time.time() - os.path.getmtime(DATA_WATCHDOG_LOG)
    if age is not None and age <= DATA_WATCHDOG_STALE:
        return
    state = _task_state(DATA_WATCHDOG_TASK)
    if state == "Running" and age is not None and age <= DATA_WATCHDOG_HUNG:
        return  # uzun ama normal tick (yanlis "stale" alarmi verme)
    if state == "Running":
        log(f"DATA WATCHDOG hung (age={age:.0f}s, Running) - end+run {DATA_WATCHDOG_TASK}")
        _run(["schtasks", "/end", "/tn", DATA_WATCHDOG_TASK])
    else:
        log(f"DATA WATCHDOG stale (age={age}, state={state or '?'}) - enable+run {DATA_WATCHDOG_TASK}")
        _run(["schtasks", "/change", "/tn", DATA_WATCHDOG_TASK, "/enable"])
    _run(["schtasks", "/run", "/tn", DATA_WATCHDOG_TASK])


def _task_state(name: str) -> str:
    """Task Scheduler durumu (Ready/Running/Disabled). Dil bagimsiz; hata -> ''.

    schtasks ciktisi yerellestirilmis (bu makinede Rusca) oldugu icin
    PowerShell enum adi kullanilir. Yalnizca log bayatken cagrilir.
    """
    try:
        out = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-ScheduledTask -TaskName '{name}').State",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return (out.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


if __name__ == "__main__":
    try:
        ensure_running()
        ensure_data_watchdog()
    except Exception as e:  # noqa: BLE001
        log(f"WATCHDOG CRASH: {e}")
