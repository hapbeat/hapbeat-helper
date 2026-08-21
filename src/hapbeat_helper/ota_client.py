"""CLI-side OTA driver for ``hapbeat-helper ota <target> <bin>``.

Two paths, picked by whether a daemon is listening on the WebSocket port:

* **daemon running (normal case)** — drive the OTA through the daemon's WS
  API (``list_devices`` → ``ota_data``).  This is the only correct path
  while a daemon is up: the daemon owns the per-IP TCP lock, the
  ``_ota_in_progress`` guard and the log_tail suppression.  Opening our own
  TCP 7701 connection would fight the daemon for the device's single
  command slot.
* **no daemon** — call ``server._do_ota_to_device`` directly.  That
  function is module-level and stateless (it only opens a
  ``TcpRawConnection``), so it is safe to use standalone.  Name lookup is
  not available in this mode because device discovery lives in the daemon.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import socket
import sys
from pathlib import Path
from typing import Optional

# ── Image validation ─────────────────────────────────────────
# Ported from Studio's utils/otaImageValidation.ts so the CLI rejects the
# same bad images the GUI does (notably a merged serial image, which would
# be written into the OTA slot as if it were the app).

ESP_IMAGE_MAGIC = 0xE9
# Every chip id we ship firmware for.
_KNOWN_CHIP_IDS = {0x00, 0x02, 0x05, 0x09, 0x0C, 0x0D, 0x10}
_CHIP_NAMES = {
    0x00: "ESP32", 0x02: "ESP32-S2", 0x05: "ESP32-C3", 0x09: "ESP32-S3",
    0x0C: "ESP32-C2", 0x0D: "ESP32-C6", 0x10: "ESP32-H2",
}
_MIN_APP_BYTES = 100 * 1024
_MAX_APP_BYTES = 2 * 1024 * 1024
# Partition-table magic (0xAA 0x50) sits at 0x8000 in a merged image.
_PARTITION_TABLE_OFFSET = 0x8000


def validate_ota_image(data: bytes) -> Optional[str]:
    """Return a human-readable reason the image is unusable, or ``None``.

    A merged image (``firmware_full_serial.bin``) also starts with 0xE9 —
    its first bytes are the *bootloader*'s header — so the magic byte alone
    does not separate the two.  The partition table at 0x8000 does.
    """
    if len(data) < _MIN_APP_BYTES:
        return (
            f"file is too small ({len(data):,} bytes); expected an app image "
            "of at least 100 KB"
        )
    if len(data) > _MAX_APP_BYTES:
        return (
            f"file is too large ({len(data):,} bytes) for the OTA partition; "
            "this looks like a merged image; pass the app-only .bin"
        )
    if data[0] != ESP_IMAGE_MAGIC:
        return (
            f"first byte is 0x{data[0]:02X} (expected 0xE9); this is not an "
            "ESP32 application image"
        )
    if (len(data) > _PARTITION_TABLE_OFFSET + 1
            and data[_PARTITION_TABLE_OFFSET] == 0xAA
            and data[_PARTITION_TABLE_OFFSET + 1] == 0x50):
        return (
            "this is a merged image (bootloader + partitions + app); OTA "
            "takes the app part only; use firmware_app_ota.bin"
        )
    chip_id = data[12]
    if chip_id not in _KNOWN_CHIP_IDS:
        return (
            f"unknown chip id in image header (0x{chip_id:02X}); the file may "
            "be corrupt"
        )
    segments = data[1]
    if segments == 0 or segments > 16:
        return (
            f"image header segment count is invalid ({segments}); the file may "
            "be corrupt"
        )
    return None


def describe_image(data: bytes) -> str:
    chip_id = data[12]
    name = _CHIP_NAMES.get(chip_id, f"unknown (0x{chip_id:02X})")
    return f"{len(data):,} bytes, {name}"


def load_ota_image(path: str) -> tuple[Optional[bytes], Optional[str]]:
    """Read *path* and validate it. Returns ``(bytes, None)`` or ``(None, err)``."""
    p = Path(path)
    if not p.is_file():
        return None, f"no such file: {p}"
    try:
        data = p.read_bytes()
    except OSError as exc:
        return None, f"cannot read {p}: {exc}"
    reason = validate_ota_image(data)
    if reason:
        return None, f"{p.name}: {reason}"
    return data, None


# ── Target resolution ────────────────────────────────────────


def is_ip_address(target: str) -> bool:
    try:
        ipaddress.ip_address(target)
        return True
    except ValueError:
        return False


def resolve_target(
    devices: list[dict], target: str,
) -> tuple[Optional[str], Optional[str]]:
    """Map ``<target>`` (IP or device name) to a single IP.

    Returns ``(ip, None)`` or ``(None, error_message)``.  Never picks one of
    several same-named devices — an ambiguous name is an error, because an
    OTA to the wrong device is not undoable from the CLI.
    """
    if is_ip_address(target):
        return target, None

    matches = [
        d for d in devices
        if str(d.get("name", "")).lower() == target.lower()
    ]
    if not matches:
        return None, (
            f"no device named {target!r}.\n{_format_candidates(devices)}"
        )
    if len(matches) > 1:
        ips = ", ".join(str(d.get("ipAddress", "?")) for d in matches)
        return None, (
            f"{len(matches)} devices are named {target!r} ({ips}). "
            "Pass an IP address instead."
        )
    dev = matches[0]
    ip = str(dev.get("ipAddress", ""))
    if not ip:
        return None, f"device {target!r} has no IP address yet"
    if not dev.get("online", False):
        return None, f"device {target!r} ({ip}) is offline"
    return ip, None


def _format_candidates(devices: list[dict]) -> str:
    if not devices:
        return "known devices: (none; the helper has not discovered any yet)"
    lines = ["known devices:"]
    for d in devices:
        state = "online" if d.get("online") else "offline"
        lines.append(
            f"  {str(d.get('name', '')) or '(unnamed)'}"
            f"  {d.get('ipAddress', '?')}  [{state}]"
        )
    return "\n".join(lines)


# ── Daemon reachability ──────────────────────────────────────


def daemon_reachable(port: int, host: str = "127.0.0.1") -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


# ── WS path ──────────────────────────────────────────────────

# The daemon itself bounds a single OTA at 600 s; allow a little more so its
# own ota_result (with the real reason) is what we report.
_WS_OVERALL_TIMEOUT = 660.0


class _ProgressPrinter:
    """One-line, overwriting progress; falls back to plain lines in verbose."""

    def __init__(self, verbose: bool, stream=None) -> None:
        self.verbose = verbose
        self.stream = stream or sys.stdout
        self._dirty = False

    def update(self, percent: int, message: str) -> None:
        if self.verbose:
            self.stream.write(f"[progress] {percent:3d}% {message}\n")
        else:
            line = f"  {percent:3d}%  {message}"
            self.stream.write("\r" + line.ljust(78)[:78])
            self._dirty = True
        self.stream.flush()

    def finish(self) -> None:
        if self._dirty:
            self.stream.write("\n")
            self.stream.flush()
            self._dirty = False


async def run_ota_via_ws(
    port: int, target: str, bin_bytes: bytes, verbose: bool,
) -> int:
    """Drive the OTA through a running daemon. Returns a process exit code."""
    import websockets

    uri = f"ws://127.0.0.1:{port}"
    try:
        ws = await websockets.connect(uri, max_size=64 * 1024 * 1024)
    except OSError as exc:
        print(f"error: cannot connect to {uri}: {exc}", file=sys.stderr)
        return 1

    async with ws:
        # The daemon pushes helper_hello + device_list on connect; ask
        # explicitly too so we do not depend on that ordering.
        await ws.send(json.dumps({"type": "list_devices", "payload": {}}))
        devices = await _await_device_list(ws, verbose)
        if devices is None:
            print("error: helper did not send a device list", file=sys.stderr)
            return 1

        ip, err = resolve_target(devices, target)
        if err:
            print(f"error: {err}", file=sys.stderr)
            return 2

        print(f"OTA -> {ip}  ({describe_image(bin_bytes)})")
        await ws.send(json.dumps({
            "type": "ota_data",
            "payload": {
                "ip": ip,
                "bin_base64": base64.b64encode(bin_bytes).decode("ascii"),
            },
        }))
        return await _consume_ota_events(ws, ip, verbose)


async def _recv_until(ws, deadline: float):
    """``ws.recv()`` bounded by an absolute monotonic *deadline*.

    ``asyncio.wait_for`` rather than ``asyncio.timeout`` — the latter is
    3.11+, and this package supports 3.10.
    """
    import asyncio
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return await asyncio.wait_for(ws.recv(), timeout=remaining)


async def _await_device_list(ws, verbose: bool) -> Optional[list[dict]]:
    import asyncio
    deadline = asyncio.get_running_loop().time() + 10.0
    try:
        while True:
            raw = await _recv_until(ws, deadline)
            msg = _parse(raw)
            if msg is None:
                continue
            if verbose:
                print(f"[recv] {msg.get('type')}")
            if msg.get("type") == "device_list":
                payload = msg.get("payload") or {}
                devices = payload.get("devices")
                return devices if isinstance(devices, list) else []
    except asyncio.TimeoutError:
        return None


async def _consume_ota_events(ws, ip: str, verbose: bool) -> int:
    import asyncio
    printer = _ProgressPrinter(verbose)
    deadline = asyncio.get_running_loop().time() + _WS_OVERALL_TIMEOUT
    try:
        while True:
            raw = await _recv_until(ws, deadline)
            msg = _parse(raw)
            if msg is None:
                continue
            mtype = msg.get("type")
            payload = msg.get("payload") or {}
            if verbose and mtype not in ("ota_progress", "ota_result"):
                print(f"[recv] {mtype}")
            if payload.get("device") != ip:
                continue
            if mtype == "ota_progress":
                printer.update(
                    int(payload.get("percent", 0) or 0),
                    str(payload.get("message", "")),
                )
            elif mtype == "ota_result":
                printer.finish()
                ok = bool(payload.get("success"))
                message = str(payload.get("message", ""))
                if ok:
                    print(f"OK  {ip}: {message or 'OTA complete'}")
                    return 0
                print(f"FAILED  {ip}: {message}", file=sys.stderr)
                return 1
    except asyncio.TimeoutError:
        printer.finish()
        print(
            f"error: no result from helper within "
            f"{_WS_OVERALL_TIMEOUT:.0f}s; check `hapbeat-helper logs`",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 — connection dropped mid-OTA
        printer.finish()
        print(f"error: connection to helper lost: {exc}", file=sys.stderr)
        return 1


def _parse(raw) -> Optional[dict]:
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return msg if isinstance(msg, dict) else None


# ── Direct path (no daemon) ──────────────────────────────────


def run_ota_direct(ip: str, bin_bytes: bytes, verbose: bool) -> int:
    """Stream the image straight to the device — only when no daemon runs.

    ``server._do_ota_to_device`` is module-level and holds no daemon state
    (it opens its own ``TcpRawConnection``), so calling it here does not
    reach into a live server object.
    """
    from hapbeat_helper.server import _do_ota_to_device

    printer = _ProgressPrinter(verbose)

    def progress(phase: str, percent: int, message: str) -> None:
        del phase
        printer.update(percent, message)

    print(f"OTA -> {ip}  ({describe_image(bin_bytes)})  [helper not running]")
    ok, message = _do_ota_to_device(ip, bin_bytes, progress)
    printer.finish()
    if ok:
        print(f"OK  {ip}: {message or 'OTA complete'}")
        return 0
    print(f"FAILED  {ip}: {message}", file=sys.stderr)
    return 1
