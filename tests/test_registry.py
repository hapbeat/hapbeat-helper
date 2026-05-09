"""DeviceRegistry behaviour."""

import time

from hapbeat_helper.device_registry import DeviceRegistry


def test_upsert_and_listener_fires():
    reg = DeviceRegistry()
    calls: list[None] = []
    reg.add_change_listener(lambda: calls.append(None))

    reg.upsert_device({"ip": "10.0.0.1", "name": "hb-1", "firmware": "v1"})
    assert "10.0.0.1" in reg.get_all_devices()
    assert reg.get_device("10.0.0.1").name == "hb-1"
    assert len(calls) == 1


def test_volume_update():
    reg = DeviceRegistry()
    reg.upsert_device({"ip": "10.0.0.1"})
    reg.update_volume("10.0.0.1", level=64, wiper=32, steps=8)
    dev = reg.get_device("10.0.0.1")
    assert dev.volume_level == 64
    assert dev.volume_wiper == 32
    assert dev.volume_steps == 8


def test_offline_after_threshold():
    reg = DeviceRegistry()
    reg.upsert_device({"ip": "10.0.0.1"})
    dev = reg.get_device("10.0.0.1")
    # Simulate stale last_seen
    dev.last_seen = time.monotonic() - 999
    assert dev.is_online is False
