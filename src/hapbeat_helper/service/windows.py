"""Windows auto-start for hapbeat-helper.

Primary mechanism: Task Scheduler via PowerShell Register-ScheduledTask.
  - Works on Windows 10 and all Windows 11 versions (including 24H2+ where
    VBScript is disabled by default).
  - Uses ``powershell.exe -WindowStyle Hidden`` so no console window appears.
  - Registered as a per-user logon task; no admin rights needed.
  - Uses ``Register-ScheduledTask`` PowerShell cmdlet (NOT ``schtasks /xml``).
    The XML approach fails with "Access Denied" in some environments due to
    how the <Principal> section is interpreted without an explicit <UserId>.

Fallback mechanism: Startup-folder VBS shim.
  - Used when PowerShell task creation fails (e.g. strict Group Policy).
  - Requires VBScript to be enabled (works on Windows 10 / pre-24H2).
  - Windows 11 24H2+ disables VBScript by default; on those systems the
    Startup-folder shim is dropped as a last resort but will not execute.

Background: Microsoft deprecated VBScript in Windows 11 24H2 (build 26100+).
``wscript.exe`` silently refuses to run ``.vbs`` files unless VBScript is
explicitly re-enabled via "Optional Features". The Task Scheduler approach
uses only PowerShell which is not deprecated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Task Scheduler task name (also used as the legacy task name for cleanup).
TASK_NAME = "HapbeatHelper"

# Legacy Startup-folder VBS shim name.
SHIM_NAME = "HapbeatHelper.vbs"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA environment variable is not set")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _shim_path() -> Path:
    return _startup_dir() / SHIM_NAME


def log_path() -> Path:
    """Where the auto-started helper's stdout/stderr are written."""
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "hapbeat-helper" / "hapbeat-helper.log"


def _hapbeat_helper_path() -> str:
    """Locate hapbeat-helper.exe, preferring the same venv we are running in.

    Resolution order:
    1. Sibling of sys.executable: <sys.executable parent>/hapbeat-helper.exe
       — most reliable; works under git-bash where shutil.which can fail on
       Windows symlinks (e.g. /c/pipx/bin/hapbeat-helper.exe).
    2. shutil.which("hapbeat-helper") — fallback for system-wide installs.
    """
    py_dir = Path(sys.executable).resolve().parent
    candidate = py_dir / "hapbeat-helper.exe"
    if candidate.is_file():
        return str(candidate)

    path = shutil.which("hapbeat-helper") or shutil.which("hapbeat-helper.exe")
    if path:
        return str(Path(path).resolve())

    raise RuntimeError(
        "hapbeat-helper.exe not found next to the current Python "
        f"({sys.executable}) and not on PATH. "
        "Install via pipx: pipx install hapbeat-helper"
    )


# ---------------------------------------------------------------------------
# Task Scheduler (primary)
# ---------------------------------------------------------------------------

def _task_exists() -> bool:
    """Return True if the HapbeatHelper scheduled task is registered."""
    result = subprocess.run(
        ["schtasks", "/query", "/tn", TASK_NAME],
        capture_output=True,
        timeout=10,
    )
    return result.returncode == 0


def _try_create_scheduled_task(exe: str, log: Path) -> bool:
    """Register a per-user logon task via PowerShell Register-ScheduledTask.

    Paths are passed through environment variables to avoid quoting issues.
    The action runs ``powershell.exe -WindowStyle Hidden`` so no console
    window appears. Admin rights are NOT required.

    Returns True on success, False on failure (sets _last_error).
    """
    # The PS1 script uses $env:_HB_EXE / $env:_HB_LOG to receive paths,
    # sidestepping all Python→PowerShell quoting complexity.
    #
    # $arg ends with `"" — breakdown:
    #   `"  = backtick-escaped double-quote → literal " in the string
    #   "   = closing delimiter of the outer PS double-quoted string
    # Result stored in $arg:
    #   -WindowStyle Hidden -NoProfile -Command "& '<exe>' start *>> '<log>'"
    ps1 = (
        '$exe = $env:_HB_EXE\n'
        '$log = $env:_HB_LOG\n'
        '$arg = "-WindowStyle Hidden -NoProfile -Command `"& \'$exe\' start *>> \'$log\'`""\n'
        '$action    = New-ScheduledTaskAction -Execute \'powershell.exe\' -Argument $arg\n'
        '$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME\n'
        '$settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -MultipleInstances IgnoreNew\n'
        '$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited\n'
        'Register-ScheduledTask -TaskName \'' + TASK_NAME + '\' '
        '-Action $action -Trigger $trigger -Settings $settings '
        '-Principal $principal -Force -ErrorAction Stop\n'
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", delete=False, encoding="utf-8"
    ) as f:
        f.write(ps1)
        tmp = f.name

    env = os.environ.copy()
    env["_HB_EXE"] = exe
    env["_HB_LOG"] = str(log)

    try:
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile",
                "-ExecutionPolicy", "Bypass",
                "-File", tmp,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        if result.returncode != 0:
            _try_create_scheduled_task._last_error = (
                (result.stdout + result.stderr).strip()
            )
        return result.returncode == 0
    finally:
        Path(tmp).unlink(missing_ok=True)


_try_create_scheduled_task._last_error = ""  # type: ignore[attr-defined]


def _run_task_now() -> None:
    """Trigger the registered task to run immediately (best-effort)."""
    subprocess.run(
        ["schtasks", "/run", "/tn", TASK_NAME],
        capture_output=True,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# VBS shim (fallback)
# ---------------------------------------------------------------------------

def _build_vbs(exe: str, log: Path) -> str:
    # VBS string-literal escaping: a literal " inside a string is written
    # as "" (two double-quotes).
    cmd = f'cmd /c ""{exe}"" start >> ""{log}"" 2>&1'
    return (
        "' Auto-generated by `hapbeat-helper install-service`.\r\n"
        "' Launches hapbeat-helper hidden at user logon and writes\r\n"
        f"' stdout/stderr to: {log}\r\n"
        "' NOTE: requires VBScript to be enabled (Windows 10 / pre-24H2).\r\n"
        "' On Windows 11 24H2+ VBScript is disabled by default and this\r\n"
        "' shim will silently fail. Use Task Scheduler instead.\r\n"
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run "{cmd}", 0, False\r\n'
    )


# ---------------------------------------------------------------------------
# Process detection
# ---------------------------------------------------------------------------

def _running_pids() -> list[int]:
    """PIDs of python.exe / hapbeat-helper.exe running as hapbeat-helper."""
    try:
        out = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process "
                    "-Filter \"Name='python.exe' OR Name='pythonw.exe' "
                    "OR Name='hapbeat-helper.exe'\" "
                    "| Where-Object { $_.CommandLine -match 'hapbeat[-_]helper' } "
                    "| Select-Object -ExpandProperty ProcessId"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if out.returncode != 0 or not out.stdout.strip():
        return []
    pids: list[int] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install() -> None:
    exe = _hapbeat_helper_path()
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)

    used_task_scheduler = False

    # --- Primary: Task Scheduler ---
    if _try_create_scheduled_task(exe, log):
        used_task_scheduler = True
        # Also remove any leftover VBS shim from a previous install.
        shim = _shim_path()
        if shim.exists():
            shim.unlink()
        # Start immediately — no need to log out / back in.
        _run_task_now()
        print("hapbeat-helper auto-start installed (Task Scheduler).")
        print(f"  task: {TASK_NAME}")
        print(f"  exe:  {exe}")
        print(f"  log:  {log}")
        print("  Helper is starting now. No need to log out and back in.")
    else:
        err = getattr(_try_create_scheduled_task, "_last_error", "")
        print(
            f"Task Scheduler registration failed ({err.strip() or 'unknown error'}).\n"
            "Falling back to Startup-folder VBS shim.\n"
            "Note: On Windows 11 24H2+ VBScript is disabled by default and the\n"
            "shim will not execute at logon. To re-enable VBScript, open\n"
            "Settings → Apps → Optional Features → Add a feature → 'VBSCRIPT'."
        )

    # --- Fallback: VBS shim ---
    if not used_task_scheduler:
        startup = _startup_dir()
        startup.mkdir(parents=True, exist_ok=True)
        shim = _shim_path()
        shim.write_text(_build_vbs(exe, log), encoding="utf-8")
        # Try to launch immediately; may silently do nothing on 24H2+.
        subprocess.Popen(
            ["wscript.exe", str(shim)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("hapbeat-helper auto-start installed (VBS shim).")
        print(f"  shim: {shim}")
        print(f"  exe:  {exe}")
        print(f"  log:  {log}")


def uninstall() -> None:
    removed_something = False

    # Remove Task Scheduler entry.
    if _task_exists():
        subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            capture_output=True,
            timeout=10,
        )
        print(f"removed Task Scheduler task: {TASK_NAME}")
        removed_something = True

    # Remove VBS shim (migration / fallback cleanup).
    shim = _shim_path()
    if shim.exists():
        shim.unlink()
        print(f"removed VBS shim: {shim}")
        removed_something = True

    if not removed_something:
        print("hapbeat-helper auto-start was not registered.")

    # Stop the currently-running instance.
    pids = _running_pids()
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    if pids:
        print(f"stopped running helper (pid={','.join(str(p) for p in pids)})")
    print("hapbeat-helper auto-start removed.")


def stop() -> None:
    """Kill any running hapbeat-helper instance (without removing registration)."""
    pids = _running_pids()
    if not pids:
        print("hapbeat-helper is not running.")
        return
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    print(f"hapbeat-helper stopped (pid={','.join(str(p) for p in pids)}).")


def status() -> str:
    """Return one of: 'not_registered', 'stopped', 'running'."""
    task_reg = _task_exists()
    shim_reg = _shim_path().exists()
    if not task_reg and not shim_reg:
        return "not_registered"
    return "running" if _running_pids() else "stopped"
