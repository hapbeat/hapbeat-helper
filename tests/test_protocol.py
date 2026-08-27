"""Round-trip tests for the L1 protocol builders."""

import struct

from hapbeat_helper import protocol


def test_header_round_trip():
    hdr = protocol.build_header(protocol.CMD_PLAY, 42, 100)
    parsed = protocol.parse_header(hdr)
    assert parsed == {
        "command_type": protocol.CMD_PLAY,
        "seq": 42,
        "payload_length": 100,
    }


def test_play_packet_layout():
    pkt = protocol.build_play(7, "explosion", target="player_1/chest", gain=0.5)
    hdr = protocol.parse_header(pkt)
    assert hdr["command_type"] == protocol.CMD_PLAY
    assert hdr["seq"] == 7
    payload = pkt[protocol.HEADER_SIZE:]
    assert payload.startswith(b"explosion\x00player_1/chest\x00")
    tail = payload[len("explosion\x00player_1/chest\x00"):]
    target_time, gain, pan = struct.unpack("<qff", tail)
    assert target_time == 0
    assert abs(gain - 0.5) < 1e-6
    assert pan == 0.0


def test_play_packet_pan():
    pkt = protocol.build_play(1, "e", target="", gain=1.0, pan=-0.25)
    payload = pkt[protocol.HEADER_SIZE:]
    tail = payload[len("e\x00\x00"):]
    _, _, pan = struct.unpack("<qff", tail)
    assert abs(pan - (-0.25)) < 1e-6


def test_play_pan_is_clamped():
    for raw, want in ((5.0, 1.0), (-5.0, -1.0)):
        pkt = protocol.build_play(1, "e", pan=raw)
        tail = pkt[protocol.HEADER_SIZE + len("e\x00\x00"):]
        _, _, pan = struct.unpack("<qff", tail)
        assert pan == want


def test_stream_begin_carries_complete_endpoint_address():
    target = "player_1/pos_neck/group_2"
    pkt = protocol.build_stream_begin(
        9, sample_rate=16000, channels=2, fmt=0,
        total_samples=0, gain=1.0, target=target,
    )
    header = protocol.parse_header(pkt)
    assert header["command_type"] == protocol.CMD_STREAM_BEGIN
    payload = pkt[protocol.HEADER_SIZE:]
    sample_rate, channels, fmt, total_samples, gain = struct.unpack(
        "<HBBIf", payload[:12],
    )
    assert (sample_rate, channels, fmt, total_samples) == (16000, 2, 0, 0)
    assert gain == 1.0
    assert payload[12:] == target.encode("utf-8") + b"\x00"


def test_pong_parser_extended():
    # build a fake PONG with all extended fields
    payload = struct.pack("<qq", 12345, 67890)
    payload += b"hapbeat-test\x00player_1/chest\x00v1.2.3\x00"
    payload += bytes([100, 50, 7])  # level, wiper, steps
    pkt = protocol.build_header(protocol.CMD_PONG, 1, len(payload)) + payload

    out = protocol.parse_pong(pkt)
    assert out["timestamp"] == 12345
    assert out["server_time"] == 67890
    assert out["device_name"] == "hapbeat-test"
    assert out["address"] == "player_1/chest"
    assert out["firmware_version"] == "v1.2.3"
    assert out["volume_level"] == 100
    assert out["volume_wiper"] == 50
    assert out["volume_steps"] == 7


def test_pong_parser_rejects_wrong_magic():
    bad = b"\x00\x00" + b"\x01\x11" + b"\x00" * 20
    assert protocol.parse_pong(bad) is None
