"""macOS launchd service management for hapbeat-helper."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

LABEL = "com.hapbeat.helper"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG_PATH = Path.home() / "Library" / "Logs" / "hapbeat-helper.log"


def log_path() -> Path:
    return LOG_PATH


def _hapbeat_helper_path() -> str:
    """Locate hapbeat-helper, preferring the same venv we are running in.

    Resolution order:
    1. Sibling of sys.executable (e.g. <venv>/bin/hapbeat-helper).
    2. shutil.which fallback (system-wide install case).
    """
    py_dir = Path(sys.executable).resolve().parent
    candidate = py_dir / "hapbeat-helper"
    if candidate.is_file():
        return str(candidate)

    path = shutil.which("hapbeat-helper")
    if path:
        return path

    raise RuntimeError(
        "hapbeat-helper not found next to the current Python "
        f"({sys.executable}) and not on PATH. "
        "Install via pipx: pipx install hapbeat-helper"
    )


def _write_plist(exe: str) -> None:
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>          <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{exe}</string>
    <string>start</string>
  </array>
  <key>RunAtLoad</key>      <true/>
  <key>KeepAlive</key>      <true/>
  <key>StandardOutPath</key> <string>{LOG_PATH}</string>
  <key>StandardErrorPath</key><string>{LOG_PATH}</string>
</dict>
</plist>
"""
    PLIST_PATH.write_text(plist, encoding="utf-8")


def _uid() -> str:
    import os
    return str(os.getuid())


def _bootstrap() -> None:
    """Load the plist via launchctl bootstrap (macOS 10.13+)."""
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{_uid()}", str(PLIST_PATH)],
        check=True,
    )


def _bootout() -> None:
    """Unload the service via launchctl bootout (macOS 10.13+)."""
    subprocess.run(
        ["launchctl", "bootout", f"gui/{_uid()}", str(PLIST_PATH)],
        check=False,  # may already be stopped
    )


def install() -> None:
    exe = _hapbeat_helper_path()
    # If a stale plist exists, unload it first so bootstrap doesn't see
    # a duplicate label.  ``_bootout`` is harmless if the job isn't
    # currently loaded.
    if PLIST_PATH.exists():
        _bootout()
    _write_plist(exe)
    _bootstrap()
    print("hapbeat-helper service installed and started.")
    print(f"  plist: {PLIST_PATH}")
    print(f"  log:   {LOG_PATH}")


def uninstall() -> None:
    _bootout()
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
    print("hapbeat-helper service removed.")


def stop() -> None:
    """Stop the running instance.

    If registered as a service (KeepAlive=true), uses ``launchctl bootout``
    to unload the job for the current session.  The plist remains in
    LaunchAgents so the job will auto-start again at the next login.
    To restart immediately without logging out, run ``install-service``.

    If no service is registered, kills any foreground process via pkill.
    """
    is_reg = PLIST_PATH.exists()
    if not is_reg:
        # No service registered → kill any foreground process.
        result = subprocess.run(
            ["pkill", "-f", "hapbeat[-_]helper"], check=False
        )
        if result.returncode == 0:
            print("hapbeat-helper stopped.")
        else:
            print("hapbeat-helper is not running.")
        return

    # bootout unloads the job without deleting the plist.
    # Unlike kickstart -k, KeepAlive does NOT cause a respawn after bootout.
    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{_uid()}", str(PLIST_PATH)],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        print("hapbeat-helper stopped.")
        print("  Auto-start is still registered — will restart at next login.")
        print("  To restart now: hapbeat-helper install-service")
    else:
        print("hapbeat-helper: could not stop (may already be stopped).")
        print(f"  ({result.stderr.decode().strip()})")


def status() -> str:
    """Return one of: 'not_registered', 'stopped', 'running'."""
    if not PLIST_PATH.exists():
        return "not_registered"
    result = subprocess.run(
        ["launchctl", "list", LABEL],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "stopped"
    # launchctl list output has PID in first column when running
    lines = result.stdout.strip().splitlines()
    if lines and not lines[-1].startswith("-"):
        return "running"
    return "stopped"
