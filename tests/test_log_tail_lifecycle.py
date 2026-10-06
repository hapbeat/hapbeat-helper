"""log_tail supervisor lifecycle: a re-subscribe must never orphan a thread.

The device has one TCP slot, so every log_tail thread left running fights the
others (and Studio's own commands) for it. A thread nobody can stop survives
every Studio reload and only a helper restart clears it. No sockets here: the
TCP worker is replaced by a fake that returns like a displaced tail.
"""
import asyncio
import threading

import pytest

import hapbeat_helper.server as server_mod
from hapbeat_helper.server import HelperServer

IP = "192.0.2.10"


class _FakeWebSocket:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


def _tail_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate()
            if t.name == f"log-tail-sup-{IP}" and t.is_alive()]


async def _wait_until(cond, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


@pytest.fixture
def server(monkeypatch):
    def displaced_worker(ip, stop, relay):
        # A real tail returns as soon as a command displaces it; the supervisor
        # then sits in its reconnect backoff — the window the race needs.
        stop.wait(0.01)

    monkeypatch.setattr(server_mod, "_log_tail_worker", displaced_worker)
    srv = HelperServer()
    yield srv
    srv._stop_all_log_tails()


async def test_resubscribe_during_backoff_keeps_the_new_thread_stoppable(server):
    ws = _FakeWebSocket()
    await server._handle_subscribe_logs(ws, {"ip": IP})
    await asyncio.sleep(0.1)  # first worker returned; supervisor is backing off

    await server._handle_unsubscribe_logs(ws, {"ip": IP})
    await server._handle_subscribe_logs(ws, {"ip": IP})

    # The stopped supervisor exits; it must not take the new one's entry with it.
    assert await _wait_until(lambda: len(_tail_threads()) <= 1)
    assert IP in server._log_threads and IP in server._log_stop_flags

    await server._handle_unsubscribe_logs(ws, {"ip": IP})
    assert await _wait_until(lambda: not _tail_threads()), (
        "a log_tail thread survived unsubscribe — it would hold the device's "
        "TCP slot until the helper restarts"
    )


async def test_studio_reloads_do_not_accumulate_tail_threads(server):
    """Each reload: the last Studio socket closes (tails stopped) and the new
    page subscribes again within the reconnect backoff."""
    for _ in range(5):
        await server._handle_subscribe_logs(_FakeWebSocket(), {"ip": IP})
        await asyncio.sleep(0.05)
        server._stop_all_log_tails()  # what _handler does for the last Studio tab
    await server._handle_subscribe_logs(_FakeWebSocket(), {"ip": IP})
    assert await _wait_until(lambda: len(_tail_threads()) <= 1)

    server._stop_all_log_tails()
    assert await _wait_until(lambda: not _tail_threads())


async def test_stop_does_not_wait_out_the_backoff(server):
    ws = _FakeWebSocket()
    await server._handle_subscribe_logs(ws, {"ip": IP})
    await asyncio.sleep(0.1)
    await server._handle_unsubscribe_logs(ws, {"ip": IP})
    # The reconnect backoff is 1-4 s.
    assert await _wait_until(lambda: not _tail_threads(), timeout=0.5)
