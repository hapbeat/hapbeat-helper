"""Health log: the run that degrades must leave its own diagnosis in the log."""
import asyncio
import logging
import time

import hapbeat_helper.server as server_mod
from hapbeat_helper.server import HelperServer


async def test_blocked_loop_is_warned_and_health_line_is_logged(monkeypatch, caplog):
    monkeypatch.setattr(server_mod, "LOOP_LAG_TICK_S", 0.02)
    monkeypatch.setattr(server_mod, "LOOP_STALL_WARN_S", 0.15)
    monkeypatch.setattr(server_mod, "HEALTH_LOG_INTERVAL_S", 0.5)
    server = HelperServer()
    caplog.set_level(logging.INFO, logger="hapbeat_helper.server")
    task = asyncio.create_task(server._health_loop())
    try:
        await asyncio.sleep(0.1)
        time.sleep(0.3)  # what a blocking call on the loop does to every stream packet
        await asyncio.sleep(0.6)
    finally:
        task.cancel()
    text = caplog.text
    assert "event loop stalled" in text
    assert "health: up=0h00m" in text
    assert "stalls=1" in text and "log_tails=0 (threads=0)" in text
