"""Persistent UDP listener for Hapbeat Layer 1 traffic.

Owns UDP port 7700: every PONG (PING reply or async push) flows through
this single socket so nothing else contends for the port. PLAY / STOP /
STREAM_* sends from the WebSocket layer are also routed through here so
the broadcast socket option survives.

A background thread does the recv loop; parsed PONGs are dispatched to
registered callbacks (the WebSocket server hands them off to the asyncio
loop with ``loop.call_soon_threadsafe``).

Design note — broadcast destinations:
    Discovery fans out across every local subnet (see :class:`BroadcastRoute`),
    while a PLAY/STOP fallback goes to exactly one destination. Keeping those
    apart is not a detail: firmware older than v0.3.0 does not de-duplicate
    PLAY/STOP by sequence number, so a device reachable on two destinations
    would fire the haptic twice. PING is idempotent and safe to duplicate.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable, List, Optional

from hapbeat_helper import protocol
from hapbeat_helper.stream_session import StreamLeaseTable

logger = logging.getLogger(__name__)

HAPBEAT_UDP_PORT = 7700
LIMITED_BROADCAST = "255.255.255.255"


class BroadcastRoute:
    """One address the helper can broadcast to.

    ``255.255.255.255`` leaves a multi-homed host through the single interface
    with the lowest metric. On a machine running Hyper-V / WSL2 / Docker that is
    often an always-up virtual switch with no Hapbeat behind it — and no
    Ethernet cable is needed for that to happen, which is why the symptom looks
    nothing like "multi-homed". A subnet-directed address (``192.168.0.255``)
    resolves through the directly-connected route for its subnet instead, so the
    metric never applies.

    The helper has mostly been spared this because it finds devices over mDNS
    and then unicasts, which resolves the same way. But mDNS is not always
    available — a network that blocks multicast, or a device whose mDNS
    responder has not come up yet — and the broadcast PING is the fallback that
    is supposed to cover exactly those cases.
    """

    __slots__ = ("addr", "network", "mask", "limited")

    def __init__(self, addr: str, network: int = 0, mask: int = 0,
                 limited: bool = False) -> None:
        self.addr = addr
        self.network = network
        self.mask = mask
        self.limited = limited

    def contains(self, ip: str) -> bool:
        """Whether ``ip`` sits on this route's subnet."""
        if self.limited or not self.mask:
            return False
        try:
            packed = [int(p) for p in ip.split(".")]
        except ValueError:
            return False
        if len(packed) != 4 or any(p < 0 or p > 255 for p in packed):
            return False
        value = (packed[0] << 24) | (packed[1] << 16) | (packed[2] << 8) | packed[3]
        return (value & self.mask) == self.network


def enumerate_broadcast_routes() -> List[BroadcastRoute]:
    """One destination per local IPv4 subnet, plus the limited broadcast.

    The address comes from each interface's real prefix rather than being
    assumed to end in ``.255``: a /16 broadcasts to ``x.y.255.255`` and a /25 to
    ``x.y.z.127``, and the subnet itself is whatever the router hands out.
    Deduplicated by address — two interfaces on one subnet (a laptop docked over
    Ethernet while Wi-Fi is still up) would otherwise deliver every packet twice.

    The limited broadcast is always kept as a catch-all, so SoftAP setups and
    hosts whose interfaces cannot be enumerated behave exactly as before.
    """
    routes: List[BroadcastRoute] = []
    seen = set()

    try:
        import ifaddr  # already a dependency (zeroconf uses it too)

        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if not ip.is_IPv4 or ip.ip.startswith("127."):
                    continue
                prefix = ip.network_prefix
                if not 0 < prefix <= 32:
                    continue
                try:
                    packed = [int(p) for p in ip.ip.split(".")]
                except ValueError:
                    continue
                if len(packed) != 4:
                    continue
                value = ((packed[0] << 24) | (packed[1] << 16)
                         | (packed[2] << 8) | packed[3])
                mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
                bcast = (value & mask) | (~mask & 0xFFFFFFFF)
                addr = "{}.{}.{}.{}".format(
                    (bcast >> 24) & 0xFF, (bcast >> 16) & 0xFF,
                    (bcast >> 8) & 0xFF, bcast & 0xFF,
                )
                if addr in seen:
                    continue
                seen.add(addr)
                routes.append(BroadcastRoute(addr, network=value & mask, mask=mask))
    except Exception as exc:  # noqa: BLE001 — never let this stop the listener
        logger.debug("could not enumerate local subnets (%s); "
                     "limited broadcast only", exc)

    routes.append(BroadcastRoute(LIMITED_BROADCAST, limited=True))
    return routes

# Drop an RTT-pending ping if its PONG hasn't arrived within this window — it
# isn't coming (RTT only matters for a couple seconds anyway), and the entry
# would otherwise leak in _pending_pings forever.
PENDING_PING_TTL_S = 5.0

PongCallback = Callable[[dict, str], None]
RttCallback = Callable[[str, float], None]


class UdpListener:
    """Sole owner of UDP port 7700."""

    def __init__(self, port: int = HAPBEAT_UDP_PORT) -> None:
        self._port = port
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        # seq -> (ip, perf_counter when sent) for RTT correlation. Only
        # RTT-tracked pings (ping_device) land here; the ~1 Hz liveness pings
        # do NOT, so this never accumulates (see send_ping).
        self._pending_pings: dict[int, tuple[str, float]] = {}
        # Monotonic 16-bit ping seq. A free-running counter (not a timestamp)
        # so back-to-back pings in one scan-loop burst get DISTINCT seqs.
        self._seq = 0
        self._lock = threading.Lock()
        # Broadcast destinations, rebuilt on every start(): a host's interfaces
        # change when a laptop is docked, a VPN comes up or Wi-Fi moves network.
        self._routes: List[BroadcastRoute] = []
        # The route a device actually answered on; None until the first PONG.
        # Written from the recv thread, read from the asyncio loop thread —
        # rebinding one attribute, which is atomic under the GIL.
        self._locked_route: Optional[BroadcastRoute] = None

        self._pong_callbacks: list[PongCallback] = []
        self._rtt_callbacks: list[RttCallback] = []
        # Stream-v2 leases and the per-device v2/legacy class, fed by matched
        # replies to our own (always 16-byte) PINGs. Survives restart().
        self.stream_leases = StreamLeaseTable()

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
        # Windows only: a prior sendto() to an unreachable peer queues an ICMP
        # port/host-unreachable, which Winsock then surfaces as
        # ConnectionResetError (WinError 10054) on the *next* recvfrom() — even
        # on this unconnected socket. That happens routinely during a firmware
        # reflash: the scan loop keeps unicast-PINGing a device that just
        # rebooted and isn't listening on 7700 yet. Without this the recv loop
        # below would die on that OSError and the helper would go deaf to ALL
        # PONGs until a full process restart (user report: 「ファーム書換後に
        # デバイスを見失い、helper 再起動でしか戻らない」). SIO_UDP_CONNRESET=False
        # tells Windows to ignore those resets. No-op on macOS/Linux (the
        # constant only exists on Windows). asyncio's own UDP transports set
        # this automatically, but we run a raw blocking socket so we must too.
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            try:
                sock.ioctl(socket.SIO_UDP_CONNRESET, False)
            except OSError as exc:
                logger.debug(
                    "SIO_UDP_CONNRESET ioctl failed (non-fatal): %s", exc,
                )
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
        self._routes = enumerate_broadcast_routes()
        self._locked_route = None
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

    def restart(self) -> bool:
        """Tear down and recreate the socket + recv thread, preserving the
        registered pong/RTT listeners.

        Recovery path for the helper's ``reset_discovery`` command: if the
        recv thread ever wedged (it shouldn't anymore — see SIO_UDP_CONNRESET
        in ``start``), this clears it in-process without restarting the whole
        daemon. Listeners live on the instance and survive stop()/start();
        only the in-flight RTT bookkeeping is dropped."""
        self.stop()
        with self._lock:
            self._pending_pings.clear()
        return self.start()

    # ── Send API ─────────────────────────────────────────────

    def _next_seq(self) -> int:
        with self._lock:
            self._seq = (self._seq + 1) & 0xFFFF
            return self._seq

    def _build_ping(self) -> tuple[int, bytes]:
        """A 16-byte stream-v2 PING. Pre-v2 firmware reads only the timestamp
        (it rejects only payloads shorter than 8 bytes) and replies as before."""
        seq = self._next_seq()
        ts_us = int(time.time() * 1_000_000)
        incarnation = self.stream_leases.note_ping(seq, ts_us)
        return seq, protocol.build_ping(seq, ts_us, incarnation)

    def _reap_pending(self, now: float) -> None:
        """Evict RTT-pending pings whose PONG never came. Caller holds _lock."""
        if not self._pending_pings:
            return
        for s in [
            s for s, (_, t0) in self._pending_pings.items()
            if now - t0 > PENDING_PING_TTL_S
        ]:
            self._pending_pings.pop(s, None)

    def send_ping(self, target_ip: str, *, track_rtt: bool = False) -> int:
        """Send a PING. `track_rtt` records the seq for RTT correlation.

        The ~1 Hz liveness pings from the scan loop pass `track_rtt=False`:
        liveness comes from the PONG callback (`_dispatch_pong`), independent
        of seq correlation. Tracking them would leak — lost or seq-collided
        PONGs are never popped, so `_pending_pings` would grow unbounded over
        a long session, inflating its lock-hold on the asyncio loop thread and
        eventually delaying UDP sends (restart "fixed" it). Only `ping_device`
        (which actually reports RTT) tracks.
        """
        sock = self._sock
        if sock is None:
            return -1
        seq, pkt = self._build_ping()
        if track_rtt:
            now = time.perf_counter()
            with self._lock:
                self._reap_pending(now)
                self._pending_pings[seq] = (target_ip, now)
        try:
            sock.sendto(pkt, (target_ip, self._port))
        except OSError as exc:
            logger.warning("UDP send_ping(%s) failed: %s", target_ip, exc)
            if track_rtt:
                with self._lock:
                    self._pending_pings.pop(seq, None)
            return -1
        return seq

    def broadcast_destination(self) -> str:
        """The single address a broadcast currently goes to.

        Once a device has answered this is its subnet's broadcast address;
        before that it is the limited broadcast, exactly as it has always been.
        """
        locked = self._locked_route
        return locked.addr if locked is not None else LIMITED_BROADCAST

    def _lock_route_for(self, ip: str) -> None:
        """Pin broadcasts to the subnet a device actually replied from.

        First reply wins, and the choice lasts until the socket is restarted.
        With devices on two subnets at once this settles on whichever answered
        first; broadcasts do not cross subnets anyway, so the alternative is not
        reaching both, it is reaching neither reliably.
        """
        if self._locked_route is not None or not self._routes:
            return
        for route in self._routes:
            if route.contains(ip):
                self._locked_route = route
                logger.info("broadcasting to %s (a device answered from %s)",
                            route.addr, ip)
                return

    def send_broadcast_ping(self) -> int:
        """PING every candidate broadcast destination.

        Idempotent, so a device reachable on two of them simply answers twice
        with no visible effect — whereas duplicating PLAY would fire the haptic
        twice on firmware that predates sequence de-duplication. This fan-out is
        what reaches a device the limited broadcast never gets to when mDNS is
        unavailable, and the PONG it provokes pins the PLAY/STOP fallback to the
        right subnet.
        """
        sock = self._sock
        if sock is None:
            return -1
        seq, pkt = self._build_ping()

        locked = self._locked_route
        if locked is not None or not self._routes:
            # Already pinned to a subnet: one destination, like any other packet.
            destinations = [self.broadcast_destination()]
        else:
            destinations = [r.addr for r in self._routes]

        sent = False
        last_error: Optional[OSError] = None
        for dst in destinations:
            try:
                sock.sendto(pkt, (dst, self._port))
                sent = True
            except OSError as exc:
                # Routine on its own: most hosts carry an adapter that can never
                # take a broadcast (Bluetooth PAN, Wi-Fi Direct, an idle virtual
                # switch). Trying anyway and letting the others through is
                # exactly what this fan-out is for.
                last_error = exc
                logger.debug("broadcast_ping to %s failed: %s", dst, exc)
        if not sent:
            logger.warning("UDP broadcast_ping failed on every destination: %s",
                           last_error)
            return -1
        return seq

    def send_raw(self, data: bytes, target_ip: str) -> bool:
        """Send arbitrary L1 packet through the listener socket.

        One destination only, including the ``<broadcast>`` case: this carries
        PLAY / STOP, and firmware older than v0.3.0 has no sequence de-duplication,
        so a device reachable twice would fire the haptic twice.
        """
        sock = self._sock
        if sock is None:
            return False
        dst = (
            self.broadcast_destination() if target_ip in ("<broadcast>", "")
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
            except OSError as exc:
                if not self._running:
                    break  # socket closed by stop()/restart() — expected exit
                # Transient error while still running: residual ICMP-unreachable
                # (should be suppressed by SIO_UDP_CONNRESET, but stay defensive
                # in case it's unavailable), or a brief NIC flap. Killing the
                # only recv thread here is exactly what made the helper need a
                # restart, so DON'T break — back off briefly and keep listening.
                logger.debug("UDP recv transient error (continuing): %s", exc)
                time.sleep(0.2)
                continue

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

            changed = self.stream_leases.on_pong(ip, pong)
            if changed is not None:
                logger.info("stream mode %s at %s", changed, ip)
            self._dispatch_pong(pong, ip)

    def _dispatch_pong(self, pong: dict, ip: str) -> None:
        # A reply proves which subnet the device is really on. Until this
        # happens a broadcast still goes out limited, which on a multi-homed
        # host may be leaving through an interface with no Hapbeat behind it.
        self._lock_route_for(ip)
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
