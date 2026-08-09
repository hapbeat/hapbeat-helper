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
