"""Junbo Windows Service — runs main.py bot as a Windows Service.

Install:
    python junbo_service.py install

Start:
    python junbo_service.py start

Stop:
    python junbo_service.py stop

Remove:
    python junbo_service.py remove
"""

import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time

import servicemanager
import win32event
import win32service
import win32serviceutil

_BOT_DIR = os.path.dirname(os.path.abspath(__file__))
# When running inside the Windows Service host (pythonservice.exe),
# sys.executable resolves to pythonservice.exe — which is NOT a usable
# Python interpreter for launching the bot. Use the real python.exe that
# lives in the same directory as pythonservice.exe instead.
_PYTHON = os.path.join(os.path.dirname(sys.executable), "python.exe")
if not os.path.exists(_PYTHON):
    _PYTHON = sys.executable  # fallback
_PID_FILE = os.path.join(_BOT_DIR, "bot.pid")
_LOG_DIR = os.path.join(_BOT_DIR, "logs")
os.makedirs(_LOG_DIR, exist_ok=True)


def _setup_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(_LOG_DIR, "junbo_service.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    return logger


log = _setup_logger("JunboService")


class JunboService(win32serviceutil.ServiceFramework):
    """Windows Service that runs Junbo bot with auto-restart on crash."""

    _svc_name_ = "JunboBot"
    _svc_display_name_ = "Junbo Weather Bot"
    _svc_description_ = "Automated weather prediction betting bot for Polymarket"

    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self._proc: subprocess.Popen | None = None
        self._monitor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = True

    def _start_bot(self) -> subprocess.Popen:
        """Launch main.py bot and write PID."""
        _out_log = open(os.path.join(_LOG_DIR, "bot_out.log"), "wb")
        proc = subprocess.Popen(
            [_PYTHON, "main.py", "bot"],
            cwd=_BOT_DIR,
            stdout=_out_log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        # Write PID file
        try:
            with open(_PID_FILE, "w") as f:
                f.write(str(proc.pid))
        except Exception as e:
            log.warning("Could not write PID file: %s", e)
        log.info("Bot started (PID: %d)", proc.pid)
        return proc

    def _monitor(self):
        """Watch the bot subprocess and restart on crash."""
        while not self._stop_event.is_set():
            if self._proc is None:
                self._proc = self._start_bot()
            ret = self._proc.poll()
            if ret is not None:
                log.warning("Bot exited with code %d — restarting in 3s", ret)
                # Dump tail of bot output for diagnosis
                try:
                    with open(os.path.join(_LOG_DIR, "bot_out.log"), "rb") as f:
                        f.seek(0, 2)
                        size = f.tell()
                        f.seek(max(0, size - 4000))
                        tail = f.read().decode("utf-8", "replace")
                    for line in tail.splitlines()[-30:]:
                        log.warning("BOT> %s", line)
                except Exception:
                    pass
                self._proc = None
                if self._stop_event.wait(3):
                    break
                self._proc = self._start_bot()
            else:
                self._stop_event.wait(5)  # check every 5s

    def SvcStop(self):
        """Stop the service — terminate bot, stop monitor."""
        log.info("Service stopping...")
        self._running = False
        self._stop_event.set()
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)

        # Kill bot subprocess
        if self._proc and self._proc.poll() is None:
            log.info("Terminating bot (PID: %d)", self._proc.pid)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("Bot did not exit in 10s — killing")
                self._proc.kill()
                self._proc.wait()
        log.info("Service stopped")

    def SvcDoRun(self):
        """Main entry point — called by SCM."""
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        log.info("Service starting...")
        self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
        self._monitor_thread.start()

        # Wait for stop signal
        win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)
        log.info("Service shutdown complete")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(JunboService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(JunboService)
