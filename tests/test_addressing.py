"""Device addressing: target/address matching + PLAY/STOP destination routing.

``address_matches`` decides which devices a packet is unicast to, so it must
agree with the firmware's ``addressMatch()`` — disagreeing either skips a device
that should have played (silent loss) or writes to one that ignores the packet
anyway. The table below is transcribed from
hapbeat-contracts/specs/device-addressing.md §4.2.
"""

import time

import pytest

from hapbeat_helper import protocol
from hapbeat_helper.device_registry import DeviceRegistry
from hapbeat_helper.server import _address_routed_ips, _explicit_ips


# (target, device address, expected) — contracts device-addressing.md §4.2
MATCH_TABLE = [
    ("", "player_1/pos_neck", True),                       # empty = all devices
    ("player_1", "player_1/pos_neck", True),               # front match
    ("player_1", "player_2/pos_neck", False),              # player mismatch
    ("player_1/pos_neck", "player_1/pos_neck", True),      # exact
    ("player_1/pos_neck", "player_1/pos_r_wrist", False),  # position mismatch
    ("*/pos_neck", "player_1/pos_neck", True),             # wildcard
    ("*/pos_neck", "player_2/pos_neck", True),
    # Firmware only treats a segment that is entirely "*" as a wildcard
    # (address_match.cpp: `t_len == 1 && *tp == '*'`), so "pos_*" is a literal.
    ("player_1/pos_*", "player_1/pos_neck", False),
    ("player_1/*", "player_1/pos_neck", True),             # whole-segment wildcard
    ("red", "red/player_1/pos_neck", True),                # prefixed address
    ("red/*/player_1", "red/alpha/player_1/pos_neck", True),
    ("player_1/pos_neck/group_1", "player_1/pos_neck/group_1", True),
    ("player_1/pos_neck/group_1", "player_1/pos_neck/group_2", False),
    ("player_1/pos_neck", "player_1/pos_neck/group_1", True),  # no group = any group
    ("*/*/group_1", "player_2/pos_chest/group_1", True),   # group only
    ("player_1/pos_neck/group_1", "player_1/pos_neck", False),  # target longer
]


@pytest.mark.parametrize("target,address,expected", MATCH_TABLE)
def test_address_matches_spec_table(target, address, expected):
    assert protocol.address_matches(target, address) is expected


def test_group_alone_never_matches():
    """The trap the positional rule sets: "group_2" lands in the player slot."""
    assert protocol.address_matches("group_2", "player_1/pos_neck/group_2") is False
    assert protocol.address_matches("*/*/group_2", "player_1/pos_neck/group_2") is True


def test_trailing_slash_matches_firmware():
    """Firmware's pointer walk ends at the terminator, so one trailing '/' is
    not an empty segment. Splitting naively would drop a device the firmware
    would have accepted."""
    assert protocol.address_matches("player_1/", "player_1/pos_neck") is True
    assert protocol.address_matches("player_1//", "player_1/pos_neck") is False


# ── Destination routing ────────────────────────────────────────────
def _registry(*devices) -> DeviceRegistry:
    reg = DeviceRegistry()
    for ip, address in devices:
        reg.upsert_device({"ip": ip, "address": address})
    return reg


def test_explicit_ips_win_over_address_routing():
    """Studio's device checkboxes must keep working without an address string."""
    assert _explicit_ips({"targets": ["10.0.0.1", "10.0.0.2"]}) == ["10.0.0.1", "10.0.0.2"]
    assert _explicit_ips({"ip": "10.0.0.3"}) == ["10.0.0.3"]
    # `target` is an address filter, not an IP — sending to it would hit a
    # bogus host.
    assert _explicit_ips({"target": "player_1/pos_neck"}) == []


def test_routes_to_every_online_device_when_target_is_empty():
    reg = _registry(("10.0.0.1", "player_1/pos_neck/group_1"),
                    ("10.0.0.2", "player_2/pos_neck/group_1"))
    assert sorted(_address_routed_ips("", reg)) == ["10.0.0.1", "10.0.0.2"]


def test_target_filters_destinations():
    reg = _registry(("10.0.0.1", "player_1/pos_neck/group_1"),
                    ("10.0.0.2", "player_2/pos_neck/group_1"))
    assert _address_routed_ips("player_1", reg) == ["10.0.0.1"]
    assert _address_routed_ips("*/*/group_1", reg) == ["10.0.0.1", "10.0.0.2"]


def test_unknown_address_fails_open():
    """A device that never reported an address stays a destination — the device
    applies the real filter, and dropping it would lose the command."""
    reg = _registry(("10.0.0.1", ""))
    assert _address_routed_ips("player_9/pos_neck", reg) == ["10.0.0.1"]


def test_offline_device_is_dropped():
    reg = _registry(("10.0.0.1", "player_1/pos_neck"))
    reg.get_device("10.0.0.1").last_seen = time.monotonic() - 60.0
    assert _address_routed_ips("", reg) == []  # caller falls back to broadcast


def test_no_match_returns_empty_so_caller_broadcasts():
    """Not a skip: a stale cached address must not swallow a STOP, or a looping
    clip would never stop. The caller broadcasts on []."""
    reg = _registry(("10.0.0.1", "player_1/pos_neck/group_1"))
    assert _address_routed_ips("player_7", reg) == []


def test_empty_registry_returns_empty():
    assert _address_routed_ips("", DeviceRegistry()) == []
