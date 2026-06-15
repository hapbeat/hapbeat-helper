"""Layer-1 wire-format round-trip tests (protocol.py).

The header byte order / packet layout is the contract with firmware — a
regression here makes the device silently reject every packet. These cover
header build↔parse round-trips (incl. field boundaries), bad-magic/short
rejection, and the PLAY packet structure (null-terminated event/target +
<qf gain).
"""
import struct

from hapbeat_helper import protocol


def test_header_roundtrip():
    for cmd, seq, plen in [
        (protocol.CMD_PLAY, 0, 0),
        (protocol.CMD_STOP, 1, 12),
        (protocol.CMD_PING, 0xFFFF, 0xFFFF),  # field maxima
        (protocol.CMD_STREAM_DATA, 12345, 1024),
    ]:
        hdr = protocol.build_header(cmd, seq, plen)
        assert len(hdr) == protocol.HEADER_SIZE
        parsed = protocol.parse_header(hdr)
        assert parsed == {
            "command_type": cmd,
            "seq": seq,
            "payload_length": plen,
        }


def test_parse_header_rejects_bad_magic_and_short():
    assert protocol.parse_header(b"\x00\x00\x01\x01\x00\x00\x00\x00") is None  # bad magic
    assert protocol.parse_header(b"\x00\x00") is None  # too short
    # wrong version byte
    bad_ver = struct.pack("<HBBHH", protocol.MAGIC, 0x99, protocol.CMD_PING, 0, 0)
    assert protocol.parse_header(bad_ver) is None


def test_build_play_structure():
    pkt = protocol.build_play(
        seq=7, event_id="alert-kit.urgent", target="player_1/chest",
        target_time_us=0, gain=0.75,
    )
    hdr = protocol.parse_header(pkt)
    assert hdr is not None
    assert hdr["command_type"] == protocol.CMD_PLAY
    assert hdr["seq"] == 7

    payload = pkt[protocol.HEADER_SIZE:]
    assert hdr["payload_length"] == len(payload)
    # event_id\0 target\0 <q(target_time_us) f(gain)
    event, rest = payload.split(b"\x00", 1)
    target, tail = rest.split(b"\x00", 1)
    assert event.decode() == "alert-kit.urgent"
    assert target.decode() == "player_1/chest"
    ttime, gain = struct.unpack("<qf", tail)
    assert ttime == 0
    assert abs(gain - 0.75) < 1e-6


def test_build_play_empty_event_and_target():
    # Empty strings still emit their null terminators (firmware reads C-strings).
    pkt = protocol.build_play(seq=0, event_id="", target="")
    payload = pkt[protocol.HEADER_SIZE:]
    event, rest = payload.split(b"\x00", 1)
    target, tail = rest.split(b"\x00", 1)
    assert event == b""
    assert target == b""
    assert len(tail) == struct.calcsize("<qf")
