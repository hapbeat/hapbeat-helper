"""Central device registry — single source of truth for device state.

Helper edition: no Qt, no serial. USB serial setup happens in Studio
(via Web Serial API) and is not surfaced here.

Listeners register a callable that is invoked synchronously after every
mutation; thread-safety is the listener's responsibility (the helper
WebSocket layer marshals calls onto the asyncio loop).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Liveness window. Scan loop pings every 2s (see server._scan_loop),
# so a device is treated as offline after ~2 missed PONGs (5s window).
# Manager parity: Manager uses 2s scan / 8s threshold; we run a touch
# tighter so power-off shows up in the Studio UI within ~5s instead
# of the 12s the previous "5s/12s" pair allowed.
_OFFLINE_THRESHOLD = 5.0


@dataclass
class HapbeatDevice:
    ip: str = ""
    name: str = ""
    mac: str = ""
    address: str = ""  # path-based address e.g. "player_1/chest"
    firmware: str = ""

    last_seen: float = 0.0  # time.monotonic()
    discovered_via: str = ""  # "mdns", "udp_broadcast"

    volume_level: Optional[int] = None
    volume_wiper: Optional[int] = None
    volume_steps: Optional[int] = None

    @property
    def is_online(self) -> bool:
        if self.last_seen == 0.0:
            return False
        return (time.monotonic() - self.last_seen) < _OFFLINE_THRESHOLD


class DeviceRegistry:
    """Thread-safe registry of known Hapbeat devices."""

    def __init__(self) -> None:
        self._devices: dict[str, HapbeatDevice] = {}
        self._lock = threading.Lock()
        self._listeners: list[Callable[[], None]] = []

    # ── Listener API ─────────────────────────────────────────

    def add_change_listener(self, callback: Callable[[], None]) -> None:
        """Register a callback fired after any device-state mutation.

        The callback runs on whatever thread caused the mutation, so the
        callback itself must be safe to invoke from background threads.
        """
        self._listeners.append(callback)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                logger.exception("device_registry listener failed")

    # ── Mutation API ─────────────────────────────────────────

    def upsert_device(self, info: dict) -> None:
        """Add or update a device from discovery info."""
        ip = info.get("ip", "")
        if not ip:
            return

        with self._lock:
            dev = self._devices.get(ip)
            if dev is None:
                dev = HapbeatDevice(ip=ip)
                self._devices[ip] = dev
            dev.name = info.get("name", dev.name) or dev.name
            dev.mac = info.get("mac", dev.mac) or dev.mac
            dev.address = info.get("address", dev.address) or dev.address
            dev.firmware = info.get("firmware", dev.firmware) or dev.firmware
            dev.last_seen = time.monotonic()
            dev.discovered_via = info.get("discovered_via", dev.discovered_via)

            # Optional volume fields (PONG carries these from 2026-04-12+)
            for key in ("volume_level", "volume_wiper", "volume_steps"):
                if key in info and info[key] is not None:
                    setattr(dev, key, info[key])

        self._notify()

    def update_volume(
        self, ip: str, level: Optional[int], wiper: Optional[int],
        steps: Optional[int],
    ) -> None:
        with self._lock:
            dev = self._devices.get(ip)
            if dev is None:
                return
            if level is not None:
                dev.volume_level = level
            if wiper is not None:
                dev.volume_wiper = wiper
            if steps is not None:
                dev.volume_steps = steps
        self._notify()

    # ── Query API ────────────────────────────────────────────

    def get_all_devices(self) -> dict[str, HapbeatDevice]:
        with self._lock:
            return dict(self._devices)

    def get_device(self, ip: str) -> Optional[HapbeatDevice]:
        with self._lock:
            return self._devices.get(ip)

    def get_all_ips(self) -> list[str]:
        with self._lock:
            return list(self._devices.keys())
