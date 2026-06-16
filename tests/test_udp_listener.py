"""udp_listener ping bookkeeping — guards the _pending_pings leak fix.

A regression here brings back the gradual helper slowdown: liveness pings
piling up in _pending_pings → the dict + its lock-hold on the asyncio loop
thread grow unbounded → UDP play/stream sends get delayed until restart.

Invariants:
  - liveness pings (track_rtt=False) must NOT populate _pending_pings;
  - seq must be distinct per call (the old monotonic_ns()//1000 aliased
    back-to-back pings in one scan-loop burst to the same key → leak);
  - stale RTT-pending entries are reaped.
"""
import time

import pytest

from hapbeat_helper.udp_listener import UdpListener, PENDING_PING_TTL_S


def test_next_seq_distinct_in_burst():
    u = UdpListener()
    seqs = [u._next_seq() for _ in range(100)]
    assert len(set(seqs)) == 100  # no aliasing — every ping gets its own seq
    assert all(0 <= s <= 0xFFFF for s in seqs)


def test_next_seq_wraps_16bit():
    u = UdpListener()
    u._seq = 0xFFFF
    assert u._next_seq() == 0  # wraps cleanly, never overflows the 16-bit field


def test_reap_pending_evicts_stale_keeps_fresh():
    u = UdpListener()
    now = time.perf_counter()
    u._pending_pings[1] = ("192.168.0.1", now - (PENDING_PING_TTL_S + 1))  # stale
    u._pending_pings[2] = ("192.168.0.2", now)                             # fresh
    u._reap_pending(now)
    assert 1 not in u._pending_pings
    assert 2 in u._pending_pings


def test_liveness_ping_does_not_leak_but_rtt_ping_tracks():
    u = UdpListener(port=47713)
    if not u.start():
        pytest.skip("could not bind UDP test port")
    try:
        # Liveness pings (the ~1 Hz scan-loop path) must leave _pending_pings
        # empty — this is the leak fix.
        for ip in ("192.168.0.10", "192.168.0.11", "192.168.0.12"):
            u.send_ping(ip)
        assert len(u._pending_pings) == 0
        # Only the RTT path (ping_device) records the seq.
        seq = u.send_ping("192.168.0.20", track_rtt=True)
        assert seq in u._pending_pings
        assert len(u._pending_pings) == 1
    finally:
        u.stop()
