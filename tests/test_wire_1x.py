"""DEC-096: the 1.x wire rules in the helper (Stream Gen 2 fixes, ERROR,
takeover, extended PINGs by unicast only, caps passthrough, Kit check).

No sockets and nothing reaches a device: packets are captured from send_raw /
a fake socket, PONGs and ERRORs are fed to the lease table and the server.
Fixture bytes are copied from hapbeat-contracts (commit 46f246e)
fixtures/stream-session-cases.json and fixtures/wire-extension-cases.json.
"""
import asyncio
import json
import re
import socket
import struct
from pathlib import Path

import pytest

from hapbeat_helper import protocol, server as server_mod, stream_session
from hapbeat_helper.server import HELPER_FEATURES, HelperServer, _helper_generation
from hapbeat_helper.stream_session import StreamLeaseTable
from hapbeat_helper.udp_listener import UdpListener

BOOT = 0x0102030405060708
NEW_BOOT = 0x1112131415161718
A = "192.168.0.7"
B = "192.168.0.8"


class _FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)

    def of_type(self, msg_type: str) -> list[dict]:
        return [m["payload"] for m in map(json.loads, self.messages) if m["type"] == msg_type]


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _tail(incarnation, ticket=7, flags=0x01, high_water=0, boot=BOOT) -> dict:
    raw = (protocol.STREAM_TAIL_MARKER
           + struct.pack("<BBHQQII", 1, flags, 0, incarnation, boot, ticket, high_water))
    return protocol.parse_stream_tail(raw)


_seq = iter(range(1, 60000))


def _reply(leases: StreamLeaseTable, ip: str, tail: dict = None, **tail_args):
    """Extended PING to ``ip`` and its matched reply (Stream Gen 2 by default)."""
    seq = next(_seq)
    ts = 1_000_000 + seq
    incarnation = leases.note_ping(ip, seq, ts)
    if tail is None:
        tail = _tail(incarnation, **tail_args)
    return leases.on_pong(ip, {"seq": seq, "timestamp": ts, "stream_tail": tail})


def _decode(packet: bytes) -> dict:
    magic, version, command, seq, length = struct.unpack("<HBBHH", packet[:8])
    assert magic == protocol.MAGIC and length == len(packet) - 8
    out = {"version": version, "command": command, "seq": seq, "payload": packet[8:]}
    if version == 2:
        out["identity"] = struct.unpack("<QII", packet[8:24])
    return out


def _error(code: int, detail: bytes = b"", message: str = "x") -> dict:
    payload = (struct.pack("<H", code) + message.encode() + b"\x00"
               + struct.pack("<BHB", 0x31, 1, len(detail)) + detail)
    return protocol.parse_error(protocol.build_header(protocol.CMD_ERROR, 9, len(payload)) + payload)


@pytest.fixture
def server(monkeypatch):
    srv = HelperServer()
    srv.sent = []
    srv.pings = []
    srv._clock = _Clock()
    monkeypatch.setattr(srv.udp, "send_raw", lambda pkt, ip: srv.sent.append((ip, _decode(pkt))) or True)
    monkeypatch.setattr(srv.udp, "send_ping",
                        lambda ip, **kw: srv.pings.append((ip, kw.get("lease", False))) or 1)
    monkeypatch.setattr(srv, "STREAM_DISCOVERY_WAIT_S", 0.05)
    return srv


def _packets(srv, ip, command=None):
    return [p for sip, p in srv.sent if sip == ip and (command is None or p["command"] == command)]


async def _begin(srv, ws, ip, stream_id, **extra):
    srv._clients.add(ws)
    await srv._handle_stream_begin(ws, {"targets": [ip], "stream_id": stream_id, **extra})


async def _data(srv, ws, ip, stream_id, data="AAAA"):
    await srv._handle_stream_data(ws, {"targets": [ip], "stream_id": stream_id, "data": data})


# ── 1. PONG classification (stream-session.md Receiver classification) ──

PONG_CASES = [
    ('no_tail',
     '4248011101013d0000401e18240a060088531e18240a0600486170626561742d4100706c617965725f312f706f735f63686573742f67726f75705f3100312e302e300008c8',
     {'class': 'gen1'}),
    ('tail_32',
     '4248011101015d0000401e18240a060088531e18240a0600486170626561742d4100706c617965725f312f706f735f63686573742f67726f75705f3100312e302e300008c84842533201010000887766554433221108070605040302010700000007000000',
     {'class': 'gen2', 'lease_valid': True, 'superseded': False, 'ticket': 7, 'high_water_ticket': 7, 'lease_reissued': False}),
    ('tail_superseded',
     '4248011101015d0000401e18240a060088531e18240a0600486170626561742d4100706c617965725f312f706f735f63686573742f67726f75705f3100312e302e300008c84842533201030000887766554433221108070605040302010500000007000000',
     {'class': 'gen2', 'lease_valid': True, 'superseded': True, 'ticket': 5, 'high_water_ticket': 7, 'lease_reissued': False}),
    ('tail_40',
     '424801110101650000401e18240a060088531e18240a0600486170626561742d4100706c617965725f312f706f735f63686573742f67726f75705f3100312e302e300008c848425332010100008877665544332211080706050403020107000000070000000001020304050607',
     {'class': 'gen2', 'lease_valid': True, 'superseded': False, 'ticket': 7, 'high_water_ticket': 7, 'lease_reissued': False}),
    ('tail_reissued',
     '4248011101015d0000401e18240a060088531e18240a0600486170626561742d4100706c617965725f312f706f735f63686573742f67726f75705f3100312e302e300008c84842533201050000887766554433221108070605040302010900000007000000',
     {'class': 'gen2', 'lease_valid': True, 'superseded': False, 'ticket': 9, 'high_water_ticket': 7, 'lease_reissued': True}),
    ('tail_unknown_flag',
     '4248011101015d0000401e18240a060088531e18240a0600486170626561742d4100706c617965725f312f706f735f63686573742f67726f75705f3100312e302e300008c84842533201090000887766554433221108070605040302010700000007000000',
     {'class': 'gen2', 'lease_valid': True, 'superseded': False, 'ticket': 7, 'high_water_ticket': 7, 'lease_reissued': False}),
    ('tail_reserved_nonzero',
     '4248011101015d0000401e18240a060088531e18240a0600486170626561742d4100706c617965725f312f706f735f63686573742f67726f75705f3100312e302e300008c84842533201010201887766554433221108070605040302010700000007000000',
     {'class': 'gen2', 'lease_valid': True, 'superseded': False, 'ticket': 7, 'high_water_ticket': 7, 'lease_reissued': False}),
    ('tail_ext_version_2',
     '4248011101015d0000401e18240a060088531e18240a0600486170626561742d4100706c617965725f312f706f735f63686573742f67726f75705f3100312e302e300008c84842533202010000887766554433221108070605040302010700000007000000',
     {'class': 'ignored'}),
    ('tail_short',
     '424801110101550000401e18240a060088531e18240a0600486170626561742d4100706c617965725f312f706f735f63686573742f67726f75705f3100312e302e300008c8484253320101000088776655443322110807060504030201',
     {'class': 'ignored'}),
    ('other_bytes',
     '4248011101015d0000401e18240a060088531e18240a0600486170626561742d4100706c617965725f312f706f735f63686573742f67726f75705f3100312e302e300008c85854524100000000000000000000000000000000000000000000000000000000',
     {'class': 'ignored'}),
    ('truncated_marker',
     '424801110101400000401e18240a060088531e18240a0600486170626561742d4100706c617965725f312f706f735f63686573742f67726f75705f3100312e302e300008c8484253',
     {'class': 'ignored'}),
    ('non_streaming_node',
     '4248011101015d0000401e18240a060088531e18240a0600486170626561742d4100706c617965725f312f706f735f63686573742f67726f75705f3100312e302e300008c84842533201000000887766554433221108070605040302010000000000000000',
     {'class': 'gen2', 'lease_valid': False, 'superseded': False, 'ticket': 0, 'high_water_ticket': 0, 'lease_reissued': False}),
]
FIXTURE_PING = {"seq": 257, "timestamp": 1700000000000000, "incarnation": 1234605616436508552}


@pytest.mark.parametrize("name,hex_,expected", PONG_CASES, ids=[c[0] for c in PONG_CASES])
def test_pong_classification_matches_contract_fixture(name, hex_, expected, monkeypatch):
    tail = protocol.parse_pong(bytes.fromhex(hex_))["stream_tail"]
    status = {"gen1": "absent", "gen2": "valid", "ignored": "ignored"}[expected["class"]]
    assert tail["status"] == status
    for key in ("lease_valid", "superseded", "ticket", "high_water_ticket", "lease_reissued"):
        if key in expected:
            assert tail[key] == expected[key], key

    # As a matched reply to the fixture's PING, it classifies the device.
    monkeypatch.setattr(stream_session, "_new_incarnation", lambda: FIXTURE_PING["incarnation"])
    leases = StreamLeaseTable()
    leases.note_ping(A, FIXTURE_PING["seq"], FIXTURE_PING["timestamp"])
    leases.on_pong(A, protocol.parse_pong(bytes.fromhex(hex_)))
    kind = {"gen1": stream_session.GEN1, "gen2": stream_session.GEN2,
            "ignored": stream_session.UNKNOWN}[expected["class"]]
    assert leases.kind(A) == kind


# ── 2. Matching ──────────────────────────────────────────────────

def test_discovery_ping_is_8_bytes_and_its_reply_never_classifies():
    u = UdpListener()
    seq, pkt = u._build_ping()
    assert len(pkt) == 8 + 8
    ts = struct.unpack("<q", pkt[8:16])[0]
    assert u.stream_leases.on_pong(A, {"seq": seq, "timestamp": ts,
                                       "stream_tail": {"status": "absent"}}) is None
    assert u.stream_leases.kind(A) == stream_session.UNKNOWN
    seq, pkt = u._build_ping(A)
    assert len(pkt) == 8 + 16


def test_reply_from_another_ip_is_not_matched():
    leases = StreamLeaseTable()
    incarnation = leases.note_ping(A, 5, 500)
    assert leases.on_pong(B, {"seq": 5, "timestamp": 500, "stream_tail": _tail(incarnation)}) is None
    assert leases.kind(B) == stream_session.UNKNOWN


def test_ignored_reply_keeps_class_and_lease():
    leases = StreamLeaseTable()
    _reply(leases, A)
    assert _reply(leases, A, {"status": "ignored"}) is None
    assert leases.state(A) == stream_session.EndpointState(stream_session.GEN2, BOOT, 7, True, False)


# ── 3. One incarnation per device ────────────────────────────────

@pytest.mark.asyncio
async def test_renewing_one_device_leaves_the_others_and_their_streams(server):
    leases = server.udp.stream_leases
    _reply(leases, A)
    _reply(leases, B)
    inc_b = leases.incarnation(B)
    ws = _FakeWebSocket()
    await _begin(server, ws, B, "b")
    leases.renew_incarnation(A)
    assert leases.incarnation(B) == inc_b and leases.incarnation(A) != inc_b
    assert leases.state(B).kind == stream_session.GEN2 and leases.state(A).kind == stream_session.UNKNOWN
    await _data(server, ws, B, "b")
    data = _packets(server, B, protocol.CMD_STREAM_DATA)
    assert data and data[-1]["identity"] == (BOOT, 7, 1)
    assert ws.of_type("stream_lost") == []


# ── 4. Supersession latch ────────────────────────────────────────

def test_superseded_stays_deferred_until_a_reissued_ticket():
    leases = StreamLeaseTable()
    _reply(leases, A)
    _reply(leases, A, flags=0x03, high_water=9)
    assert leases.is_superseded(A)
    # Our entry expired and got a fresh ticket: still not ours to take back.
    _reply(leases, A, ticket=12, high_water=12)
    assert leases.is_superseded(A) and leases.begin_session(A) is None
    # A re-issued ticket clears the deferral.
    _reply(leases, A, ticket=13, flags=0x05, high_water=12)
    assert not leases.is_superseded(A)
    assert leases.begin_session(A)[1].ticket == 13


# ── 5. Device-side changes during a transaction ──────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    ({"boot": NEW_BOOT}, "device_rebooted"),
    ({"ticket": 8}, "lease_changed"),
    ({"flags": 0x03, "high_water": 9}, "superseded"),
])
async def test_retired_identity_ends_the_session_and_tells_its_owner(server, change, reason):
    leases = server.udp.stream_leases
    _reply(leases, A)
    _reply(leases, B)
    ws_a, ws_b = _FakeWebSocket(), _FakeWebSocket()
    await _begin(server, ws_a, A, "a")
    await _begin(server, ws_b, B, "b")
    lease_change = _reply(leases, A, **change)
    assert lease_change.reason == reason
    # The UDP thread hands the change to the loop.
    server._loop = asyncio.get_running_loop()
    server._on_lease_change(A, lease_change)
    for _ in range(5):
        await asyncio.sleep(0)
    ends = _packets(server, A, protocol.CMD_STREAM_END)
    assert ends and ends[0]["identity"] == (BOOT, 7, 1), "END under the old identity"
    assert ws_a.of_type("stream_lost") == [{"stream_id": "a", "targets": [A], "reason": reason}]
    sent_before = len(_packets(server, A))
    await _data(server, ws_a, A, "a")
    assert len(_packets(server, A)) == sent_before, "no DATA under the retired identity"
    assert ws_a.of_type("stream_ack")[-1] == {"status": "no_session", "stream_id": "a"}
    # The other device and transaction carry on.
    await _data(server, ws_b, B, "b")
    assert _packets(server, B, protocol.CMD_STREAM_DATA)[-1]["identity"] == (BOOT, 7, 1)
    assert ws_b.of_type("stream_lost") == []


@pytest.mark.asyncio
async def test_data_checks_the_identity_before_sending(server):
    leases = server.udp.stream_leases
    _reply(leases, A)
    ws = _FakeWebSocket()
    await _begin(server, ws, A, "a")
    _reply(leases, A, boot=NEW_BOOT)  # change not delivered to the loop yet
    await _data(server, ws, A, "a")
    assert _packets(server, A, protocol.CMD_STREAM_DATA) == []
    assert ws.of_type("stream_lost")[0]["reason"] == "device_rebooted"


@pytest.mark.asyncio
async def test_class_change_ends_a_gen1_session_in_the_old_format(server):
    leases = server.udp.stream_leases
    _reply(leases, A, {"status": "absent"})
    ws = _FakeWebSocket()
    await _begin(server, ws, A, "a")
    _reply(leases, A)  # firmware updated to Stream Gen 2
    await server._check_stream_identity(A)
    end = _packets(server, A, protocol.CMD_STREAM_END)
    assert [p["version"] for p in end] == [1]
    assert ws.of_type("stream_lost")[0]["reason"] == "device_rebooted"


# ── 6. Takeover ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_superseded_target_defers_without_takeover(server):
    leases = server.udp.stream_leases
    _reply(leases, A, flags=0x03, high_water=9)
    _reply(leases, B)
    inc = leases.incarnation(A)
    ws = _FakeWebSocket()
    await server._handle_stream_begin(ws, {"targets": [A, B], "stream_id": "s"})
    ack = ws.of_type("stream_ack")[0]
    assert ack["targets"] == [B] and ack["deferred"] == [A]
    assert leases.incarnation(A) == inc
    assert _packets(server, A) == []


@pytest.mark.asyncio
async def test_takeover_renews_only_that_device_and_pings_it(server):
    leases = server.udp.stream_leases
    _reply(leases, A, flags=0x03, high_water=9)
    _reply(leases, B)
    inc_a, inc_b = leases.incarnation(A), leases.incarnation(B)
    ws = _FakeWebSocket()
    await server._handle_stream_begin(ws, {"targets": [A, B], "stream_id": "s", "takeover": True})
    assert leases.incarnation(A) != inc_a and leases.incarnation(B) == inc_b
    assert (A, True) in server.pings


# ── 7. ERROR ─────────────────────────────────────────────────────

ERROR_CASES = [
    ('424801ff03032700140073747265616d2074696d6564206f7574003134121008070605040302010700000029000000', {'error_code': 20, 'name': 'STREAM_TIMED_OUT', 'message': 'stream timed out', 'ref_command': 49, 'ref_seq': 4660, 'detail_hex': '08070605040302010700000029000000'}),
    ('424801ff03031800100073747265616d2067656e203220726571756972656400', {'error_code': 16, 'name': 'STREAM_GEN_UNSUPPORTED', 'message': 'stream gen 2 required'}),
]


@pytest.mark.parametrize("hex_,expected", ERROR_CASES)
def test_parse_error_matches_contract_fixture(hex_, expected):
    out = protocol.parse_error(bytes.fromhex(hex_))
    assert out["error_code"] == expected["error_code"]
    assert protocol.ERROR_NAMES[out["error_code"]] == expected["name"]
    assert out["message"] == expected["message"]
    if "ref_command" in expected:
        assert out["ref_command"] == expected["ref_command"]
        assert out["ref_seq"] == expected["ref_seq"]
        assert out["detail"].hex() == expected["detail_hex"]
    else:
        assert "detail" not in out


class _FakeSock:
    def __init__(self, listener, datagrams):
        self.listener, self.datagrams = listener, list(datagrams)

    def recvfrom(self, _size):
        if not self.datagrams:
            self.listener._running = False
            raise socket.timeout()
        return self.datagrams.pop(0)


def test_listener_takes_errors_from_port_7700_only():
    u = UdpListener()
    got = []
    u.add_error_listener(lambda ip, err: got.append((ip, err["error_code"])))
    err = bytes.fromhex(ERROR_CASES[0][0])
    u._sock = _FakeSock(u, [(err, (A, 7700)), (err, (B, 50000))])
    u._running = True
    u._recv_loop()
    assert got == [(A, protocol.ERR_STREAM_TIMED_OUT)]


async def _streaming(server, ip=A, legacy=False):
    leases = server.udp.stream_leases
    _reply(leases, ip, {"status": "absent"} if legacy else None)
    ws = _FakeWebSocket()
    await _begin(server, ws, ip, "s")
    await _data(server, ws, ip, "s")
    return ws


def _envelope(*identity):
    return struct.pack("<QII", *identity)


@pytest.mark.asyncio
async def test_errors_not_about_our_session_are_ignored(server):
    ws = await _streaming(server)
    before = list(server.sent)
    await server._handle_device_error("192.168.0.99", _error(protocol.ERR_STREAM_STOPPED, _envelope(BOOT, 7, 1)))
    await server._handle_device_error(A, _error(protocol.ERR_STREAM_STOPPED, _envelope(BOOT, 7, 2)))
    await server._handle_device_error(A, _error(protocol.ERR_STREAM_STOPPED))
    assert ws.of_type("stream_lost") == [] and server.sent == before
    assert A in server._active_streams


@pytest.mark.asyncio
@pytest.mark.parametrize("code,reason,sends_end", [
    (protocol.ERR_STREAM_SUPERSEDED, "superseded", True),
    (protocol.ERR_STREAM_STOPPED, "stopped", False),
    (protocol.ERR_STREAM_TARGET_MISMATCH, "target_mismatch", False),
])
async def test_errors_that_end_the_transaction(server, code, reason, sends_end):
    ws = await _streaming(server)
    await server._handle_device_error(A, _error(code, _envelope(BOOT, 7, 1)))
    assert ws.of_type("stream_lost") == [{"stream_id": "s", "targets": [A], "reason": reason}]
    assert bool(_packets(server, A, protocol.CMD_STREAM_END)) == sends_end
    assert A not in server._active_streams and A not in server._stream_wire
    if code == protocol.ERR_STREAM_SUPERSEDED:
        assert server.udp.stream_leases.is_superseded(A)
    if code == protocol.ERR_STREAM_TARGET_MISMATCH:
        assert (A, True) in server.pings
    # An error about the session just ended changes nothing more.
    await server._handle_device_error(A, _error(code, _envelope(BOOT, 7, 1)))
    assert len(ws.of_type("stream_lost")) == 1


@pytest.mark.asyncio
async def test_not_started_resends_the_same_begin_at_once(server):
    ws = await _streaming(server)
    begins = len(_packets(server, A, protocol.CMD_STREAM_BEGIN))
    await server._handle_device_error(A, _error(protocol.ERR_STREAM_NOT_STARTED, _envelope(BOOT, 7, 1)))
    resent = _packets(server, A, protocol.CMD_STREAM_BEGIN)
    assert len(resent) == begins + 1 and resent[-1]["identity"] == (BOOT, 7, 1)
    assert ws.of_type("stream_lost") == []


@pytest.mark.asyncio
async def test_timed_out_continues_in_a_new_generation(server):
    ws = await _streaming(server)
    await server._handle_device_error(A, _error(protocol.ERR_STREAM_TIMED_OUT, _envelope(BOOT, 7, 1)))
    assert _packets(server, A, protocol.CMD_STREAM_END) == []
    assert _packets(server, A, protocol.CMD_STREAM_BEGIN)[-1]["identity"] == (BOOT, 7, 2)
    await _data(server, ws, A, "s")
    data = _packets(server, A, protocol.CMD_STREAM_DATA)[-1]
    assert data["identity"] == (BOOT, 7, 2)
    assert struct.unpack("<I", data["payload"][16:20])[0] == 0, "new session starts at offset 0"
    assert ws.of_type("stream_lost") == []


@pytest.mark.asyncio
async def test_lease_invalid_renews_the_lease_and_continues(server, monkeypatch):
    ws = await _streaming(server)
    leases = server.udp.stream_leases
    monkeypatch.setattr(server.udp, "send_ping", lambda ip, **kw: _reply(leases, ip, ticket=8) and 1)
    await server._handle_device_error(A, _error(protocol.ERR_STREAM_LEASE_INVALID, _envelope(BOOT, 7, 1)))
    assert _packets(server, A, protocol.CMD_STREAM_END)[0]["identity"] == (BOOT, 7, 1)
    assert _packets(server, A, protocol.CMD_STREAM_BEGIN)[-1]["identity"] == (BOOT, 8, 1)
    await _data(server, ws, A, "s")
    assert _packets(server, A, protocol.CMD_STREAM_DATA)[-1]["identity"] == (BOOT, 8, 1)
    assert ws.of_type("stream_lost") == []


@pytest.mark.asyncio
async def test_lease_invalid_without_a_new_lease_is_unreachable(server, monkeypatch):
    ws = await _streaming(server)
    leases = server.udp.stream_leases
    monkeypatch.setattr(server.udp, "send_ping",
                        lambda ip, **kw: _reply(leases, ip, ticket=0, flags=0) and 1)
    await server._handle_device_error(A, _error(protocol.ERR_STREAM_LEASE_INVALID, _envelope(BOOT, 7, 1)))
    assert ws.of_type("stream_lost") == [{"stream_id": "s", "targets": [A], "reason": "unreachable"}]
    assert A not in server._active_streams


@pytest.mark.asyncio
async def test_gen_unsupported_reclassifies_and_continues_in_gen2(server, monkeypatch):
    ws = await _streaming(server, legacy=True)
    leases = server.udp.stream_leases
    monkeypatch.setattr(server.udp, "send_ping", lambda ip, **kw: _reply(leases, ip) and 1)
    await server._handle_device_error(A, _error(protocol.ERR_STREAM_GEN_UNSUPPORTED))
    end = _packets(server, A, protocol.CMD_STREAM_END)
    assert [p["version"] for p in end] == [1], "old-format END"
    begin = _packets(server, A, protocol.CMD_STREAM_BEGIN)[-1]
    assert begin["version"] == 2 and begin["identity"] == (BOOT, 7, 1)
    assert ws.of_type("stream_lost") == []


@pytest.mark.asyncio
async def test_other_errors_are_only_logged(server, caplog):
    ws = await _streaming(server)
    before = list(server.sent)
    for _ in range(3):
        await server._handle_device_error(A, _error(protocol.ERR_UNKNOWN_COMMAND))
    await server._handle_device_error(A, _error(protocol.ERR_STREAM_ENDED, _envelope(BOOT, 7, 1)))
    assert server.sent == before and ws.of_type("stream_lost") == []
    assert sum("UNKNOWN_COMMAND" in r.getMessage() for r in caplog.records) == 1
    assert any("STREAM_ENDED" in r.getMessage() for r in caplog.records)
    assert server._device_errors_since_health == 4


# ── 8. PING shapes ───────────────────────────────────────────────

class _SendSock:
    def __init__(self):
        self.sent = []

    def sendto(self, pkt, addr):
        self.sent.append((addr[0], len(pkt) - 8))


def test_broadcast_and_liveness_pings_are_8_bytes_extended_only_unicast():
    u = UdpListener()
    u._sock = _SendSock()
    u._routes = []
    u._refresh_routes = lambda: None
    u.send_broadcast_ping()
    u.send_ping(A)
    u.send_ping(B, lease=True)
    assert u._sock.sent == [("255.255.255.255", 8), (A, 8), (B, 16)]


@pytest.mark.asyncio
async def test_scan_sends_extended_pings_only_to_streaming_devices_and_for_one_ttl(monkeypatch):
    srv = HelperServer()
    srv._clock = _Clock()
    srv.sent = []
    monkeypatch.setattr(srv.udp, "send_raw", lambda pkt, ip: srv.sent.append((ip, _decode(pkt))) or True)
    sock = _SendSock()
    srv.udp._sock = sock
    srv.udp._routes = []
    monkeypatch.setattr(srv.udp, "_refresh_routes", lambda: None)
    for ip in (A, B):
        srv.registry.upsert_device({"ip": ip, "name": ip})
    _reply(srv.udp.stream_leases, A)
    ws = _FakeWebSocket()
    await _begin(srv, ws, A, "s")

    def tick():
        sock.sent.clear()
        srv._send_scan_pings(srv._lease_ips())
        return sorted(sock.sent)

    assert tick() == [(A, 16), (B, 8), ("255.255.255.255", 8)]
    await srv._handle_stream_end(ws, {"stream_id": "s"})
    srv._clock.now += 14.0
    assert tick() == [(A, 16), (B, 8), ("255.255.255.255", 8)]
    srv._clock.now += 1.5
    assert tick() == [(A, 8), (B, 8), ("255.255.255.255", 8)]


# ── 9. BEGIN repetition / 10. own gap ────────────────────────────

def _commands(srv, ip):
    return [p["command"] for p in _packets(srv, ip)]


@pytest.mark.asyncio
async def test_gen2_begin_is_repeated_and_gen1_is_not(server):
    leases = server.udp.stream_leases
    _reply(leases, A)
    _reply(leases, B, {"status": "absent"})
    ws = _FakeWebSocket()
    server._clients.add(ws)
    both = {"targets": [A, B], "stream_id": "s"}
    await server._handle_stream_begin(ws, both)
    for _ in range(4):
        server._clock.now += 0.01
        await server._handle_stream_data(ws, {**both, "data": "AAAA"})
    BEGIN, DATA = protocol.CMD_STREAM_BEGIN, protocol.CMD_STREAM_DATA
    assert _commands(server, A) == [BEGIN] + [BEGIN, DATA] * 3 + [DATA]
    server._clock.now += 0.5
    await server._handle_stream_data(ws, {**both, "data": "AAAA"})
    assert _commands(server, A)[-2:] == [BEGIN, DATA]
    assert {p["identity"] for p in _packets(server, A, BEGIN)} == {(BOOT, 7, 1)}
    assert _commands(server, B) == [BEGIN] + [DATA] * 5


@pytest.mark.asyncio
async def test_own_gap_of_3s_ends_and_begins_a_new_generation(server):
    ws = await _streaming(server)
    for _ in range(3):
        await _data(server, ws, A, "s")
    server.sent.clear()
    server._clock.now += 3.0
    await _data(server, ws, A, "s")
    rows = _packets(server, A)
    cmds = [p["command"] for p in rows]
    assert cmds[0] == protocol.CMD_STREAM_END and rows[0]["identity"] == (BOOT, 7, 1)
    assert cmds[1] == protocol.CMD_STREAM_BEGIN and rows[1]["identity"] == (BOOT, 7, 2)
    data = rows[cmds.index(protocol.CMD_STREAM_DATA)]
    assert data["identity"] == (BOOT, 7, 2)
    assert struct.unpack("<I", data["payload"][16:20])[0] == 0
    assert ws.of_type("stream_lost") == []


# ── 11. unreachable ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_device_going_offline_loses_its_transaction(server):
    ws = await _streaming(server)
    await server._on_devices_offline([A, B])
    assert ws.of_type("stream_lost") == [{"stream_id": "s", "targets": [A], "reason": "unreachable"}]
    assert _packets(server, A, protocol.CMD_STREAM_END) == []
    assert server._active_streams == {}


# ── 12. get_info passthrough / reset_storage ─────────────────────

CAPS = {"caps_version": 1, "generation": 1, "stream_gens": [2], "kit_schema": {"min": 2, "max": 2},
        "kit_clip_formats": ["pcm16"], "future_thing": {"x": 1}}


@pytest.mark.asyncio
async def test_get_info_passes_caps_through(monkeypatch):
    srv = HelperServer()
    reply = {"status": "ok", "name": "n", "fw": "1.0.0", "caps": CAPS,
             "stream_formats": ["pcm16", "opus"], "stream_protocol": 2,
             "storage_incompatible": ["audio_partition"]}
    monkeypatch.setattr(server_mod, "_send_tcp_query", lambda ip, cmd: reply)
    ws = _FakeWebSocket()
    await srv._dispatch(ws, {"type": "get_info", "payload": {"ip": A}})
    out = ws.of_type("get_info_result")[0]
    for key in ("caps", "stream_formats", "stream_protocol", "storage_incompatible"):
        assert out[key] == reply[key]


@pytest.mark.asyncio
async def test_reset_storage_is_relayed_and_area_must_be_a_string(monkeypatch):
    srv = HelperServer()
    sent = []
    monkeypatch.setattr(server_mod, "_send_tcp_to_one",
                        lambda ip, cmd: sent.append((ip, cmd)) or {"ip": ip, "success": True, "response": {}})
    ws = _FakeWebSocket()
    await srv._dispatch(ws, {"type": "reset_storage", "payload": {"ip": A, "area": "audio_partition"}})
    assert sent == [(A, {"cmd": "reset_storage", "area": "audio_partition"})]
    await srv._dispatch(ws, {"type": "reset_storage", "payload": {"ip": A, "area": 3}})
    assert len(sent) == 1
    assert ws.of_type("write_result")[-1]["error"] == "invalid_area"


# ── 13. Kit check before writing ─────────────────────────────────

class _FakeTcp:
    info = None
    commands: list = []

    def __init__(self, ip):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def connect(self, **_):
        return True

    def send_json(self, cmd):
        _FakeTcp.commands.append(cmd["cmd"])

    def send_raw(self, data, timeout=None):
        pass

    def read_response(self, timeout=5.0):
        return {"status": "ok"}


def _kit(tmp_path: Path, schema="2.0.0") -> Path:
    pack = tmp_path / "kit-a"
    (pack / "install-clips").mkdir(parents=True)
    (pack / "kit-a-manifest.json").write_text(json.dumps(
        {"schema_version": schema, "name": "kit-a", "version": "1.0.0", "events": {}}))
    (pack / "install-clips" / "a.wav").write_bytes(b"RIFF0000")
    return pack


@pytest.mark.parametrize("caps,ok", [
    (None, True),
    (CAPS, True),
    ({**CAPS, "kit_schema": {"min": 3, "max": 3}}, False),
    ({**CAPS, "kit_clip_formats": ["opus"]}, False),
])
def test_kit_is_checked_against_caps_before_install(tmp_path, monkeypatch, caps, ok):
    _FakeTcp.commands = []
    _FakeTcp.info = {"status": "ok", **({"caps": caps} if caps else {})}
    monkeypatch.setattr(server_mod, "TcpRawConnection", _FakeTcp)
    success, message = server_mod._deploy_kit_to_device(A, _kit(tmp_path), "")
    assert success is ok, message
    assert ("kit_install" in _FakeTcp.commands) is ok
    if not ok:
        assert "kit" in message or "PCM16" in message


# ── helper_hello (helper-ws.md) ──────────────────────────────────

def test_helper_hello_lists_every_handled_type_and_the_features():
    srv = HelperServer()
    hello = srv._helper_hello_msg("1.0.0d4")["payload"]
    source = Path(server_mod.__file__).read_text(encoding="utf-8")
    assert hello["types"] == sorted(set(re.findall(r'@on\("([a-z0-9_]+)"\)', source)))
    assert {"get_info", "stream_begin", "stream_end", "reset_storage"} <= set(hello["types"])
    assert hello["generation"] == 1 and hello["version"] == "1.0.0d4"
    assert hello["features"] == HELPER_FEATURES
    assert _helper_generation("0.5.0") == 0


@pytest.mark.asyncio
async def test_unlisted_type_is_still_an_unknown_type_error():
    srv = HelperServer()
    ws = _FakeWebSocket()
    await srv._dispatch(ws, {"type": "no_such_request", "payload": {}})
    assert ws.of_type("error") == [{"message": "unknown type: no_such_request"}]
