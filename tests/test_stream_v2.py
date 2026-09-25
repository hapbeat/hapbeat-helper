"""Stream session v2 with the per-device legacy fallback (DEC-075).

No sockets: packets are captured from send_raw, PONGs are fed to the lease table.
"""
import struct
import time

import pytest

from hapbeat_helper import protocol, stream_session
from hapbeat_helper.server import HelperServer
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


@pytest.mark.asyncio
async def test_v2_restarts_immediately_but_legacy_waits_300ms(server):
    leases = server.udp.stream_leases
    _reply(leases, V2_IP, 1, 100, _tail(leases.incarnation))
    _reply(leases, LEGACY_IP, 2, 101, {"status": "absent"})
    ws = _FakeWebSocket()
    for ip in (V2_IP, LEGACY_IP):
        payload = {"targets": [ip], "stream_id": "a"}
        await server._handle_stream_begin(ws, payload)
        await server._handle_stream_end(ws, payload)
        started = time.monotonic()
        await server._handle_stream_begin(ws, {"targets": [ip], "stream_id": "b"})
        elapsed = time.monotonic() - started
        if ip == V2_IP:
            assert elapsed < 0.1
            gens = [struct.unpack("<QII", p["payload"][:16])[2] for p in _packets(server, ip)]
            assert gens == [1, 1, 2]
        else:
            assert elapsed >= 0.28


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
