"""mDNS device discovery for Hapbeat devices.

Browses ``_hapbeat._udp.local.`` via zeroconf and forwards discovered
devices to the registered callbacks. The companion :class:`UdpListener`
handles all UDP/PONG traffic, so this module is mDNS-only.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from zeroconf import (
    ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf,
)

logger = logging.getLogger(__name__)

HAPBEAT_SERVICE_TYPE = "_hapbeat._udp.local."

DeviceCallback = Callable[[dict], None]
RemovedCallback = Callable[[str], None]


class MdnsScanner:
    """Continuously browses for Hapbeat services via mDNS."""

    def __init__(self) -> None:
        self._zeroconf: Optional[Zeroconf] = None
        self._browser: Optional[ServiceBrowser] = None
        self._known: dict[str, dict] = {}  # ip -> info
        self._lock = threading.Lock()
        self._found_callbacks: list[DeviceCallback] = []
        self._removed_callbacks: list[RemovedCallback] = []

    # ── Listener API ─────────────────────────────────────────

    def add_found_listener(self, cb: DeviceCallback) -> None:
        self._found_callbacks.append(cb)

    def add_removed_listener(self, cb: RemovedCallback) -> None:
        self._removed_callbacks.append(cb)

    # ── Lifecycle ────────────────────────────────────────────

    def start(self) -> None:
        if self._zeroconf is not None:
            return
        self._zeroconf = Zeroconf()
        self._browser = ServiceBrowser(
            self._zeroconf,
            HAPBEAT_SERVICE_TYPE,
            handlers=[self._on_state_change],
        )
        logger.info("mDNS browsing started for %s", HAPBEAT_SERVICE_TYPE)

    def stop(self) -> None:
        if self._browser is not None:
            self._browser.cancel()
            self._browser = None
        if self._zeroconf is not None:
            self._zeroconf.close()
            self._zeroconf = None

    # ── Query ────────────────────────────────────────────────

    def get_devices(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._known)

    # ── Internal ─────────────────────────────────────────────

    def _on_state_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change == ServiceStateChange.Added:
            info = zeroconf.get_service_info(service_type, name)
            if info is not None:
                self._handle_added(info)
        elif state_change == ServiceStateChange.Removed:
            self._handle_removed(name)

    def _handle_added(self, info: ServiceInfo) -> None:
        addresses = info.parsed_addresses()
        if not addresses:
            return
        ip = addresses[0]

        txt: dict[str, str] = {}
        if info.properties:
            for key_bytes, val_bytes in info.properties.items():
                key = (
                    key_bytes.decode("utf-8", errors="replace")
                    if isinstance(key_bytes, bytes) else str(key_bytes)
                )
                val = (
                    val_bytes.decode("utf-8", errors="replace")
                    if isinstance(val_bytes, bytes)
                    else str(val_bytes) if val_bytes is not None else ""
                )
                txt[key] = val

        device_info: dict = {
            "ip": ip,
            "port": info.port,
            "name": txt.get("name", info.name),
            "group": int(txt.get("group", "0") or "0"),
            "firmware": txt.get("fw", ""),
            "mac": txt.get("mac", ""),
            "discovered_via": "mdns",
        }
        with self._lock:
            self._known[ip] = device_info

        logger.info(
            "mDNS: %s @ %s", device_info["name"] or "(unnamed)", ip,
        )
        for cb in list(self._found_callbacks):
            try:
                cb(device_info)
            except Exception:  # noqa: BLE001
                logger.exception("mdns found listener failed")

    def _handle_removed(self, name: str) -> None:
        removed_ip: Optional[str] = None
        with self._lock:
            for ip, dev in self._known.items():
                if dev.get("name") == name or name.startswith(
                    dev.get("name", "\x00"),
                ):
                    removed_ip = ip
                    break
            if removed_ip is not None:
                del self._known[removed_ip]

        if removed_ip is not None:
            logger.info("mDNS removed: %s", name)
            for cb in list(self._removed_callbacks):
                try:
                    cb(removed_ip)
                except Exception:  # noqa: BLE001
                    logger.exception("mdns removed listener failed")
