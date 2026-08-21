"""`hapbeat-helper ota <target> <bin>` — argument parsing, image sanity,
target resolution, and the WS event loop up to ota_result.

The image checks matter most: a merged serial image also starts with 0xE9
(that byte belongs to the bootloader), so magic alone does not separate it
from an app image — pushing one over OTA would write bootloader bytes into
the app slot.
"""
import asyncio
import json

import pytest

from hapbeat_helper import cli, ota_client


# ── argparse ────────────────────────────────────────────────


def test_ota_subcommand_takes_target_and_bin():
    args = cli._build_parser().parse_args(["ota", "duo-01", "fw.bin"])
    assert args.func is cli._cmd_ota
    assert args.target == "duo-01"
    assert args.bin == "fw.bin"
    assert args.port == cli.WS_PORT


def test_ota_subcommand_accepts_port_override():
    args = cli._build_parser().parse_args(
        ["ota", "192.168.0.48", "fw.bin", "--port", "7999"]
    )
    assert args.port == 7999


def test_ota_subcommand_requires_both_operands():
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["ota", "192.168.0.48"])


# ── image validation ────────────────────────────────────────


def _app_image(size: int = 200 * 1024) -> bytearray:
    data = bytearray(size)
    data[0] = 0xE9   # magic
    data[1] = 4      # segment count
    data[12] = 0x09  # ESP32-S3
    return data


def test_valid_app_image_passes():
    assert ota_client.validate_ota_image(bytes(_app_image())) is None


def test_bad_magic_is_rejected():
    data = _app_image()
    data[0] = 0x00
    reason = ota_client.validate_ota_image(bytes(data))
    assert reason and "0xE9" in reason


def test_merged_serial_image_is_rejected_despite_valid_magic():
    # Merged layout: partition-table magic 0xAA 0x50 sits at 0x8000.
    data = _app_image(size = 300 * 1024)
    data[0x8000] = 0xAA
    data[0x8001] = 0x50
    reason = ota_client.validate_ota_image(bytes(data))
    assert reason and "merged" in reason


def test_truncated_file_is_rejected():
    reason = ota_client.validate_ota_image(bytes(_app_image(size=1024)))
    assert reason and "too small" in reason


def test_unknown_chip_id_is_rejected():
    data = _app_image()
    data[12] = 0x7F
    reason = ota_client.validate_ota_image(bytes(data))
    assert reason and "chip id" in reason


def test_missing_file_reports_an_error(tmp_path):
    data, err = ota_client.load_ota_image(str(tmp_path / "nope.bin"))
    assert data is None
    assert "no such file" in err


def test_load_ota_image_reads_a_valid_file(tmp_path):
    p = tmp_path / "firmware_app_ota.bin"
    p.write_bytes(bytes(_app_image()))
    data, err = ota_client.load_ota_image(str(p))
    assert err is None
    assert data is not None and data[0] == 0xE9


# ── target resolution ───────────────────────────────────────


DEVICES = [
    {"name": "duo-01", "ipAddress": "192.168.0.48", "online": True},
    {"name": "duo-02", "ipAddress": "192.168.0.49", "online": False},
    {"name": "dup", "ipAddress": "192.168.0.50", "online": True},
    {"name": "dup", "ipAddress": "192.168.0.51", "online": True},
]


def test_ip_target_passes_through():
    assert ota_client.resolve_target(DEVICES, "192.168.0.99") == (
        "192.168.0.99", None,
    )


def test_name_resolves_to_ip():
    assert ota_client.resolve_target(DEVICES, "duo-01")[0] == "192.168.0.48"


def test_offline_device_is_refused():
    ip, err = ota_client.resolve_target(DEVICES, "duo-02")
    assert ip is None
    assert "offline" in err


def test_duplicate_names_are_refused_with_both_ips():
    ip, err = ota_client.resolve_target(DEVICES, "dup")
    assert ip is None
    assert "192.168.0.50" in err and "192.168.0.51" in err


def test_unknown_name_lists_candidates():
    ip, err = ota_client.resolve_target(DEVICES, "ghost")
    assert ip is None
    assert "duo-01" in err  # candidate list is shown


# ── WS flow ─────────────────────────────────────────────────


class FakeWs:
    """Minimal stand-in for a websockets client connection."""

    def __init__(self, script: list[dict]) -> None:
        self._outbox = asyncio.Queue()
        self.sent: list[dict] = []
        self._script = script

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text: str) -> None:
        msg = json.loads(text)
        self.sent.append(msg)
        if msg["type"] == "list_devices":
            await self._outbox.put({
                "type": "device_list",
                "payload": {"devices": DEVICES},
            })
        elif msg["type"] == "ota_data":
            for item in self._script:
                await self._outbox.put(item)

    async def recv(self) -> str:
        return json.dumps(await self._outbox.get())


def _run_ws_ota(script, target="duo-01", monkeypatch=None):
    fake = FakeWs(script)

    async def fake_connect(uri, **kwargs):
        del uri, kwargs
        return fake

    import websockets
    monkeypatch.setattr(websockets, "connect", fake_connect)
    rc = asyncio.run(ota_client.run_ota_via_ws(
        7703, target, bytes(_app_image()), verbose=False,
    ))
    return rc, fake


def test_successful_ota_returns_zero_and_sends_the_image(monkeypatch):
    rc, fake = _run_ws_ota([
        {"type": "ota_progress",
         "payload": {"device": "192.168.0.48", "percent": 50, "message": "…"}},
        {"type": "ota_result",
         "payload": {"device": "192.168.0.48", "success": True, "message": "OK"}},
    ], monkeypatch=monkeypatch)
    assert rc == 0
    ota = [m for m in fake.sent if m["type"] == "ota_data"][0]
    assert ota["payload"]["ip"] == "192.168.0.48"
    assert ota["payload"]["bin_base64"]


def test_failed_ota_returns_one(monkeypatch):
    rc, _ = _run_ws_ota([
        {"type": "ota_result",
         "payload": {"device": "192.168.0.48", "success": False,
                     "message": "phase=stall"}},
    ], monkeypatch=monkeypatch)
    assert rc == 1


def test_events_for_another_device_are_ignored(monkeypatch):
    rc, _ = _run_ws_ota([
        # A concurrent OTA to a different device must not end our wait.
        {"type": "ota_result",
         "payload": {"device": "192.168.0.99", "success": False,
                     "message": "someone else"}},
        {"type": "ota_result",
         "payload": {"device": "192.168.0.48", "success": True, "message": "OK"}},
    ], monkeypatch=monkeypatch)
    assert rc == 0


def test_unresolvable_target_exits_two_without_sending_the_image(monkeypatch):
    rc, fake = _run_ws_ota([], target="ghost", monkeypatch=monkeypatch)
    assert rc == 2
    assert not [m for m in fake.sent if m["type"] == "ota_data"]
