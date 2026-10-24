"""Layer 1 UDP protocol helpers for Hapbeat communication.

Packet format uses path-based device addressing.
See hapbeat-contracts/specs/device-addressing.md for full spec.
"""

import struct
from typing import Optional

MAGIC = 0x4842  # "HB"
VERSION = 0x01
STREAM_VERSION = 0x02  # STREAM_BEGIN/DATA/END in Stream Gen 2 (stream-session.md)
HEADER_SIZE = 8
STREAM_TAIL_MARKER = b"HBS2"
STREAM_TAIL_SIZE = 32

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

# ERROR codes (contracts message-format.md §0xFF).
ERR_UNSUPPORTED_VERSION = 0x0001
ERR_UNKNOWN_COMMAND = 0x0002
ERR_MALFORMED = 0x0003
ERR_STREAM_GEN_UNSUPPORTED = 0x0010
ERR_STREAM_LEASE_INVALID = 0x0011
ERR_STREAM_SUPERSEDED = 0x0012
ERR_STREAM_NOT_STARTED = 0x0013
ERR_STREAM_TIMED_OUT = 0x0014
ERR_STREAM_STOPPED = 0x0015
ERR_STREAM_ENDED = 0x0016
ERR_STREAM_TARGET_MISMATCH = 0x0017
# Codes whose detail is the rejected packet's 16-byte stream envelope.
STREAM_ERROR_CODES = range(ERR_STREAM_LEASE_INVALID, ERR_STREAM_TARGET_MISMATCH + 1)
ERROR_NAMES = {
    ERR_UNSUPPORTED_VERSION: "UNSUPPORTED_VERSION",
    ERR_UNKNOWN_COMMAND: "UNKNOWN_COMMAND",
    ERR_MALFORMED: "MALFORMED",
    ERR_STREAM_GEN_UNSUPPORTED: "STREAM_GEN_UNSUPPORTED",
    ERR_STREAM_LEASE_INVALID: "STREAM_LEASE_INVALID",
    ERR_STREAM_SUPERSEDED: "STREAM_SUPERSEDED",
    ERR_STREAM_NOT_STARTED: "STREAM_NOT_STARTED",
    ERR_STREAM_TIMED_OUT: "STREAM_TIMED_OUT",
    ERR_STREAM_STOPPED: "STREAM_STOPPED",
    ERR_STREAM_ENDED: "STREAM_ENDED",
    ERR_STREAM_TARGET_MISMATCH: "STREAM_TARGET_MISMATCH",
}


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


def build_header(command_type: int, seq: int, payload_length: int,
                 version: int = VERSION) -> bytes:
    """Build 8-byte Layer 1 header.

    Header layout (little-endian):
        - magic:          uint16  (0x4842 = "HB")
        - version:        uint8   (0x01; 0x02 for v2 stream commands)
        - command_type:   uint8
        - seq:            uint16  (sequence number)
        - payload_length: uint16
    """
    return struct.pack(
        "<HBBHH", MAGIC, version, command_type, seq, payload_length,
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
    pan: float = 0.0,
    group: int = 0,  # legacy compat — ignored if target is set
) -> bytes:
    """Build a PLAY command packet.

    `pan` (-1.0 left / 0.0 center / +1.0 right) is the trailing optional field
    of the 0x01 payload (DEC-055). We always emit it; older firmware simply
    reads the payload up to `gain` and ignores the extra 4 bytes.
    """
    event_bytes = event_id.encode("utf-8") + b"\x00"
    target_bytes = target.encode("utf-8") + b"\x00"
    pan = max(-1.0, min(1.0, float(pan)))
    payload = (
        event_bytes
        + target_bytes
        + struct.pack("<qff", target_time_us, gain, pan)
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


def build_ping(seq: int, timestamp_us: int, client_incarnation: int = 0) -> bytes:
    """PING. 8 bytes for discovery; a nonzero ``client_incarnation`` makes it
    the 16-byte extended PING that requests a Stream Gen 2 lease (Stream Gen 1
    firmware reads only the timestamp). The PING format is frozen."""
    payload = struct.pack("<q", timestamp_us)
    if client_incarnation:
        payload += struct.pack("<Q", client_incarnation)
    return build_header(CMD_PING, seq, len(payload)) + payload


def build_stream_begin(
    seq: int,
    sample_rate: int = 16000,
    channels: int = 1,
    fmt: int = 1,
    total_samples: int = 0,
    gain: float = 1.0,
    target: str = "",
) -> bytes:
    payload = (
        struct.pack("<HBBIf", sample_rate, channels, fmt, total_samples, gain)
        + target.encode("utf-8") + b"\x00"
    )
    return build_header(CMD_STREAM_BEGIN, seq, len(payload)) + payload


def build_stream_data(seq: int, offset: int, data: bytes) -> bytes:
    payload = struct.pack("<I", offset) + data
    return build_header(CMD_STREAM_DATA, seq, len(payload)) + payload


def build_stream_end(seq: int) -> bytes:
    return build_header(CMD_STREAM_END, seq, 0)


# Stream Gen 2 (hapbeat-contracts/specs/stream-session.md): header version 2
# and a 16-byte (boot id, lease ticket, generation) envelope. The builders
# above stay the Stream Gen 1 format that firmware 0.4.x and older understands.

def stream_envelope(boot_id: int, ticket: int, generation: int) -> bytes:
    if not boot_id or not ticket or not generation:
        raise ValueError("Stream Gen 2 identity fields must be nonzero")
    return struct.pack("<QII", boot_id, ticket, generation)


def build_stream_begin_v2(
    seq: int,
    boot_id: int,
    ticket: int,
    generation: int,
    sample_rate: int = 16000,
    channels: int = 1,
    fmt: int = 1,
    total_samples: int = 0,
    gain: float = 1.0,
    target: str = "",
) -> bytes:
    payload = (
        stream_envelope(boot_id, ticket, generation)
        + struct.pack("<HBBIf", sample_rate, channels, fmt, total_samples, gain)
    )
    if target:
        payload += target.encode("utf-8") + b"\x00"
    return build_header(CMD_STREAM_BEGIN, seq, len(payload), STREAM_VERSION) + payload


def build_stream_data_v2(seq: int, boot_id: int, ticket: int, generation: int,
                         offset: int, data: bytes) -> bytes:
    payload = stream_envelope(boot_id, ticket, generation) + struct.pack("<I", offset) + data
    return build_header(CMD_STREAM_DATA, seq, len(payload), STREAM_VERSION) + payload


def build_stream_end_v2(seq: int, boot_id: int, ticket: int, generation: int) -> bytes:
    payload = stream_envelope(boot_id, ticket, generation)
    return build_header(CMD_STREAM_END, seq, len(payload), STREAM_VERSION) + payload


def parse_stream_tail(rest: bytes) -> dict:
    """Classify the bytes after volume_wiper in a PONG (stream-session.md
    Receiver classification, 1.x rules).

    ``absent``: no byte after volume_wiper (Stream Gen 1 firmware).
    ``valid``: at least 32 bytes starting with ``HBS2`` and extension_version 1;
    only the first 32 bytes are read, and the reserved field and flag bits
    other than 0-2 are ignored (a later major may use them).
    ``ignored``: anything else; callers must not change class or lease for it.
    """
    if not rest:
        return {"status": "absent"}
    if len(rest) < STREAM_TAIL_SIZE or rest[:4] != STREAM_TAIL_MARKER:
        return {"status": "ignored"}
    version, flags, _reserved, echoed, boot_id, ticket, high_water = struct.unpack(
        "<BBHQQII", rest[4:STREAM_TAIL_SIZE],
    )
    if version != 1:
        return {"status": "ignored"}
    return {
        "status": "valid",
        "lease_valid": bool(flags & 0x01) and boot_id != 0 and ticket != 0,
        "superseded": bool(flags & 0x02),
        "lease_reissued": bool(flags & 0x04),
        "echoed_incarnation": echoed,
        "boot_id": boot_id,
        "ticket": ticket,
        "high_water_ticket": high_water,
    }


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
        - stream lease tail: 32 bytes starting with "HBS2", only in a
          Stream Gen 2 device's direct reply to a 16-byte PING

    ``stream_tail`` is always set: {"status": "absent"|"ignored"|"valid", ...}.
    Firmware never sent volume_steps in a PONG (it comes from get_info); the
    byte after volume_wiper is the lease tail marker.
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
        "stream_tail": {"status": "absent"},
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
            result["stream_tail"] = parse_stream_tail(rest[2:])

    return result


def parse_error(data: bytes) -> Optional[dict]:
    """Parse an ERROR (0xFF, contracts message-format.md §0xFF).

    Returns ``error_code`` and ``message``, plus ``ref_command``, ``ref_seq``
    and ``detail`` (bytes) when the 1.x reference fields are present, or None
    for anything that is not a well-formed ERROR.
    """
    hdr = parse_header(data)
    if not hdr or hdr["command_type"] != CMD_ERROR:
        return None
    payload = data[HEADER_SIZE:HEADER_SIZE + hdr["payload_length"]]
    if len(payload) < 3:
        return None
    (code,) = struct.unpack_from("<H", payload)
    end = payload.find(b"\x00", 2)
    if end < 0:
        return None
    result: dict = {
        "seq": hdr["seq"],
        "error_code": code,
        "message": payload[2:end].decode("utf-8", errors="replace"),
    }
    offset = end + 1
    if len(payload) >= offset + 4:
        ref_command, ref_seq, detail_length = struct.unpack_from("<BHB", payload, offset)
        result.update({
            "ref_command": ref_command,
            "ref_seq": ref_seq,
            "detail": bytes(payload[offset + 4:offset + 4 + detail_length]),
        })
    return result
