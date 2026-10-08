"""Stream session v2 with the per-device legacy fallback (DEC-075).

No sockets: packets are captured from send_raw, PONGs are fed to the lease table.
"""
import asyncio
import json
import struct
import time

import pytest

from hapbeat_helper import protocol, stream_session
from hapbeat_helper.server import STREAM_END_REPEATS, HelperServer
from hapbeat_helper.stream_session import StreamLeaseTable

BOOT = 0x0102030405060708
V2_IP = "192.168.0.7"
LEGACY_IP = "192.168.0.8"


class _FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


def _tail(incarnation: int, ticket: int = 7, flags: int = 0x01, high_water: int = 0) -> dict:
    raw = (protocol.STREAM_TAIL_MARKER
           + struct.pack("<BBHQQII", 1, flags, 0, incarnation, BOOT, ticket, high_water))
    return protocol.parse_stream_tail(raw)


def _reply(leases: StreamLeaseTable, ip: str, seq: int, ts: int, tail: dict):
    leases.note_ping(seq, ts)
    return leases.on_pong(ip, {"seq": seq, "timestamp": ts, "stream_tail": tail})


def _decode(packet: bytes) -> dict:
    magic, version, command, seq, length = struct.unpack("<HBBHH", packet[:8])
    assert magic == protocol.MAGIC and length == len(packet) - 8
    return {"version": version, "command": command, "seq": seq, "payload": packet[8:]}


# ── protocol ─────────────────────────────────────────────────────

def test_v2_builders_match_contract_fixture():
    begin = protocol.build_stream_begin_v2(
        4660, BOOT, 7, 42, sample_rate=16000, channels=2, fmt=0, gain=1.0, target="*")
    data = protocol.build_stream_data_v2(4660, BOOT, 7, 42, 0, bytes.fromhex("1000f0ff"))
    end = protocol.build_stream_end_v2(4660, BOOT, 7, 42)
    assert begin.hex() == "4248023034121e000807060504030201070000002a000000803e0200000000000000803f2a00"
    assert data.hex() == "42480231341218000807060504030201070000002a000000000000001000f0ff"
    assert end.hex() == "42480232341210000807060504030201070000002a000000"


def test_legacy_builders_are_unchanged_v1_format():
    assert protocol.build_stream_end(5).hex() == "4248013205000000"
    begin = _decode(protocol.build_stream_begin(1, 16000, 1, 0, 0, 1.0, ""))
    assert begin["version"] == 1 and len(begin["payload"]) == 13


def test_ping_is_16_bytes_with_incarnation_and_8_without():
    assert len(protocol.build_ping(1, 2, 0x55)) == 8 + 16
    assert len(protocol.build_ping(1, 2)) == 8 + 8


def test_stream_tail_classification():
    assert protocol.parse_stream_tail(b"")["status"] == "absent"
    assert protocol.parse_stream_tail(b"\x07")["status"] == "absent"
    assert protocol.parse_stream_tail(b"HB")["status"] == "malformed"
    assert protocol.parse_stream_tail(protocol.STREAM_TAIL_MARKER + b"\x00" * 10)["status"] == "malformed"
    bad_flags = protocol.STREAM_TAIL_MARKER + struct.pack("<BBHQQII", 1, 0x04, 0, 1, BOOT, 7, 0)
    assert protocol.parse_stream_tail(bad_flags)["status"] == "malformed"
    valid = _tail(0x55)
    assert valid["status"] == "valid" and valid["lease_valid"] and not valid["superseded"]


def test_pong_with_lease_tail_keeps_volume_fields():
    payload = struct.pack("<qq", 10, 20) + b"n\x00a\x00fw\x00" + bytes([3, 64])
    payload += protocol.STREAM_TAIL_MARKER + struct.pack("<BBHQQII", 1, 1, 0, 9, BOOT, 7, 0)
    out = protocol.parse_pong(protocol.build_header(protocol.CMD_PONG, 4, len(payload)) + payload)
    assert out["volume_level"] == 3 and out["volume_wiper"] == 64
    assert "volume_steps" not in out
    assert out["stream_tail"]["status"] == "valid" and out["stream_tail"]["ticket"] == 7


# ── lease table ──────────────────────────────────────────────────

def test_matched_reply_classifies_and_unmatched_never_does():
    leases = StreamLeaseTable()
    inc = leases.incarnation
    # Unsolicited (timestamp 0) and unknown-seq replies change nothing.
    assert leases.on_pong(V2_IP, {"seq": 0, "timestamp": 0, "stream_tail": _tail(inc)}) is None
    assert leases.on_pong(V2_IP, {"seq": 77, "timestamp": 5, "stream_tail": _tail(inc)}) is None
    assert leases.kind(V2_IP) == stream_session.UNKNOWN
    assert _reply(leases, V2_IP, 1, 100, _tail(inc)) == stream_session.V2
    assert _reply(leases, LEGACY_IP, 2, 101, {"status": "absent"}) == stream_session.LEGACY
    # Malformed tail is ignored, not treated as legacy.
    assert _reply(leases, "192.168.0.9", 3, 102, {"status": "malformed"}) is None
    assert leases.kind("192.168.0.9") == stream_session.UNKNOWN


def test_late_or_foreign_incarnation_reply_cannot_roll_back():
    leases = StreamLeaseTable()
    _reply(leases, V2_IP, 1, 200, _tail(leases.incarnation))
    # An older (late) tailless reply for the same device must not flip it to legacy.
    leases.note_ping(2, 150)
    assert leases.on_pong(V2_IP, {"seq": 2, "timestamp": 150, "stream_tail": {"status": "absent"}}) is None
    assert leases.kind(V2_IP) == stream_session.V2
    # A tail echoing another incarnation is ignored.
    assert _reply(leases, V2_IP, 3, 300, _tail(leases.incarnation ^ 1, ticket=9)) is None
    assert leases.begin_session(V2_IP)[1].ticket == 7


def test_generations_increase_and_superseded_or_invalid_leases_defer():
    leases = StreamLeaseTable()
    _reply(leases, V2_IP, 1, 100, _tail(leases.incarnation))
    first = leases.begin_session(V2_IP)[1]
    second = leases.begin_session(V2_IP)[1]
    assert (first.generation, second.generation) == (1, 2)
    _reply(leases, V2_IP, 2, 200, _tail(leases.incarnation, flags=0x03, high_water=9))
    assert leases.is_superseded(V2_IP) and leases.begin_session(V2_IP) is None
    _reply(leases, LEGACY_IP, 3, 300, {"status": "absent"})
    leases.renew_incarnation()
    assert leases.kind(V2_IP) == stream_session.UNKNOWN
    assert leases.kind(LEGACY_IP) == stream_session.LEGACY  # no lease to renew
    _reply(leases, "192.168.0.10", 4, 400, _tail(leases.incarnation, flags=0x00, ticket=0))
    assert leases.kind("192.168.0.10") == stream_session.V2
    assert leases.begin_session("192.168.0.10") is None  # v2 without a lease defers, never legacy


# ── server integration ──────────────────────────────────────────

@pytest.fixture
def server(monkeypatch):
    server = HelperServer()
    server.sent = []
    monkeypatch.setattr(server.udp, "send_raw", lambda pkt, ip: server.sent.append((ip, pkt)) or True)
    return server


def _packets(server, ip):
    return [_decode(pkt) for sent_ip, pkt in server.sent if sent_ip == ip]


@pytest.mark.asyncio
async def test_v2_and_legacy_targets_stream_in_their_own_formats(server):
    leases = server.udp.stream_leases
    _reply(leases, V2_IP, 1, 100, _tail(leases.incarnation))
    _reply(leases, LEGACY_IP, 2, 101, {"status": "absent"})
    ws = _FakeWebSocket()
    both = {"targets": [V2_IP, LEGACY_IP], "stream_id": "s", "format": "pcm16"}
    await server._handle_stream_begin(ws, both)
    await server._handle_stream_data(ws, {**both, "offset": 0, "data": "AAAAAA=="})
    # Studio rewinds its offset on seek; v2 firmware would reject it.
    await server._handle_stream_data(ws, {**both, "offset": 0, "data": "AAAA"})
    await server._handle_stream_end(ws, both)

    v2 = _packets(server, V2_IP)
    assert [p["version"] for p in v2] == [2, 2, 2, 2]
    offsets = [struct.unpack("<I", p["payload"][16:20])[0] for p in v2 if p["command"] == protocol.CMD_STREAM_DATA]
    assert offsets == [0, 4], "v2 offsets count carried bytes and never go backward"
    assert struct.unpack("<QII", v2[-1]["payload"]) == (BOOT, 7, 1)
    legacy = _packets(server, LEGACY_IP)
    assert [p["version"] for p in legacy] == [1, 1, 1, 1]
    assert legacy[-1]["payload"] == b""


def _timed_server(monkeypatch):
    server = HelperServer()
    server.sent = []
    monkeypatch.setattr(
        server.udp, "send_raw",
        lambda pkt, ip: server.sent.append((time.monotonic(), ip, _decode(pkt))) or True)
    leases = server.udp.stream_leases
    _reply(leases, V2_IP, 1, 100, _tail(leases.incarnation))
    _reply(leases, LEGACY_IP, 2, 101, {"status": "absent"})
    return server


@pytest.mark.asyncio
async def test_restart_after_end_v2_immediately_legacy_after_guard_without_stalling(monkeypatch):
    """Loop playback: END then BEGIN at once to a v2 and a legacy device in one
    stream. v2 restarts at once; the legacy device gets its BEGIN only after
    the 300 ms guard, and neither the handler nor the v2 device waits for it."""
    server = _timed_server(monkeypatch)
    ws = _FakeWebSocket()
    both = {"targets": [V2_IP, LEGACY_IP]}
    await server._handle_stream_begin(ws, {**both, "stream_id": "a"})
    await server._handle_stream_end(ws, {**both, "stream_id": "a"})
    t_end = time.monotonic()
    await server._handle_stream_begin(ws, {**both, "stream_id": "b"})
    assert time.monotonic() - t_end < 0.05, "BEGIN must not hold up the WS handler"
    for i in range(10):  # real-time paced DATA, 16 ms apart
        await server._handle_stream_data(ws, {**both, "stream_id": "b", "offset": i * 4, "data": "AAAAAA=="})
        await asyncio.sleep(0.016)
    await server._handle_stream_end(ws, {**both, "stream_id": "b"})
    await asyncio.sleep(0.4)

    def stream_b(ip):
        rows = [(t, p) for t, sip, p in server.sent if sip == ip]
        begins = [k for k, (_, p) in enumerate(rows) if p["command"] == protocol.CMD_STREAM_BEGIN]
        return rows[begins[1]:]

    v2 = stream_b(V2_IP)
    assert v2[0][0] - t_end < 0.05
    # Stream a's repeated END interleaves here; it carries the older generation.
    generation_b = struct.unpack("<I", v2[0][1]["payload"][12:16])[0]
    v2_b = [p for _, p in v2 if struct.unpack("<I", p["payload"][12:16])[0] == generation_b]
    assert [p["command"] for p in v2_b] == (
        [protocol.CMD_STREAM_BEGIN] + [protocol.CMD_STREAM_DATA] * 10
        + [protocol.CMD_STREAM_END] * (1 + STREAM_END_REPEATS))
    assert all(struct.unpack("<I", p["payload"][12:16])[0] < generation_b
               for _, p in v2 if p not in v2_b)

    legacy = stream_b(LEGACY_IP)
    assert [p["command"] for _, p in legacy] == [protocol.CMD_STREAM_BEGIN] + [protocol.CMD_STREAM_DATA] * 10 + [protocol.CMD_STREAM_END]
    assert legacy[0][0] - t_end >= 0.28, "legacy BEGIN waits out the END->BEGIN guard"
    # Real-time pace kept: no burst of the DATA that arrived during the guard.
    gaps = [b[0] - a[0] for a, b in zip(legacy[1:], legacy[2:-1])]
    assert min(gaps) > 0.008, gaps


@pytest.mark.asyncio
async def test_new_begin_replaces_a_guarded_start(monkeypatch):
    server = _timed_server(monkeypatch)
    ws = _FakeWebSocket()
    one = {"targets": [LEGACY_IP]}
    await server._handle_stream_begin(ws, {**one, "stream_id": "a"})
    await server._handle_stream_end(ws, {**one, "stream_id": "a"})
    await server._handle_stream_begin(ws, {**one, "stream_id": "b"})  # guarded
    await server._handle_stream_data(ws, {**one, "stream_id": "b", "offset": 0, "data": "AAAA"})
    await server._handle_stream_begin(ws, {**one, "stream_id": "c"})  # replaces b before it went out
    await asyncio.sleep(0.4)
    cmds = [p["command"] for _, ip, p in server.sent if ip == LEGACY_IP]
    # a: BEGIN END, then c's BEGIN only (still guarded: no END since a's).
    assert cmds == [protocol.CMD_STREAM_BEGIN, protocol.CMD_STREAM_END, protocol.CMD_STREAM_BEGIN]
    server._legacy_delayed.pop(LEGACY_IP).task.cancel()  # c still streaming (no END)


@pytest.mark.asyncio
async def test_unknown_target_is_discovered_before_begin(server, monkeypatch):
    leases = server.udp.stream_leases

    def answer_ping(ip, **_):
        _reply(leases, ip, 5, 500, _tail(leases.incarnation))
        return 5

    monkeypatch.setattr(server.udp, "send_ping", answer_ping)
    await server._handle_stream_begin(_FakeWebSocket(), {"targets": [V2_IP], "stream_id": "x"})
    assert [p["version"] for p in _packets(server, V2_IP)] == [2]


@pytest.mark.asyncio
async def test_unanswered_target_defers_without_packets(server, monkeypatch):
    monkeypatch.setattr(server, "STREAM_DISCOVERY_WAIT_S", 0.05)
    monkeypatch.setattr(server.udp, "send_ping", lambda ip, **_: -1)
    ws = _FakeWebSocket()
    await server._handle_stream_begin(ws, {"targets": [V2_IP], "stream_id": "x"})
    assert server.sent == []
    assert '"no_target"' in ws.messages[-1] and V2_IP in ws.messages[-1]


@pytest.mark.asyncio
async def test_superseded_lease_is_reacquired_on_explicit_start(server, monkeypatch):
    leases = server.udp.stream_leases
    _reply(leases, V2_IP, 1, 100, _tail(leases.incarnation, flags=0x03, high_water=9))
    old = leases.incarnation

    def answer_ping(ip, **_):
        _reply(leases, ip, 6, 600, _tail(leases.incarnation, ticket=10, high_water=9))
        return 6

    monkeypatch.setattr(server.udp, "send_ping", answer_ping)
    await server._handle_stream_begin(_FakeWebSocket(), {"targets": [V2_IP], "stream_id": "x"})
    assert leases.incarnation != old
    begin = _packets(server, V2_IP)[0]
    assert struct.unpack("<QII", begin["payload"][:16]) == (BOOT, 10, 1)


# ── stream reliability (2026-10-09) ─────────────────────────────

@pytest.mark.asyncio
async def test_v2_end_is_repeated_and_legacy_end_is_not(server):
    leases = server.udp.stream_leases
    _reply(leases, V2_IP, 1, 100, _tail(leases.incarnation))
    _reply(leases, LEGACY_IP, 2, 101, {"status": "absent"})
    ws = _FakeWebSocket()
    both = {"targets": [V2_IP, LEGACY_IP], "stream_id": "s"}
    await server._handle_stream_begin(ws, both)
    await server._handle_stream_end(ws, both)
    await asyncio.sleep(0.35)
    ends = lambda ip: [p for p in _packets(server, ip) if p["command"] == protocol.CMD_STREAM_END]
    assert len(ends(V2_IP)) == 1 + STREAM_END_REPEATS
    assert len({p["payload"] for p in ends(V2_IP)}) == 1, "repeats are the same END"
    assert len(ends(LEGACY_IP)) == 1


@pytest.mark.asyncio
async def test_displaced_owner_is_told_and_its_data_gets_no_session_once(server):
    leases = server.udp.stream_leases
    _reply(leases, V2_IP, 1, 100, _tail(leases.incarnation))
    old_ws, new_ws = _FakeWebSocket(), _FakeWebSocket()
    server._clients.update({old_ws, new_ws})
    await server._handle_stream_begin(old_ws, {"targets": [V2_IP], "stream_id": "old"})
    await server._handle_stream_begin(new_ws, {"targets": [V2_IP], "stream_id": "new"})
    msgs = [json.loads(m) for m in old_ws.messages]
    assert msgs[-1] == {"type": "stream_displaced", "payload": {
        "stream_id": "old", "targets": [V2_IP], "by": "new", "same_client": False}}
    for _ in range(3):
        await server._handle_stream_data(old_ws, {"targets": [V2_IP], "stream_id": "old", "data": "AAAA"})
    acks = [json.loads(m)["payload"] for m in old_ws.messages[len(msgs):]]
    assert acks == [{"status": "no_session", "stream_id": "old"}]
    assert server._stream_orphans_since_health == 3


@pytest.mark.asyncio
async def test_end_without_targets_reaches_every_device_the_stream_owns(server):
    leases = server.udp.stream_leases
    _reply(leases, V2_IP, 1, 100, _tail(leases.incarnation))
    ws = _FakeWebSocket()
    await server._handle_stream_begin(ws, {"targets": [V2_IP], "stream_id": "s"})
    # No targets and an address-filter "target": not an IP, must not matter.
    await server._handle_stream_end(ws, {"stream_id": "s", "target": "player_1"})
    assert protocol.CMD_STREAM_END in [p["command"] for p in _packets(server, V2_IP)]
    assert server._active_streams == {}


@pytest.mark.asyncio
async def test_device_that_loses_its_lease_is_reported_deferred_not_started(server, monkeypatch):
    leases = server.udp.stream_leases
    _reply(leases, V2_IP, 1, 100, _tail(leases.incarnation))
    monkeypatch.setattr(leases, "begin_session", lambda ip: None)
    ws = _FakeWebSocket()
    await server._handle_stream_begin(ws, {"targets": [V2_IP], "stream_id": "s"})
    ack = json.loads(ws.messages[-1])["payload"]
    assert ack == {"status": "no_target", "targets": [], "deferred": [V2_IP], "stream_id": "s"}
