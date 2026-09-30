"""MCP tool layer: request shaping, reply handling, wait_for_rating polling.

The tool logic and the daemon link are tested without the ``mcp`` package;
only the registration test at the bottom needs it (skipped otherwise). The
daemon is faked by an in-process WebSocket server on an ephemeral port.
"""

import asyncio
import json
import socket

import pytest
import websockets

from hapbeat_helper.mcp_server import (
    HELPER_NOT_RUNNING,
    SUBMIT_TRIAL_TIMEOUT_S,
    AgentError,
    HapbeatAgentTools,
    HelperLink,
)


class _FakeRequester:
    def __init__(self, replies=None) -> None:
        self.calls: list[tuple[str, dict, float | None]] = []
        self._replies = list(replies or [])

    async def __call__(self, method, params, timeout=None):
        self.calls.append((method, params, timeout))
        if self._replies:
            reply = self._replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        return {}


@pytest.mark.parametrize(
    "invoke, method, params",
    [
        (lambda t: t.status(), "status", {}),
        (lambda t: t.get_guide(), "get_guide", {}),
        (lambda t: t.get_catalog(), "get_catalog", {}),
        (lambda t: t.get_knowledge(), "get_knowledge", {}),
        (lambda t: t.get_knowledge("thud"), "get_knowledge", {"term": "thud"}),
        (lambda t: t.get_trial("t1"), "get_trial", {"trialId": "t1"}),
        (lambda t: t.list_trials(), "list_trials", {}),
        (lambda t: t.list_trials(5, True), "list_trials", {"limit": 5, "unratedOnly": True}),
        (lambda t: t.audition("t1", "c1"), "audition", {"trialId": "t1", "candidateId": "c1"}),
        (
            lambda t: t.audition("t1", "c1", True), "audition",
            {"trialId": "t1", "candidateId": "c1", "play": True},
        ),
        (lambda t: t.adopt("t1", "c1"), "adopt", {"trialId": "t1", "candidateId": "c1"}),
        (
            lambda t: t.propose_insight("s", ["t1/c1"]), "propose_insight",
            {"statement": "s", "evidence": ["t1/c1"]},
        ),
    ],
)
async def test_tools_shape_requests(invoke, method, params):
    fake = _FakeRequester([{"ok": "result"}])
    assert await invoke(HapbeatAgentTools(fake)) == {"ok": "result"}
    assert fake.calls == [(method, params, None)]


async def test_submit_trial_uses_long_timeout():
    fake = _FakeRequester()
    trial = {"format": "hapbeat-trial@1", "id": "t1"}
    await HapbeatAgentTools(fake).submit_trial(trial)
    assert fake.calls == [("submit_trial", {"trial": trial}, SUBMIT_TRIAL_TIMEOUT_S)]


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


async def test_wait_for_rating_returns_once_rated():
    clock = _FakeClock()
    rating = {"best": "c2"}
    fake = _FakeRequester([
        {"trial": {}, "rating": None},
        {"trial": {}, "rating": None},
        {"trial": {}, "rating": rating},
    ])
    tools = HapbeatAgentTools(fake, sleep=clock.sleep, clock=clock)
    assert await tools.wait_for_rating("t1", 60) == {"status": "rated", "rating": rating}
    assert [c[0] for c in fake.calls] == ["get_trial"] * 3
    assert clock.now == 4.0


async def test_wait_for_rating_times_out_as_waiting():
    clock = _FakeClock()
    fake = _FakeRequester([{"rating": None}] * 100)
    tools = HapbeatAgentTools(fake, sleep=clock.sleep, clock=clock)
    assert await tools.wait_for_rating("t1", 10) == {"status": "waiting"}
    assert clock.now <= 10.0
    assert len(fake.calls) == 6  # t = 0, 2, 4, 6, 8, 10


async def test_wait_for_rating_propagates_errors():
    fake = _FakeRequester([AgentError("STUDIO_NOT_READY: x")])
    clock = _FakeClock()
    tools = HapbeatAgentTools(fake, sleep=clock.sleep, clock=clock)
    with pytest.raises(AgentError, match="STUDIO_NOT_READY"):
        await tools.wait_for_rating("t1", 30)


@pytest.mark.parametrize("timeout_sec", [9, 1801])
async def test_wait_for_rating_rejects_out_of_range_timeout(timeout_sec):
    fake = _FakeRequester()
    with pytest.raises(AgentError, match="timeoutSec"):
        await HapbeatAgentTools(fake).wait_for_rating("t1", timeout_sec)
    assert fake.calls == []


# ── HelperLink against a fake daemon ────────────────────────────────


def _fake_daemon(respond):
    """WS handler: greets like the real daemon, then answers agent_request
    with ``respond(payload)`` (None = stay silent / close)."""
    async def handler(ws):
        await ws.send(json.dumps({"type": "helper_hello", "payload": {"version": "x"}}))
        await ws.send(json.dumps({"type": "device_list", "payload": {"devices": []}}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] != "agent_request":
                continue
            # Unrelated pushes between request and reply must be ignored.
            await ws.send(json.dumps({"type": "device_list", "payload": {"devices": []}}))
            reply = respond(msg["payload"])
            if reply == "close":
                await ws.close()
                return
            if reply is not None:
                await ws.send(json.dumps({"type": "agent_response", "payload": reply}))
    return handler


async def test_link_returns_result_and_reuses_connection():
    connections = []

    def respond(p):
        return {"requestId": p["requestId"], "ok": True, "result": {"method": p["method"]}}

    handler = _fake_daemon(respond)

    async def counting(ws):
        connections.append(ws)
        await handler(ws)

    async with websockets.serve(counting, "127.0.0.1", 0) as srv:
        link = HelperLink(srv.sockets[0].getsockname()[1])
        assert await link.request("status", {}) == {"method": "status"}
        assert await link.request("get_guide", {}) == {"method": "get_guide"}
        assert len(connections) == 1
        await link.close()


async def test_link_raises_studio_error_text():
    def respond(p):
        return {"requestId": p["requestId"], "ok": False, "error": "STUDIO_NOT_READY: open"}

    async with websockets.serve(_fake_daemon(respond), "127.0.0.1", 0) as srv:
        link = HelperLink(srv.sockets[0].getsockname()[1])
        with pytest.raises(AgentError, match="^STUDIO_NOT_READY: open$"):
            await link.request("status", {})
        await link.close()


async def test_link_times_out():
    async with websockets.serve(_fake_daemon(lambda p: None), "127.0.0.1", 0) as srv:
        link = HelperLink(srv.sockets[0].getsockname()[1])
        with pytest.raises(AgentError, match="^TIMEOUT"):
            await link.request("status", {}, timeout=0.2)
        await link.close()


async def test_link_reconnects_after_drop():
    calls = {"n": 0}

    def respond(p):
        calls["n"] += 1
        if calls["n"] == 1:
            return "close"
        return {"requestId": p["requestId"], "ok": True, "result": {}}

    async with websockets.serve(_fake_daemon(respond), "127.0.0.1", 0) as srv:
        link = HelperLink(srv.sockets[0].getsockname()[1])
        with pytest.raises(AgentError, match="^HELPER_NOT_RUNNING"):
            await link.request("status", {})
        await asyncio.sleep(0.05)
        assert await link.request("status", {}) == {}
        await link.close()


async def test_link_without_daemon_reports_helper_not_running():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    link = HelperLink(port)
    with pytest.raises(AgentError) as exc:
        await link.request("status", {})
    assert str(exc.value) == HELPER_NOT_RUNNING


# ── MCP registration (needs the optional `mcp` extra) ───────────────


async def test_mcp_server_registers_all_tools():
    pytest.importorskip("mcp")
    from hapbeat_helper.mcp_server import build_mcp_server

    fake = _FakeRequester([{"clipCount": 1}, AgentError("STUDIO_NOT_READY: x")])
    server = build_mcp_server(HapbeatAgentTools(fake))
    names = {t.name for t in await server.list_tools()}
    assert names == {
        "status", "get_guide", "get_catalog", "get_knowledge", "submit_trial",
        "get_trial", "list_trials", "audition", "adopt", "propose_insight",
        "wait_for_rating",
    }
    await server.call_tool("status", {})
    assert fake.calls[0][0] == "status"
    with pytest.raises(Exception, match="STUDIO_NOT_READY"):
        await server.call_tool("get_trial", {"trialId": "t1"})
