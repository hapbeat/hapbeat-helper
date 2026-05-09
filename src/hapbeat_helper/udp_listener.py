"""Persistent UDP listener for Hapbeat Layer 1 traffic.

Owns UDP port 7700: every PONG (PING reply or async push) flows through
this single socket so nothing else contends for the port. PLAY / STOP /
STREAM_* sends from the WebSocket layer are also routed through here so
the broadcast socket option survives.

A background thread does the recv loop; parsed PONGs are dispatched to
registered callbacks (the WebSocket server hands them off to the asyncio
loop with ``loop.call_soon_threadsafe``).
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable, Optional

from hapbeat_helper import protocol

logger = logging.getLogger(__name__)

HAPBEAT_UDP_PORT = 7700

PongCallback = Callable[[dict, str], None]
RttCallback = Callable[[str, float], None]


class UdpListener:
    """Sole owner of UDP port 7700."""

    def __init__(self, port: int = HAPBEAT_UDP_PORT) -> None:
        self._port = port
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        # seq -> (ip, perf_counter when sent) for RTT correlation
        self._pending_pings: dict[int, tuple[str, float]] = {}
        self._lock = threading.Lock()

        self._pong_callbacks: list[PongCallback] = []
        self._rtt_callbacks: list[RttCallback] = []

    # ── Listener API ─────────────────────────────────────────

    def add_pong_listener(self, cb: PongCallback) -> None:
        self._pong_callbacks.append(cb)

    def add_rtt_listener(self, cb: RttCallback) -> None:
        self._rtt_callbacks.append(cb)

    def remove_rtt_listener(self, cb: RttCallback) -> None:
        """Best-effort: silently no-op if the callback wasn't registered."""
        try:
            self._rtt_callbacks.remove(cb)
        except ValueError:
            pass

    # ── Lifecycle ────────────────────────────────────────────

    def start(self) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            pass
        try:
            sock.bind(("0.0.0.0", self._port))
        except OSError as exc:
            logger.error(
                "UDP listener bind failed on port %d: %s", self._port, exc,
            )
            sock.close()
            return False

        sock.settimeout(0.2)
        self._sock = sock
        self._running = True
        self._thread = threading.Thread(
            target=self._recv_loop,
            daemon=True,
            name="udp-listener",
        )
        self._thread.start()
        logger.info("UDP listener started on 0.0.0.0:%d", self._port)
        return True

    def stop(self) -> None:
        self._running = False
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    # ── Send API ─────────────────────────────────────────────

    def send_ping(self, target_ip: str) -> int:
        sock = self._sock
        if sock is None:
            return -1
        seq = int(time.monotonic_ns() // 1000) & 0xFFFF
        ts_us = int(time.time() * 1_000_000)
        pkt = protocol.build_ping(seq, ts_us)
        with self._lock:
            self._pending_pings[seq] = (target_ip, time.perf_counter())
        try:
            sock.sendto(pkt, (target_ip, self._port))
        except OSError as exc:
            logger.warning("UDP send_ping(%s) failed: %s", target_ip, exc)
            with self._lock:
                self._pending_pings.pop(seq, None)
            return -1
        return seq

    def send_broadcast_ping(self) -> int:
        sock = self._sock
        if sock is None:
            return -1
        seq = int(time.monotonic_ns() // 1000) & 0xFFFF
        ts_us = int(time.time() * 1_000_000)
        pkt = protocol.build_ping(seq, ts_us)
        try:
            sock.sendto(pkt, ("255.255.255.255", self._port))
        except OSError as exc:
            logger.warning("UDP broadcast_ping failed: %s", exc)
            return -1
        return seq

    def send_raw(self, data: bytes, target_ip: str) -> bool:
        """Send arbitrary L1 packet through the listener socket."""
        sock = self._sock
        if sock is None:
            return False
        dst = (
            "255.255.255.255" if target_ip in ("<broadcast>", "")
            else target_ip
        )
        try:
            sock.sendto(data, (dst, self._port))
            return True
        except OSError as exc:
            logger.warning("UDP send_raw to %s failed: %s", dst, exc)
            return False

    # ── Recv loop ────────────────────────────────────────────

    def _recv_loop(self) -> None:
        while self._running:
            sock = self._sock
            if sock is None:
                break
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            ip = addr[0]
            pong = protocol.parse_pong(data)
            if pong is None:
                continue

            seq = pong.get("seq")
            if seq is not None:
                with self._lock:
                    pending = self._pending_pings.pop(seq, None)
                if pending is not None:
                    _, t0 = pending
                    rtt_ms = (time.perf_counter() - t0) * 1000
                    self._dispatch_rtt(ip, rtt_ms)

            self._dispatch_pong(pong, ip)

    def _dispatch_pong(self, pong: dict, ip: str) -> None:
        for cb in list(self._pong_callbacks):
            try:
                cb(pong, ip)
            except Exception:  # noqa: BLE001
                logger.exception("pong listener failed")

    def _dispatch_rtt(self, ip: str, rtt_ms: float) -> None:
        for cb in list(self._rtt_callbacks):
            try:
                cb(ip, rtt_ms)
            except Exception:  # noqa: BLE001
                logger.exception("rtt listener failed")
