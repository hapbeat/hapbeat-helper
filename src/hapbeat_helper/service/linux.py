"""Linux systemd --user service management for hapbeat-helper."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SERVICE_NAME = "hapbeat-helper.service"
SERVICE_PATH = Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME


def _hapbeat_helper_path() -> str:
    path = shutil.which("hapbeat-helper")
    if not path:
        raise RuntimeError(
            "hapbeat-helper is not on PATH. "
            "Install via pipx: pipx install hapbeat-helper"
        )
    return path


def _write_unit(exe: str) -> None:
    SERVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    unit = f"""\
[Unit]
Description=Hapbeat Helper daemon
After=network.target

[Service]
ExecStart={exe} start --foreground
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""
    SERVICE_PATH.write_text(unit, encoding="utf-8")


def install() -> None:
    exe = _hapbeat_helper_path()
    _write_unit(exe)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", SERVICE_NAME], check=True)
    print("hapbeat-helper service installed and started.")
    print(f"  unit: {SERVICE_PATH}")
    print()
    print("To persist across logouts (optional):")
    print(f"  loginctl enable-linger $(whoami)")


def uninstall() -> None:
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", SERVICE_NAME],
        check=False,
    )
    if SERVICE_PATH.exists():
        SERVICE_PATH.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print("hapbeat-helper service removed.")


def status() -> str:
    """Return one of: 'not_registered', 'stopped', 'running'."""
    if not SERVICE_PATH.exists():
        return "not_registered"
    result = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE_NAME],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip() == "active":
        return "running"
    return "stopped"
