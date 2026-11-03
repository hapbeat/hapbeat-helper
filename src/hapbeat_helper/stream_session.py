"""WifiUdp Stream Gen 2 lease tracking with a per-device Stream Gen 1 fallback.

Implements hapbeat-contracts/specs/stream-session.md for the helper:

- extended (16-byte) PINGs are unicast only, to the devices the helper streams
  to or is about to; discovery uses 8-byte PINGs, which never allocate a lease;
- each device has its own client incarnation (helper-streaming.md), so a
  takeover on one device does not change the helper's lease on the others;
- a device is classified only from *matched* direct replies to our own
  extended PING (ip, seq, echoed timestamp and incarnation match, timestamp
  newer than the last accepted reply): a valid HBS2 tail means Stream Gen 2,
  no byte after volume_wiper means Stream Gen 1, anything else is ignored;
- once superseded, a device stays deferred under the same incarnation until a
  reply carries lease_reissued or a client asks for a takeover;
- Stream Gen 2 sessions carry (boot id, lease ticket, generation) and need no
  cooldown; Stream Gen 1 sessions keep a 300 ms END->BEGIN guard for that
  device only (DEC-075).

Pure logic plus a lock: the UDP recv thread feeds PONGs, the asyncio loop reads.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

PENDING_TTL_S = 15.0
LEGACY_END_TO_BEGIN_S = 0.3

UNKNOWN = "unknown"
GEN2 = "gen2"
GEN1 = "gen1"

# Why a device's stream identity changed (LeaseChange.reason).
DEVICE_REBOOTED = "device_rebooted"
LEASE_CHANGED = "lease_changed"
SUPERSEDED = "superseded"
CLASS_CHANGED = "class_changed"


@dataclass(frozen=True)
class StreamIdentity:
    boot_id: int
    ticket: int
    generation: int


@dataclass(frozen=True)
class EndpointState:
    """A device's stream state as the last matched reply left it."""
    kind: str
    boot_id: int
    ticket: int
    lease_valid: bool
    superseded: bool


@dataclass(frozen=True)
class LeaseChange:
    """A matched reply changed the device's class, boot, ticket or supersession."""
    reason: str
    kind: str


@dataclass
class _Endpoint:
    kind: str = UNKNOWN
    boot_id: int = 0
    ticket: int = 0
    lease_valid: bool = False
    superseded: bool = False
    last_timestamp_us: int = 0
    # 0 until the first extended PING to this device.
    incarnation: int = 0
    # Matched replies accepted so far (lets a waiter see a fresh one).
    replies: int = 0
    warned_ignored: bool = False


@dataclass
class _Pending:
    ip: str
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
    _pending: dict[int, _Pending] = field(default_factory=dict)
    _endpoints: dict[str, _Endpoint] = field(default_factory=dict)
    _next_generation: dict[tuple[str, int, int], int] = field(default_factory=dict)
    _legacy_ended_at: dict[str, float] = field(default_factory=dict)

    def incarnation(self, ip: str) -> int:
        """The client incarnation this helper uses for ``ip`` (0 = none yet)."""
        with self._lock:
            endpoint = self._endpoints.get(ip)
            return endpoint.incarnation if endpoint else 0

    def note_ping(self, ip: str, seq: int, timestamp_us: int) -> int:
        """Record an outgoing extended PING to ``ip``; returns the incarnation
        to put in it (created on the first extended PING to that device)."""
        now = self.clock()
        with self._lock:
            for stale in [s for s, p in self._pending.items() if now - p.sent_at > PENDING_TTL_S]:
                del self._pending[stale]
            endpoint = self._endpoints.setdefault(ip, _Endpoint())
            if not endpoint.incarnation:
                endpoint.incarnation = _new_incarnation()
            self._pending[seq] = _Pending(ip, timestamp_us, endpoint.incarnation, now)
            return endpoint.incarnation

    def renew_incarnation(self, ip: str) -> None:
        """Explicit stream-ownership reacquisition on ``ip`` (a client's
        takeover). Only that device's Stream Gen 2 state resets to unknown;
        a Stream Gen 1 class survives because old firmware has no lease."""
        with self._lock:
            endpoint = self._endpoints.setdefault(ip, _Endpoint())
            endpoint.incarnation = _new_incarnation()
            for seq in [s for s, p in self._pending.items() if p.ip == ip]:
                del self._pending[seq]
            if endpoint.kind != GEN1:
                endpoint.kind = UNKNOWN
                endpoint.lease_valid = False
                endpoint.superseded = False

    def on_pong(self, ip: str, pong: dict) -> Optional[LeaseChange]:
        """Classify ``ip`` from a parsed PONG.

        Returns what changed (class, boot, ticket, newly superseded) or None.
        Unmatched, unsolicited, late and ignored replies change nothing.
        """
        timestamp = pong.get("timestamp", 0)
        tail = pong.get("stream_tail") or {"status": "absent"}
        status = tail.get("status")
        with self._lock:
            pending = self._pending.get(pong.get("seq"))
            endpoint = self._endpoints.get(ip)
            if (not timestamp or pending is None or endpoint is None
                    or pending.ip != ip or pending.timestamp_us != timestamp
                    or pending.incarnation != endpoint.incarnation
                    or timestamp <= endpoint.last_timestamp_us):
                return None
            if status == "valid":
                if tail.get("echoed_incarnation") != pending.incarnation:
                    return None
            elif status != "absent":
                warn = not endpoint.warned_ignored
                endpoint.warned_ignored = True
                if warn:
                    logger.warning("ignoring a PONG stream tail from %s that this "
                                   "helper cannot read (a newer firmware major?)", ip)
                return None
            return self._accept_locked(endpoint, tail, timestamp)

    @staticmethod
    def _accept_locked(endpoint: _Endpoint, tail: dict, timestamp: int) -> Optional[LeaseChange]:
        before = (endpoint.kind, endpoint.boot_id, endpoint.ticket, endpoint.superseded)
        if tail.get("status") == "valid":
            lease_valid = bool(tail["lease_valid"])
            # The supersession latch: a new ticket alone (our entry expired and
            # was replaced) must not take the device back from the application
            # that superseded us; only a re-issued ticket or a takeover clears it.
            keep = (endpoint.kind == GEN2 and endpoint.superseded
                    and not (lease_valid and tail.get("lease_reissued")))
            endpoint.kind = GEN2
            endpoint.boot_id = tail["boot_id"]
            endpoint.ticket = tail["ticket"]
            endpoint.lease_valid = lease_valid
            endpoint.superseded = bool(tail["superseded"]) or keep
        else:
            endpoint.kind = GEN1
            endpoint.boot_id = 0
            endpoint.ticket = 0
            endpoint.lease_valid = False
            endpoint.superseded = False
        endpoint.last_timestamp_us = timestamp
        endpoint.replies += 1
        kind, boot_id, ticket, superseded = before
        if endpoint.kind != kind:
            return LeaseChange(CLASS_CHANGED, endpoint.kind)
        if endpoint.kind != GEN2:
            return None
        if endpoint.boot_id != boot_id:
            return LeaseChange(DEVICE_REBOOTED, endpoint.kind)
        if endpoint.ticket != ticket:
            return LeaseChange(LEASE_CHANGED, endpoint.kind)
        if endpoint.superseded and not superseded:
            return LeaseChange(SUPERSEDED, endpoint.kind)
        return None

    def state(self, ip: str) -> EndpointState:
        with self._lock:
            e = self._endpoints.get(ip) or _Endpoint()
            return EndpointState(e.kind, e.boot_id, e.ticket, e.lease_valid, e.superseded)

    def replies(self, ip: str) -> int:
        """Matched replies accepted from ``ip`` so far."""
        with self._lock:
            endpoint = self._endpoints.get(ip)
            return endpoint.replies if endpoint else 0

    def mark_superseded(self, ip: str) -> None:
        """The device said STREAM_SUPERSEDED: defer it like a superseded reply."""
        with self._lock:
            endpoint = self._endpoints.get(ip)
            if endpoint is not None and endpoint.kind == GEN2:
                endpoint.superseded = True

    def kind(self, ip: str) -> str:
        with self._lock:
            endpoint = self._endpoints.get(ip)
            return endpoint.kind if endpoint else UNKNOWN

    def is_superseded(self, ip: str) -> bool:
        with self._lock:
            endpoint = self._endpoints.get(ip)
            return bool(endpoint and endpoint.kind == GEN2 and endpoint.superseded)

    @staticmethod
    def _format_locked(endpoint: Optional[_Endpoint]) -> Optional[str]:
        if endpoint is None:
            return None
        if endpoint.kind == GEN1:
            return GEN1
        if (endpoint.kind != GEN2 or not endpoint.lease_valid or endpoint.superseded
                or not endpoint.boot_id or not endpoint.ticket):
            return None
        return GEN2

    def begin_format(self, ip: str) -> Optional[str]:
        """GEN2 or GEN1 if a session could begin on ``ip`` now, else None."""
        with self._lock:
            return self._format_locked(self._endpoints.get(ip))

    def begin_session(self, ip: str) -> Optional[tuple[str, Optional[StreamIdentity]]]:
        """Pick the wire format for a new session on ``ip``.

        Returns (GEN2, identity) with a fresh generation, (GEN1, None), or None
        when the device must defer (unknown, no usable lease, superseded).
        """
        with self._lock:
            endpoint = self._endpoints.get(ip)
            chosen = self._format_locked(endpoint)
            if chosen != GEN2:
                return (GEN1, None) if chosen == GEN1 else None
            key = (ip, endpoint.boot_id, endpoint.ticket)
            generation = self._next_generation.get(key, 0) + 1
            if generation > 0xFFFFFFFF:
                return None  # generation exhausted: renew the lease first
            self._next_generation[key] = generation
            return GEN2, StreamIdentity(endpoint.boot_id, endpoint.ticket, generation)

    def legacy_guard_remaining(self, ip: str) -> float:
        with self._lock:
            ended = self._legacy_ended_at.get(ip)
        if ended is None:
            return 0.0
        return max(0.0, LEGACY_END_TO_BEGIN_S - (self.clock() - ended))

    def note_legacy_end(self, ip: str) -> None:
        with self._lock:
            self._legacy_ended_at[ip] = self.clock()
