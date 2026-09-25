import pytest

from hapbeat_helper import protocol
from hapbeat_helper.server import HelperServer


class _FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


def _classify_legacy(server: HelperServer, ip: str) -> None:
    """A matched reply without an HBS2 tail: pre-v2 firmware (v1 streams)."""
    leases = server.udp.stream_leases
    leases.note_ping(900, 1_000)
    leases.on_pong(ip, {"seq": 900, "timestamp": 1_000, "stream_tail": {"status": "absent"}})


@pytest.mark.asyncio
async def test_stale_stream_end_cannot_stop_newer_stream_on_same_target(monkeypatch):
    server = HelperServer()
    ws = _FakeWebSocket()
    sent: list[tuple[int, int, str]] = []

    def capture(packet: bytes, ip: str) -> bool:
        header = protocol.parse_header(packet)
        sent.append((header["command_type"], header["seq"], ip))
        return True

    monkeypatch.setattr(server.udp, "send_raw", capture)
    target = "192.168.0.7"
    _classify_legacy(server, target)

    await server._handle_stream_begin(
        ws,
        {"targets": [target], "stream_id": "old", "format": "pcm16"},
    )
    await server._handle_stream_begin(
        ws,
        {"targets": [target], "stream_id": "new", "format": "pcm16"},
    )
    await server._handle_stream_end(
        ws,
        {"targets": [target], "stream_id": "old"},
    )
    await server._handle_stream_data(
        ws,
        {"targets": [target], "stream_id": "new", "offset": 0, "data": "AA=="},
    )
    await server._handle_stream_end(
        ws,
        {"targets": [target], "stream_id": "new"},
    )

    assert sent == [
        (protocol.CMD_STREAM_BEGIN, 0, target),
        (protocol.CMD_STREAM_BEGIN, 0, target),
        (protocol.CMD_STREAM_DATA, 1, target),
        (protocol.CMD_STREAM_END, 2, target),
    ]


@pytest.mark.asyncio
async def test_stale_stream_data_is_ignored_after_newer_begin(monkeypatch):
    server = HelperServer()
    ws = _FakeWebSocket()
    command_types: list[int] = []

    def capture(packet: bytes, _ip: str) -> bool:
        command_types.append(protocol.parse_header(packet)["command_type"])
        return True

    monkeypatch.setattr(server.udp, "send_raw", capture)
    target = "192.168.0.7"
    _classify_legacy(server, target)

    await server._handle_stream_begin(ws, {"targets": [target], "stream_id": "old"})
    await server._handle_stream_begin(ws, {"targets": [target], "stream_id": "new"})
    await server._handle_stream_data(
        ws,
        {"targets": [target], "stream_id": "old", "offset": 0, "data": "AA=="},
    )

    assert command_types == [protocol.CMD_STREAM_BEGIN, protocol.CMD_STREAM_BEGIN]


@pytest.mark.asyncio
async def test_serial_client_without_stream_id_keeps_working(monkeypatch):
    server = HelperServer()
    ws = _FakeWebSocket()
    packets: list[tuple[int, int]] = []

    def capture(packet: bytes, _ip: str) -> bool:
        header = protocol.parse_header(packet)
        packets.append((header["command_type"], header["seq"]))
        return True

    monkeypatch.setattr(server.udp, "send_raw", capture)
    _classify_legacy(server, "192.168.0.7")
    payload = {"targets": ["192.168.0.7"]}
    await server._handle_stream_begin(ws, payload)
    await server._handle_stream_data(ws, {**payload, "offset": 0, "data": "AA=="})
    await server._handle_stream_end(ws, payload)

    assert packets == [
        (protocol.CMD_STREAM_BEGIN, 0),
        (protocol.CMD_STREAM_DATA, 1),
        (protocol.CMD_STREAM_END, 2),
    ]
