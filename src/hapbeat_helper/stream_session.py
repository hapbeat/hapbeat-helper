"""WifiUdp stream session v2 lease tracking with a per-device legacy fallback.

Implements hapbeat-contracts/specs/stream-session-v2.md for the helper:

- every PING carries this process's client incarnation (16-byte payload);
- a device is classified only from *matched* direct replies to our own PING
  (seq, echoed timestamp and incarnation match, timestamp newer than the last
  accepted reply): a valid HBS2 tail means v2, no tail means pre-v2 firmware
  (legacy), a malformed tail is ignored;
- v2 sessions carry (boot id, lease ticket, generation) and need no cooldown;
- legacy sessions use the v1 stream format with a 300 ms END->BEGIN guard for
  that device only (DEC-075).

Pure logic plus a lock: the UDP recv thread feeds PONGs, the asyncio loop reads.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

PENDING_TTL_S = 15.0
LEGACY_END_TO_BEGIN_S = 0.3

UNKNOWN = "unknown"
V2 = "v2"
LEGACY = "legacy"


@dataclass(frozen=True)
class StreamIdentity:
    boot_id: int
    ticket: int
    generation: int


@dataclass
class _Endpoint:
    kind: str = UNKNOWN
    boot_id: int = 0
    ticket: int = 0
    lease_valid: bool = False
    superseded: bool = False
    last_timestamp_us: int = 0


@dataclass
class _Pending:
    timestamp_us: int
    incarnation: int
    sent_at: float


def _new_incarnation() -> int:
    while True:
        value = secrets.randbits(64)
        if value:
            return value


@dataclass
class StreamLeaseTable:
    clock: Callable[[], float] = time.monotonic
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _incarnation: int = field(default_factory=_new_incarnation)
    _pending: dict[int, _Pending] = field(default_factory=dict)
    _endpoints: dict[str, _Endpoint] = field(default_factory=dict)
    _next_generation: dict[tuple[int, int], int] = field(default_factory=dict)
    _legacy_ended_at: dict[str, float] = field(default_factory=dict)

    @property
    def incarnation(self) -> int:
        return self._incarnation

    def note_ping(self, seq: int, timestamp_us: int) -> int:
        """Record an outgoing PING; returns the incarnation to put in it.

        Entries live PENDING_TTL_S so every device answering one broadcast can
        be correlated (a reply must not consume the entry).
        """
        now = self.clock()
        with self._lock:
            for stale in [s for s, p in self._pending.items() if now - p.sent_at > PENDING_TTL_S]:
                del self._pending[stale]
            self._pending[seq] = _Pending(timestamp_us, self._incarnation, now)
            return self._incarnation

    def renew_incarnation(self) -> None:
        """Explicit stream-ownership reacquisition (e.g. the user starts a
        stream while this helper is superseded). v2 state resets to unknown;
        legacy classification survives because old firmware has no lease."""
        with self._lock:
            self._incarnation = _new_incarnation()
            self._pending.clear()
            for endpoint in self._endpoints.values():
                if endpoint.kind != LEGACY:
                    endpoint.kind = UNKNOWN
                    endpoint.lease_valid = False
                    endpoint.superseded = False

    def on_pong(self, ip: str, pong: dict) -> Optional[str]:
        """Classify ``ip`` from a parsed PONG. Returns the new kind when the
        class changed, else None. Unmatched/unsolicited replies change nothing."""
        timestamp = pong.get("timestamp", 0)
        tail = pong.get("stream_tail") or {"status": "absent"}
        with self._lock:
            pending = self._pending.get(pong.get("seq"))
            endpoint = self._endpoints.setdefault(ip, _Endpoint())
            if (not timestamp or pending is None or pending.timestamp_us != timestamp
                    or pending.incarnation != self._incarnation
                    or timestamp <= endpoint.last_timestamp_us):
                return None
            status = tail.get("status")
            if status == "malformed":
                return None
            previous = endpoint.kind
            if status == "valid":
                if tail.get("echoed_incarnation") != pending.incarnation:
                    return None
                endpoint.kind = V2
                endpoint.boot_id = tail["boot_id"]
                endpoint.ticket = tail["ticket"]
                endpoint.lease_valid = bool(tail["lease_valid"])
                endpoint.superseded = bool(tail["superseded"])
            else:
                endpoint.kind = LEGACY
                endpoint.lease_valid = False
                endpoint.superseded = False
            endpoint.last_timestamp_us = timestamp
            return endpoint.kind if endpoint.kind != previous else None

    def kind(self, ip: str) -> str:
        with self._lock:
            endpoint = self._endpoints.get(ip)
            return endpoint.kind if endpoint else UNKNOWN

    def is_superseded(self, ip: str) -> bool:
        with self._lock:
            endpoint = self._endpoints.get(ip)
            return bool(endpoint and endpoint.kind == V2 and endpoint.superseded)

    @staticmethod
    def _format_locked(endpoint: Optional[_Endpoint]) -> Optional[str]:
        if endpoint is None:
            return None
        if endpoint.kind == LEGACY:
            return LEGACY
        if (endpoint.kind != V2 or not endpoint.lease_valid or endpoint.superseded
                or not endpoint.boot_id or not endpoint.ticket):
            return None
        return V2

    def begin_format(self, ip: str) -> Optional[str]:
        """V2 or LEGACY if a session could begin on ``ip`` now, else None."""
        with self._lock:
            return self._format_locked(self._endpoints.get(ip))

    def begin_session(self, ip: str) -> Optional[tuple[str, Optional[StreamIdentity]]]:
        """Pick the wire format for a new session on ``ip``.

        Returns (V2, identity) with a fresh generation, (LEGACY, None), or None
        when the device must defer (unknown, no usable lease, superseded).
        """
        with self._lock:
            endpoint = self._endpoints.get(ip)
            chosen = self._format_locked(endpoint)
            if chosen != V2:
                return (LEGACY, None) if chosen == LEGACY else None
            key = (endpoint.boot_id, endpoint.ticket)
            generation = self._next_generation.get(key, 0) + 1
            if generation > 0xFFFFFFFF:
                return None  # generation exhausted: renew the lease first
            self._next_generation[key] = generation
            return V2, StreamIdentity(endpoint.boot_id, endpoint.ticket, generation)

    def legacy_guard_remaining(self, ip: str) -> float:
        with self._lock:
            ended = self._legacy_ended_at.get(ip)
        if ended is None:
            return 0.0
        return max(0.0, LEGACY_END_TO_BEGIN_S - (self.clock() - ended))

    def note_legacy_end(self, ip: str) -> None:
        with self._lock:
            self._legacy_ended_at[ip] = self.clock()
