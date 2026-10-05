"""Subnet-directed broadcast: route maths and the discovery/playback split.

`255.255.255.255` leaves a multi-homed host through the single interface with
the lowest metric, which on a machine running Hyper-V / WSL2 / Docker is often a
virtual switch with no Hapbeat behind it. The helper is mostly spared because it
finds devices over mDNS and unicasts — but the broadcast PING is the fallback
for exactly the cases where mDNS is unavailable, so it has the same hole.

The rule these tests exist to protect: **only the PING may fan out.**
`send_raw` carries PLAY/STOP, and firmware older than v0.3.0 does not
de-duplicate those by sequence number, so a device reachable on two destinations
would fire the haptic twice.
"""
from __future__ import annotations

import pytest

from hapbeat_helper import udp_listener as mod
from hapbeat_helper.udp_listener import (
    LIMITED_BROADCAST,
    BroadcastRoute,
    UdpListener,
    enumerate_broadcast_routes,
)


class FakeAdapterIp:
    def __init__(self, ip, prefix, is_ipv4=True):
        self.ip = ip
        self.network_prefix = prefix
        self.is_IPv4 = is_ipv4


class FakeAdapter:
    def __init__(self, ips):
        self.ips = ips


def fake_ifaddr(monkeypatch, ips):
    """Stand in for the ifaddr module so tests do not depend on real NICs."""
    class Stub:
        @staticmethod
        def get_adapters():
            return [FakeAdapter([FakeAdapterIp(*i) for i in ips])]

    monkeypatch.setitem(__import__("sys").modules, "ifaddr", Stub)


# ── Route maths ─────────────────────────────────────────────────────
# A /24 is not the only case: assuming ".255" is exactly the bug this replaces.
@pytest.mark.parametrize("ip,prefix,expected", [
    ("192.168.0.205", 24, "192.168.0.255"),
    ("10.1.2.3", 8, "10.255.255.255"),
    ("172.17.192.1", 16, "172.17.255.255"),
    ("192.168.1.10", 25, "192.168.1.127"),
    ("192.168.1.200", 25, "192.168.1.255"),
    ("192.168.1.10", 30, "192.168.1.11"),
])
def test_broadcast_address_comes_from_the_real_prefix(monkeypatch, ip, prefix, expected):
    fake_ifaddr(monkeypatch, [(ip, prefix)])
    assert [r.addr for r in enumerate_broadcast_routes()] == [expected, LIMITED_BROADCAST]


def test_limited_broadcast_is_always_kept_as_catch_all(monkeypatch):
    # SoftAP setups and hosts we cannot enumerate depend on it, and dropping it
    # would regress every single-NIC user.
    fake_ifaddr(monkeypatch, [])
    routes = enumerate_broadcast_routes()
    assert [r.addr for r in routes] == [LIMITED_BROADCAST]
    assert routes[-1].limited


def test_enumeration_failure_falls_back_to_limited_broadcast(monkeypatch):
    class Exploding:
        @staticmethod
        def get_adapters():
            raise RuntimeError("no interface data")

    monkeypatch.setitem(__import__("sys").modules, "ifaddr", Exploding)
    assert [r.addr for r in enumerate_broadcast_routes()] == [LIMITED_BROADCAST]


def test_same_subnet_interfaces_collapse_to_one_destination(monkeypatch):
    # A docked laptop has Ethernet and Wi-Fi on one subnet; without dedup the
    # device receives every discovery packet twice.
    fake_ifaddr(monkeypatch, [("192.168.0.67", 24), ("192.168.0.223", 24)])
    assert [r.addr for r in enumerate_broadcast_routes()] == [
        "192.168.0.255", LIMITED_BROADCAST]


def test_loopback_and_ipv6_are_skipped(monkeypatch):
    fake_ifaddr(monkeypatch, [
        ("127.0.0.1", 8, True),
        ("fe80::1", 64, False),
        ("192.168.0.5", 24, True),
    ])
    assert [r.addr for r in enumerate_broadcast_routes()] == [
        "192.168.0.255", LIMITED_BROADCAST]


def test_route_membership():
    route = BroadcastRoute("192.168.0.255", network=0xC0A80000, mask=0xFFFFFF00)
    assert route.contains("192.168.0.42")
    assert not route.contains("192.168.1.42")
    assert not route.contains("nonsense")
    # The catch-all must never win the lock, or the fallback would go back to
    # the limited broadcast that failed to reach the device in the first place.
    assert not BroadcastRoute(LIMITED_BROADCAST, limited=True).contains("192.168.0.42")


# ── Listener behaviour ──────────────────────────────────────────────
class FakeSocket:
    def __init__(self):
        self.sent = []

    def sendto(self, packet, addr):
        self.sent.append(addr[0])


@pytest.fixture
def listener(monkeypatch):
    fake_ifaddr(monkeypatch, [("192.168.0.205", 24), ("172.17.192.1", 16)])
    u = UdpListener(port=7700)
    u._routes = enumerate_broadcast_routes()
    u._sock = FakeSocket()
    return u


def test_ping_fans_out_until_a_device_answers(listener):
    listener.send_broadcast_ping()
    assert listener._sock.sent == ["192.168.0.255", "172.17.255.255", LIMITED_BROADCAST]


def test_send_raw_broadcast_never_fans_out(listener):
    # The critical rule: one PLAY produces exactly one datagram.
    listener.send_raw(b"play", "<broadcast>")
    assert listener._sock.sent == [LIMITED_BROADCAST]


def test_pong_pins_the_broadcast_fallback(listener):
    listener._dispatch_pong({}, "192.168.0.42")

    listener.send_raw(b"play", "<broadcast>")
    assert listener._sock.sent == ["192.168.0.255"]

    # Discovery stops probing the others once pinned.
    listener._sock.sent.clear()
    listener.send_broadcast_ping()
    assert listener._sock.sent == ["192.168.0.255"]


def test_first_reply_wins_and_unknown_subnets_keep_the_catch_all(listener):
    listener._lock_route_for("192.168.0.42")
    listener._lock_route_for("172.17.0.9")
    assert listener.broadcast_destination() == "192.168.0.255"

    other = UdpListener(port=7700)
    other._routes = listener._routes
    other._sock = FakeSocket()
    other._lock_route_for("10.9.9.9")   # a route we did not enumerate
    assert other.broadcast_destination() == LIMITED_BROADCAST


def test_explicit_unicast_target_is_untouched(listener):
    listener.send_raw(b"play", "192.168.0.42")
    assert listener._sock.sent == ["192.168.0.42"]


def test_single_nic_host_is_unchanged(monkeypatch):
    # Regression guard: one interface must behave as it did before.
    fake_ifaddr(monkeypatch, [("192.168.0.205", 24)])
    u = UdpListener(port=7700)
    u._routes = enumerate_broadcast_routes()
    u._sock = FakeSocket()
    u.send_broadcast_ping()
    assert u._sock.sent == ["192.168.0.255", LIMITED_BROADCAST]


def test_ping_survives_one_dead_interface(monkeypatch, listener):
    # A virtual switch that rejects the send must not stop the others — that is
    # the very interface the fan-out exists to work around.
    class PartlyDead(FakeSocket):
        def sendto(self, packet, addr):
            if addr[0] == "172.17.255.255":
                raise OSError(101, "Network is unreachable")
            super().sendto(packet, addr)

    listener._sock = PartlyDead()
    assert listener.send_broadcast_ping() != -1
    assert listener._sock.sent == ["192.168.0.255", LIMITED_BROADCAST]


def test_ping_reports_failure_only_when_nothing_gets_out(listener):
    class Dead(FakeSocket):
        def sendto(self, packet, addr):
            raise OSError(101, "Network is unreachable")

    listener._sock = Dead()
    assert listener.send_broadcast_ping() == -1


# ── Releasing the lock (contracts message-format.md §2.1) ───────────
# The lock used to last until the socket was restarted. After the PC moved to
# another Wi-Fi network every PING still went to the old subnet, Studio never
# saw the devices on the new network, and only a helper restart recovered.
SUBNETS = [("192.168.0.205", 24), ("172.17.192.1", 16)]


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(mod.time, "monotonic", fake)
    return fake


def make_listener(monkeypatch, subnets):
    """A started listener as of the (fake) current time, on a fake socket."""
    fake_ifaddr(monkeypatch, subnets)
    u = UdpListener(port=7700)
    u._routes = enumerate_broadcast_routes()
    u._routes_enumerated_at = mod.time.monotonic()
    u._sock = FakeSocket()
    return u


def count_enumerations(monkeypatch):
    calls = []
    real = mod.enumerate_broadcast_routes

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(mod, "enumerate_broadcast_routes", counting)
    return calls


def test_lock_is_released_when_its_network_is_gone(monkeypatch, clock):
    u = make_listener(monkeypatch, [("192.168.0.205", 24)])
    u._lock_route_for("192.168.0.42")
    assert u.broadcast_destination() == "192.168.0.255"

    # Wi-Fi moves to another network: the old subnet disappears.
    fake_ifaddr(monkeypatch, [("192.168.11.7", 24)])
    clock.now += 2.0
    u.send_broadcast_ping()
    assert u._sock.sent == ["192.168.11.255", LIMITED_BROADCAST]
    assert u.broadcast_destination() == LIMITED_BROADCAST

    # The first device on the new network locks again.
    u._lock_route_for("192.168.11.30")
    assert u.broadcast_destination() == "192.168.11.255"


def test_lock_is_released_when_nobody_answers_for_route_ttl(monkeypatch, clock):
    # The interface list cannot show a switch to a network that reuses the old
    # subnet number, or devices that all went away: silence has to do it.
    u = make_listener(monkeypatch, SUBNETS)
    u._lock_route_for("192.168.0.42")

    clock.now += mod.ROUTE_TTL_S + 0.5
    u.send_broadcast_ping()
    assert u._sock.sent == ["192.168.0.255", "172.17.255.255", LIMITED_BROADCAST]
    assert u.broadcast_destination() == LIMITED_BROADCAST


def test_route_ttl_is_the_device_ttl():
    # contracts §2.1: route_ttl is the window that retires a known device.
    from hapbeat_helper.device_registry import _OFFLINE_THRESHOLD
    assert UdpListener(port=7700).route_ttl == _OFFLINE_THRESHOLD


def test_pongs_from_the_locked_subnet_keep_the_lock(monkeypatch, clock):
    u = make_listener(monkeypatch, SUBNETS)
    u._lock_route_for("192.168.0.42")
    for _ in range(5):
        clock.now += mod.ROUTE_TTL_S - 1.0
        u._lock_route_for("192.168.0.42")  # liveness PONG
        u._sock.sent.clear()
        u.send_broadcast_ping()
        assert u._sock.sent == ["192.168.0.255"]


def test_pongs_from_another_subnet_do_not_keep_the_lock(monkeypatch, clock):
    u = make_listener(monkeypatch, SUBNETS)
    u._lock_route_for("192.168.0.42")
    clock.now += mod.ROUTE_TTL_S - 1.0
    u._lock_route_for("172.17.0.9")
    clock.now += 2.0
    u.send_broadcast_ping()
    assert u.broadcast_destination() == LIMITED_BROADCAST
    assert len(u._sock.sent) == 3


def test_fan_out_follows_a_network_change_before_any_lock(monkeypatch, clock):
    u = make_listener(monkeypatch, [("192.168.0.205", 24)])
    fake_ifaddr(monkeypatch, [("10.0.0.5", 24)])
    clock.now += 2.0
    u.send_broadcast_ping()
    assert u._sock.sent == ["10.0.0.255", LIMITED_BROADCAST]


def test_new_interface_keeps_a_live_lock(monkeypatch, clock):
    # Docking Ethernet while the devices are still on Wi-Fi changes nothing.
    u = make_listener(monkeypatch, [("192.168.0.205", 24)])
    u._lock_route_for("192.168.0.42")
    fake_ifaddr(monkeypatch, [("192.168.0.205", 24), ("172.17.192.1", 16)])
    clock.now += 2.0
    u.send_broadcast_ping()
    assert u._sock.sent == ["192.168.0.255"]


def test_enumeration_is_rate_limited_and_off_the_playback_path(monkeypatch, clock):
    u = make_listener(monkeypatch, SUBNETS)
    calls = count_enumerations(monkeypatch)

    for _ in range(50):
        u.send_raw(b"play", "<broadcast>")
        u.send_raw(b"play", "192.168.0.42")
        u.send_ping("192.168.0.42")
    clock.now += 5.0
    u.send_raw(b"stop", "<broadcast>")
    assert calls == []

    clock.now -= 4.5   # 0.5 s after the last enumeration
    u.send_broadcast_ping()
    assert calls == []
    clock.now += 0.6
    u.send_broadcast_ping()
    u.send_broadcast_ping()
    assert len(calls) == 1
