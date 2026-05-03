"""Windows Task Scheduler service management for hapbeat-helper."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

TASK_NAME = "HapbeatHelper"


def _hapbeat_helper_path() -> str:
    path = shutil.which("hapbeat-helper")
    if not path:
        raise RuntimeError(
            "hapbeat-helper is not on PATH. "
            "Install via pipx: pipx install hapbeat-helper"
        )
    return str(Path(path).resolve())


def _pythonw_wrapper_path(exe: str) -> str:
    """Return the pythonw.exe path co-located with the helper's python."""
    # pipx shims are .cmd wrappers — unwrap to the real python venv
    py = shutil.which("pythonw") or shutil.which("python")
    return py or exe


def install() -> None:
    exe = _hapbeat_helper_path()
    # Use pythonw.exe via -m so no console window appears.
    # hapbeat-helper pipx shim is a .exe on Windows; run it with a hidden
    # PowerShell wrapper to suppress the console window.
    ps_cmd = (
        f"Start-Process -WindowStyle Hidden "
        f"-FilePath '{exe}' "
        f"-ArgumentList 'start','--foreground'"
    )
    # Uninstall existing task first (idempotent)
    subprocess.run(
        ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
        capture_output=True,
    )
    result = subprocess.run(
        [
            "schtasks",
            "/create",
            "/tn", TASK_NAME,
            "/sc", "onlogon",
            "/tr",
            (
                f"powershell.exe -WindowStyle Hidden -Command "
                f"\"Start-Process -WindowStyle Hidden "
                f"-FilePath '{exe}' "
                f"-ArgumentList 'start','--foreground'\""
            ),
            "/rl", "highest",
            "/f",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"schtasks /create failed: {result.stderr.strip()}"
        )
    # Trigger the task immediately so user does not have to log out/in
    subprocess.run(["schtasks", "/run", "/tn", TASK_NAME], check=False)
    print("hapbeat-helper service installed and started.")
    print(f"  Task Scheduler entry: {TASK_NAME}")
    print(f"  executable: {exe}")


def uninstall() -> None:
    result = subprocess.run(
        ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 and "not exist" not in result.stderr.lower():
        raise RuntimeError(
            f"schtasks /delete failed: {result.stderr.strip()}"
        )
    # Kill any running instance
    subprocess.run(
        ["schtasks", "/end", "/tn", TASK_NAME],
        capture_output=True,
    )
    print("hapbeat-helper service removed.")


def status() -> str:
    """Return one of: 'not_registered', 'stopped', 'running'."""
    result = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return "not_registered"
    output = result.stdout
    # "Status:" line contains "Running" when active
    for line in output.splitlines():
        if line.strip().lower().startswith("status:"):
            val = line.split(":", 1)[-1].strip().lower()
            if "running" in val:
                return "running"
            return "stopped"
    return "stopped"
