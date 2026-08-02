"""Layer 1 UDP protocol helpers for Hapbeat communication.

Packet format uses path-based device addressing.
See hapbeat-contracts/specs/device-addressing.md for full spec.
"""

import struct
from typing import Optional

MAGIC = 0x4842  # "HB"
VERSION = 0x01
HEADER_SIZE = 8

# Command types
CMD_PLAY = 0x01
CMD_STOP = 0x02
CMD_STOP_ALL = 0x03
CMD_PING = 0x10
CMD_PONG = 0x11
CMD_STREAM_BEGIN = 0x30
CMD_STREAM_DATA = 0x31
CMD_STREAM_END = 0x32
CMD_ERROR = 0xFF


def address_matches(target: Optional[str], device_address: Optional[str]) -> bool:
    """Does ``device_address`` accept a packet aimed at ``target``?

    A port of the firmware's ``addressMatch()``
    (hapbeat-device-firmware/src/address_match.cpp), which is the authority:
    the device re-applies this filter on receipt, so the helper only uses it to
    pick unicast destinations, never to decide what plays.

    Rules (contracts/specs/device-addressing.md §4.3):

    - an empty target matches every address;
    - both strings are split on ``/`` and compared **positionally**, so
      ``group_2`` alone is compared against the *player* slot and never
      matches — write ``*/*/group_2``;
    - a ``*`` target segment matches any single address segment, but only when
      the whole segment is ``*`` (``pos_*`` is compared literally);
    - fewer target segments than address segments is a front-match;
    - more target segments than address segments is a mismatch;
    - a single trailing ``/`` is ignored (``"player_1/" == "player_1"``),
      because firmware walks pointers and exits at the terminator rather than
      producing an empty segment. Splitting naively would drop a device the
      firmware *would* have accepted.
    """
    if not target:
        return True

    target_segments = target.split("/")
    address_segments = (device_address or "").split("/")

    # Only the last empty segment is dropped, and only once: "a//" really does
    # compare an empty segment in firmware, and still mismatches.
    count = len(target_segments)
    if count > 1 and target_segments[count - 1] == "":
        count -= 1

    for i in range(count):
        if i >= len(address_segments):
            return False  # target longer than address = mismatch
        if target_segments[i] != "*" and target_segments[i] != address_segments[i]:
            return False

    return True  # front-match or exact match


def build_header(command_type: int, seq: int, payload_length: int) -> bytes:
    """Build 8-byte Layer 1 header.

    Header layout (little-endian):
        - magic:          uint16  (0x4842 = "HB")
        - version:        uint8   (0x01)
        - command_type:   uint8
        - seq:            uint16  (sequence number)
        - payload_length: uint16
    """
    return struct.pack(
        "<HBBHH", MAGIC, VERSION, command_type, seq, payload_length,
    )


def parse_header(data: bytes) -> Optional[dict]:
    """Parse Layer 1 header. Returns dict or None if invalid."""
    if len(data) < HEADER_SIZE:
        return None
    magic, version, cmd, seq, payload_len = struct.unpack(
        "<HBBHH", data[:HEADER_SIZE],
    )
    if magic != MAGIC or version != VERSION:
        return None
    return {
        "command_type": cmd,
        "seq": seq,
        "payload_length": payload_len,
    }


def build_play(
    seq: int,
    event_id: str,
    target: str = "",
    target_time_us: int = 0,
    gain: float = 1.0,
    group: int = 0,  # legacy compat — ignored if target is set
) -> bytes:
    """Build a PLAY command packet."""
    event_bytes = event_id.encode("utf-8") + b"\x00"
    target_bytes = target.encode("utf-8") + b"\x00"
    payload = (
        event_bytes
        + target_bytes
        + struct.pack("<qf", target_time_us, gain)
    )
    return build_header(CMD_PLAY, seq, len(payload)) + payload


def build_stop(seq: int, event_id: str, target: str = "") -> bytes:
    event_bytes = event_id.encode("utf-8") + b"\x00"
    target_bytes = target.encode("utf-8") + b"\x00"
    payload = event_bytes + target_bytes
    return build_header(CMD_STOP, seq, len(payload)) + payload


def build_stop_all(seq: int, target: str = "") -> bytes:
    target_bytes = target.encode("utf-8") + b"\x00"
    return build_header(CMD_STOP_ALL, seq, len(target_bytes)) + target_bytes


def build_ping(seq: int, timestamp_us: int) -> bytes:
    payload = struct.pack("<q", timestamp_us)
    return build_header(CMD_PING, seq, len(payload)) + payload


def build_stream_begin(
    seq: int,
    sample_rate: int = 16000,
    channels: int = 1,
    fmt: int = 1,
    total_samples: int = 0,
    gain: float = 1.0,
) -> bytes:
    payload = struct.pack("<HBBIf", sample_rate, channels, fmt, total_samples, gain)
    return build_header(CMD_STREAM_BEGIN, seq, len(payload)) + payload


def build_stream_data(seq: int, offset: int, data: bytes) -> bytes:
    payload = struct.pack("<I", offset) + data
    return build_header(CMD_STREAM_DATA, seq, len(payload)) + payload


def build_stream_end(seq: int) -> bytes:
    return build_header(CMD_STREAM_END, seq, 0)


def parse_pong(data: bytes) -> Optional[dict]:
    """Parse a PONG response (device extended format).

    Expected payload layout:
        - timestamp:        int64
        - server_time:      int64
        - device_name:      null-terminated UTF-8 string
        - address:          null-terminated UTF-8 string
        - firmware_version: null-terminated UTF-8 string
        - volume_level:     uint8 (trailing, optional)
        - volume_wiper:     uint8 (trailing, optional)
        - volume_steps:     uint8 (trailing, optional)
    """
    hdr = parse_header(data)
    if not hdr or hdr["command_type"] != CMD_PONG:
        return None

    payload = data[HEADER_SIZE:]
    if len(payload) < 16:
        return None

    timestamp, server_time = struct.unpack("<qq", payload[:16])
    result: dict = {
        "seq": hdr["seq"],
        "timestamp": timestamp,
        "server_time": server_time,
    }

    if len(payload) > 16:
        rest = payload[16:]

        def _read_str(buf: bytes) -> tuple[str, bytes]:
            idx = buf.find(b"\x00")
            if idx < 0:
                return buf.decode("utf-8", errors="replace"), b""
            return buf[:idx].decode("utf-8", errors="replace"), buf[idx + 1:]

        result["device_name"], rest = _read_str(rest)
        if rest:
            result["address"], rest = _read_str(rest)
        if rest:
            result["firmware_version"], rest = _read_str(rest)

        if len(rest) >= 1:
            result["volume_level"] = rest[0]
        if len(rest) >= 2:
            result["volume_wiper"] = rest[1]
        if len(rest) >= 3:
            result["volume_steps"] = rest[2]

    return result
