"""Start a command with NO console window (Windows task helper).

Why: on Windows 11 the default terminal is Windows Terminal. A scheduled
task that runs a console program (powershell.exe) gets a new terminal tab
even with ``-WindowStyle Hidden``, so the window flashes on the desktop at
every trigger. Running the same command through ``pythonw.exe`` (GUI
subsystem, no console) with ``CREATE_NO_WINDOW`` avoids the window entirely.

Usage (task action):
  pythonw.exe "...\\scripts\\run_hidden.py" powershell.exe -NoProfile ... -File "..."

The launcher exits immediately; the child keeps running detached.
Importing this module does nothing (no side effects).
"""

import subprocess
import sys


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 2
    # CREATE_NO_WINDOW: child console exists but no window is created.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    subprocess.Popen(argv[1:], creationflags=flags, close_fds=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
