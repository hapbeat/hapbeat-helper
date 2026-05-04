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
    path = shutil.which("hapbeat-helper")
    if not path:
        raise RuntimeError(
            "hapbeat-helper is not on PATH. "
            "Install via pipx: pipx install hapbeat-helper"
        )
    return path


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
    # If already loaded, bootout first (idempotent reinstall)
    if is_registered():
        _bootout()
    _write_plist(exe)
    _bootstrap()
    print(f"hapbeat-helper service installed and started.")
    print(f"  plist: {PLIST_PATH}")
    print(f"  log:   {LOG_PATH}")


def uninstall() -> None:
    _bootout()
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
    print("hapbeat-helper service removed.")


def stop() -> None:
    """Stop the running instance.

    Uses `kickstart -k` (SIGTERM). With KeepAlive=true the launchd job
    will respawn it — that's restart-style semantics. To stop permanently,
    use `uninstall()` instead.
    """
    is_reg = PLIST_PATH.exists()
    if not is_reg:
        # No service registered → kill any helper python the user started
        # in foreground.
        subprocess.run(
            ["pkill", "-f", "hapbeat[-_]helper"], check=False
        )
        print("hapbeat-helper foreground process(es) killed.")
        return
    subprocess.run(
        ["launchctl", "kickstart", "-k", f"gui/{_uid()}/{LABEL}"],
        check=False,
    )
    print("hapbeat-helper restarted (kickstart -k; KeepAlive will respawn).")


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
