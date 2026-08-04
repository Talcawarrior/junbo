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


if __name__ == "__main__":
    try:
        ensure_running()
    except Exception as e:  # noqa: BLE001
        log(f"WATCHDOG CRASH: {e}")
