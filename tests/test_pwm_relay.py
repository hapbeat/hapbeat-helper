import pytest

from hapbeat_helper.server import HelperServer


@pytest.mark.asyncio
async def test_post_play_hold_relay_preserves_explicit_ms(monkeypatch):
    server = HelperServer()
    calls = []

    async def capture(ws, payload, cmd):
        calls.append((payload, cmd))

    monkeypatch.setattr(server, "_handle_tcp_command", capture)
    await server._dispatch(
        object(),
        {
            "type": "set_pwm_post_play_hold",
            "payload": {"targets": ["192.168.0.7"], "ms": 500},
        },
    )

    assert calls == [
        (
            {"targets": ["192.168.0.7"], "ms": 500},
            {"cmd": "set_pwm_post_play_hold", "ms": 500},
        )
    ]


@pytest.mark.asyncio
async def test_post_play_hold_relay_does_not_invent_missing_value(monkeypatch):
    server = HelperServer()
    commands = []

    async def capture(ws, payload, cmd):
        commands.append(cmd)

    monkeypatch.setattr(server, "_handle_tcp_command", capture)
    await server._dispatch(
        object(),
        {"type": "set_pwm_post_play_hold", "payload": {"targets": ["192.168.0.7"]}},
    )

    assert commands == [{"cmd": "set_pwm_post_play_hold"}]


@pytest.mark.asyncio
async def test_post_play_return_relay_preserves_explicit_ms(monkeypatch):
    server = HelperServer()
    calls = []

    async def capture(ws, payload, cmd):
        calls.append((payload, cmd))

    monkeypatch.setattr(server, "_handle_tcp_command", capture)
    await server._dispatch(
        object(),
        {
            "type": "set_pwm_post_play_return",
            "payload": {"targets": ["192.168.0.7"], "ms": 500},
        },
    )

    assert calls == [
        (
            {"targets": ["192.168.0.7"], "ms": 500},
            {"cmd": "set_pwm_post_play_return", "ms": 500},
        )
    ]


@pytest.mark.asyncio
async def test_haptic_output_mode_relay_preserves_explicit_mode(monkeypatch):
    server = HelperServer()
    calls = []

    async def capture(ws, payload, cmd):
        calls.append((payload, cmd))

    monkeypatch.setattr(server, "_handle_tcp_command", capture)
    await server._dispatch(
        object(),
        {
            "type": "set_haptic_output_mode",
            "payload": {"targets": ["192.168.0.7"], "mode": "pam"},
        },
    )

    assert calls == [
        (
            {"targets": ["192.168.0.7"], "mode": "pam"},
            {"cmd": "set_haptic_output_mode", "mode": "pam"},
        )
    ]


@pytest.mark.asyncio
async def test_haptic_output_mode_relay_does_not_invent_missing_mode(monkeypatch):
    server = HelperServer()
    commands = []

    async def capture(ws, payload, cmd):
        commands.append(cmd)

    monkeypatch.setattr(server, "_handle_tcp_command", capture)
    await server._dispatch(
        object(),
        {"type": "set_haptic_output_mode", "payload": {"targets": ["192.168.0.7"]}},
    )

    assert commands == [{"cmd": "set_haptic_output_mode"}]


@pytest.mark.asyncio
async def test_pwm_bias_enabled_relay_preserves_explicit_boolean(monkeypatch):
    server = HelperServer()
    calls = []

    async def capture(ws, payload, cmd):
        calls.append((payload, cmd))

    monkeypatch.setattr(server, "_handle_tcp_command", capture)
    await server._dispatch(
        object(),
        {
            "type": "set_pwm_bias_enabled",
            "payload": {"targets": ["192.168.0.7"], "enabled": False},
        },
    )

    assert calls == [
        (
            {"targets": ["192.168.0.7"], "enabled": False},
            {"cmd": "set_pwm_bias_enabled", "enabled": False},
        )
    ]
