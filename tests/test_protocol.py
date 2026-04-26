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
    target_time, gain = struct.unpack("<qf", tail)
    assert target_time == 0
    assert abs(gain - 0.5) < 1e-6


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
