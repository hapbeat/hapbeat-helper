"""Agent relay (agent_* messages) through HelperServer._handler.

Serves only the WebSocket handler on an ephemeral 127.0.0.1 port — no UDP /
mDNS subsystems are started, so nothing reaches real devices.
"""

import asyncio
import json

import pytest
import websockets

from hapbeat_helper.server import (
    AGENT_ERR_STUDIO_DISCONNECTED,
    AGENT_ERR_STUDIO_NOT_READY,
    HelperServer,
)


@pytest.fixture
async def relay():
    server = HelperServer()
    # Same max_size as HelperServer.run().
    async with websockets.serve(
        server._handler, "127.0.0.1", 0, max_size=64 * 1024 * 1024,
    ) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        yield server, f"ws://127.0.0.1:{port}"


async def _recv_type(ws, msg_type: str, timeout: float = 2.0) -> dict:
    """Next message of *msg_type*, skipping helper_hello / device_list etc."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=deadline - loop.time())
        msg = json.loads(raw)
        if msg["type"] == msg_type:
            return msg["payload"]


async def _assert_no_type(ws, msg_type: str, timeout: float = 0.3) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await _recv_type(ws, msg_type, timeout)


async def _send(ws, msg_type: str, payload: dict) -> None:
    await ws.send(json.dumps({"type": msg_type, "payload": payload}))


async def _register(ws) -> None:
    await _send(ws, "agent_endpoint_register", {"studioVersion": "t", "folderName": "f"})
    assert await _recv_type(ws, "agent_endpoint_registered") == {}


def _request(request_id: str = "r1") -> dict:
    return {"requestId": request_id, "method": "status", "params": {}}


async def test_request_without_endpoint_is_studio_not_ready(relay):
    _server, uri = relay
    async with websockets.connect(uri) as agent:
        await _send(agent, "agent_request", _request())
        assert await _recv_type(agent, "agent_response") == {
            "requestId": "r1", "ok": False, "error": AGENT_ERR_STUDIO_NOT_READY,
        }


async def test_request_is_forwarded_and_response_returned(relay):
    server, uri = relay
    async with websockets.connect(uri) as studio, websockets.connect(uri) as agent:
        await _register(studio)
        await _send(agent, "agent_request", _request())
        assert await _recv_type(studio, "agent_request") == _request()

        response = {"requestId": "r1", "ok": True, "result": {"clipCount": 3}}
        await _send(studio, "agent_response", response)
        assert await _recv_type(agent, "agent_response") == response
        assert server._agent_pending == {}


async def test_endpoint_disconnect_fails_pending_requests(relay):
    server, uri = relay
    async with websockets.connect(uri) as agent:
        studio = await websockets.connect(uri)
        await _register(studio)
        await _send(agent, "agent_request", _request("a"))
        await _recv_type(studio, "agent_request")
        await studio.close()

        assert await _recv_type(agent, "agent_response") == {
            "requestId": "a", "ok": False, "error": AGENT_ERR_STUDIO_DISCONNECTED,
        }
        assert server._agent_endpoint is None
        await _send(agent, "agent_request", _request("b"))
        assert (await _recv_type(agent, "agent_response"))["error"] == AGENT_ERR_STUDIO_NOT_READY


async def test_requester_disconnect_discards_its_requests(relay):
    server, uri = relay
    async with websockets.connect(uri) as studio:
        await _register(studio)
        agent = await websockets.connect(uri)
        await _send(agent, "agent_request", _request("gone"))
        await _recv_type(studio, "agent_request")
        await agent.close()
        for _ in range(50):
            if not server._agent_pending:
                break
            await asyncio.sleep(0.01)
        assert server._agent_pending == {}

        # A late answer has nowhere to go and must not disturb the endpoint.
        await _send(studio, "agent_response", {"requestId": "gone", "ok": True, "result": {}})
        await _assert_no_type(studio, "error")
        assert server._agent_endpoint is not None


async def test_last_registration_wins(relay):
    _server, uri = relay
    async with (
        websockets.connect(uri) as old_tab,
        websockets.connect(uri) as new_tab,
        websockets.connect(uri) as agent,
    ):
        await _register(old_tab)
        await _register(new_tab)
        await _send(agent, "agent_request", _request())
        assert await _recv_type(new_tab, "agent_request") == _request()
        await _assert_no_type(old_tab, "agent_request")


async def test_unregister_clears_endpoint(relay):
    server, uri = relay
    async with websockets.connect(uri) as studio, websockets.connect(uri) as agent:
        await _register(studio)
        await _send(studio, "agent_endpoint_unregister", {})
        await _send(agent, "agent_request", _request())
        assert (await _recv_type(agent, "agent_response"))["error"] == AGENT_ERR_STUDIO_NOT_READY
        assert server._agent_endpoint is None


async def test_unregister_from_non_endpoint_is_ignored(relay):
    server, uri = relay
    async with websockets.connect(uri) as studio, websockets.connect(uri) as other:
        await _register(studio)
        await _send(other, "agent_endpoint_unregister", {})
        await _send(other, "ping", {})
        await _recv_type(other, "pong")
        assert server._agent_endpoint is not None


async def test_response_from_other_client_is_ignored(relay):
    server, uri = relay
    async with (
        websockets.connect(uri) as studio,
        websockets.connect(uri) as agent,
        websockets.connect(uri) as intruder,
    ):
        await _register(studio)
        await _send(agent, "agent_request", _request())
        await _recv_type(studio, "agent_request")
        await _send(intruder, "agent_response", {"requestId": "r1", "ok": True, "result": {}})
        await _assert_no_type(agent, "agent_response")
        assert "r1" in server._agent_pending


@pytest.mark.parametrize("request_id", ["", "a/b", "x" * 65, 12, None, "abc\n"])
async def test_invalid_request_id_is_rejected_and_not_forwarded(relay, request_id):
    _server, uri = relay
    async with websockets.connect(uri) as studio, websockets.connect(uri) as agent:
        await _register(studio)
        await _send(agent, "agent_request", {"requestId": request_id, "method": "status"})
        assert "requestId" in (await _recv_type(agent, "error"))["message"]
        await _assert_no_type(studio, "agent_request")


async def test_oversized_request_is_rejected(relay):
    _server, uri = relay
    async with websockets.connect(uri, max_size=None) as studio, \
            websockets.connect(uri, max_size=None) as agent:
        await _register(studio)
        big = _request("big")
        big["params"] = {"blob": "x" * (4 * 1024 * 1024)}
        await _send(agent, "agent_request", big)
        assert (await _recv_type(agent, "agent_response"))["error"] == "PAYLOAD_TOO_LARGE"
        await _assert_no_type(studio, "agent_request")


async def test_agent_messages_are_not_broadcast(relay):
    _server, uri = relay
    async with (
        websockets.connect(uri) as studio,
        websockets.connect(uri) as agent,
        websockets.connect(uri) as bystander,
    ):
        await _register(studio)
        await _send(agent, "agent_request", _request())
        await _recv_type(studio, "agent_request")
        await _send(studio, "agent_response", {"requestId": "r1", "ok": True, "result": {}})
        await _recv_type(agent, "agent_response")
        await _assert_no_type(bystander, "agent_request")
        await _assert_no_type(bystander, "agent_response")


async def test_mcp_client_does_not_keep_log_threads_alive(relay):
    """Closing the last Studio tab stops log_tail threads even while an MCP
    server stays connected (it is not a Studio client)."""
    import threading

    server, uri = relay
    stop = threading.Event()
    server._log_threads["192.0.2.1"] = object()
    server._log_stop_flags["192.0.2.1"] = stop
    async with websockets.connect(uri) as agent:
        async with websockets.connect(uri) as studio:
            await _register(studio)
            await _send(agent, "agent_request", _request())
            await _recv_type(studio, "agent_request")
        await _recv_type(agent, "agent_response")  # STUDIO_DISCONNECTED
        for _ in range(50):
            if stop.is_set():
                break
            await asyncio.sleep(0.02)
        assert stop.is_set()
        assert server._log_threads == {}
