"""MCP server (stdio) exposing Hapbeat Studio's AI-trial workflow to agents.

``hapbeat-helper mcp`` runs this. It is a thin client: every tool becomes an
``agent_request`` sent to the running helper daemon over its WebSocket, which
relays it to the Studio tab that registered as the agent endpoint. Studio does
the work (validation, rendering, knowledge base writes) and its
``agent_response`` is returned as the tool result. Nothing here duplicates that
logic.

stdout belongs to the MCP protocol; logs go to stderr.

The ``mcp`` package is an optional extra (``hapbeat-helper[mcp]``) and is only
imported inside :func:`build_mcp_server`, so the rest of this module (and the
tests for it) work without it.
"""

# No ``from __future__ import annotations`` here: the MCP SDK evaluates the
# tool signatures, and the tools are defined inside build_mcp_server with
# annotations (pydantic Field, local aliases) that only exist in that scope.

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0
SUBMIT_TRIAL_TIMEOUT_S = 120.0
RATING_POLL_INTERVAL_S = 2.0
WAIT_FOR_RATING_DEFAULT_S = 300
WAIT_FOR_RATING_MIN_S = 10
WAIT_FOR_RATING_MAX_S = 1800
# Relay payloads are capped at 4 MB by the daemon; leave room for the envelope.
_WS_MAX_SIZE = 8 * 1024 * 1024

HELPER_NOT_RUNNING = (
    'HELPER_NOT_RUNNING: start it with "hapbeat-helper start" (or install-service)'
)


class AgentError(Exception):
    """A request that failed in an expected way; the message goes to the agent."""


class HelperLink:
    """One reusable WebSocket connection to the helper daemon.

    Connects lazily on the first request and reconnects on the next request
    after the connection drops. Replies are matched by ``requestId``; anything
    else the daemon pushes (``helper_hello``, ``device_list``, ...) is ignored.
    """

    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self.uri = f"ws://{host}:{port}"
        self._ws: Any = None
        self._reader: Optional[asyncio.Task] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._connect_lock = asyncio.Lock()

    async def request(
        self, method: str, params: dict, timeout: float = DEFAULT_TIMEOUT_S,
    ) -> Any:
        """Send one ``agent_request`` and return ``result`` of the reply.

        Raises :class:`AgentError` when the daemon is unreachable, the reply
        does not arrive in *timeout* seconds, or Studio answered ``ok: false``.
        """
        from websockets.exceptions import ConnectionClosed

        ws = await self._ensure_connected()
        request_id = uuid.uuid4().hex
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await ws.send(json.dumps({
                "type": "agent_request",
                "payload": {"requestId": request_id, "method": method, "params": params},
            }))
            reply = await asyncio.wait_for(future, timeout=timeout)
        except ConnectionClosed:
            raise AgentError(HELPER_NOT_RUNNING) from None
        except asyncio.TimeoutError:
            raise AgentError(
                f"TIMEOUT: no reply from Studio for {method!r} within {timeout:g} s"
            ) from None
        finally:
            self._pending.pop(request_id, None)
        if not reply.get("ok"):
            raise AgentError(str(reply.get("error") or "unknown error"))
        return reply.get("result")

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._reader is not None:
            self._reader.cancel()

    async def _ensure_connected(self) -> Any:
        async with self._connect_lock:
            if self._ws is not None and self._reader is not None and not self._reader.done():
                return self._ws
            import websockets

            try:
                ws = await websockets.connect(self.uri, max_size=_WS_MAX_SIZE)
            except OSError:
                raise AgentError(HELPER_NOT_RUNNING) from None
            self._ws = ws
            self._reader = asyncio.create_task(self._read_loop(ws))
            logger.info("connected to helper daemon at %s", self.uri)
            return ws

    async def _read_loop(self, ws) -> None:
        from websockets.exceptions import ConnectionClosed

        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(msg, dict) or msg.get("type") != "agent_response":
                    continue
                payload = msg.get("payload")
                if not isinstance(payload, dict):
                    continue
                future = self._pending.get(payload.get("requestId"))
                if future is not None and not future.done():
                    future.set_result(payload)
        except ConnectionClosed:
            pass
        finally:
            logger.info("helper daemon connection closed")
            if self._ws is ws:
                self._ws = None
            # Fail whatever was still waiting on this connection now rather
            # than at its timeout.
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(AgentError(HELPER_NOT_RUNNING))


Requester = Callable[..., Awaitable[Any]]


class HapbeatAgentTools:
    """Tool behaviour, independent of the MCP SDK.

    *request* is ``HelperLink.request`` (or a test double) with the signature
    ``(method, params, timeout=...) -> result``.
    """

    def __init__(
        self,
        request: Requester,
        *,
        poll_interval: float = RATING_POLL_INTERVAL_S,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._request = request
        self._poll_interval = poll_interval
        self._sleep = sleep
        self._clock = clock

    async def status(self) -> Any:
        return await self._request("status", {})

    async def get_guide(self) -> Any:
        return await self._request("get_guide", {})

    async def get_catalog(self) -> Any:
        return await self._request("get_catalog", {})

    async def get_knowledge(self, term: Optional[str] = None) -> Any:
        params: dict[str, Any] = {}
        if term is not None:
            params["term"] = term
        return await self._request("get_knowledge", params)

    async def submit_trial(self, trial: dict) -> Any:
        return await self._request(
            "submit_trial", {"trial": trial}, timeout=SUBMIT_TRIAL_TIMEOUT_S,
        )

    async def get_trial(self, trialId: str) -> Any:  # noqa: N803 — wire name
        return await self._request("get_trial", {"trialId": trialId})

    async def list_trials(
        self, limit: Optional[int] = None, unratedOnly: Optional[bool] = None,  # noqa: N803
    ) -> Any:
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if unratedOnly is not None:
            params["unratedOnly"] = unratedOnly
        return await self._request("list_trials", params)

    async def audition(
        self, trialId: str, candidateId: str, play: Optional[bool] = None,  # noqa: N803
    ) -> Any:
        params: dict[str, Any] = {"trialId": trialId, "candidateId": candidateId}
        if play is not None:
            params["play"] = play
        return await self._request("audition", params)

    async def adopt(self, trialId: str, candidateId: str) -> Any:  # noqa: N803
        return await self._request(
            "adopt", {"trialId": trialId, "candidateId": candidateId},
        )

    async def propose_insight(self, statement: str, evidence: list[str]) -> Any:
        return await self._request(
            "propose_insight", {"statement": statement, "evidence": evidence},
        )

    async def wait_for_rating(
        self, trialId: str, timeoutSec: int = WAIT_FOR_RATING_DEFAULT_S,  # noqa: N803
    ) -> dict:
        """Poll ``get_trial`` until the human has rated it or time runs out."""
        if not WAIT_FOR_RATING_MIN_S <= timeoutSec <= WAIT_FOR_RATING_MAX_S:
            raise AgentError(
                f"timeoutSec must be {WAIT_FOR_RATING_MIN_S}-{WAIT_FOR_RATING_MAX_S}"
            )
        deadline = self._clock() + timeoutSec
        while True:
            trial = await self.get_trial(trialId)
            rating = trial.get("rating") if isinstance(trial, dict) else None
            if rating is not None:
                return {"status": "rated", "rating": rating}
            if self._clock() + self._poll_interval > deadline:
                return {"status": "waiting"}
            await self._sleep(self._poll_interval)


# ── MCP surface ─────────────────────────────────────────────────────

SERVER_INSTRUCTIONS = """\
Design haptic (vibration) clips together with a human in Hapbeat Studio.
Call get_guide first and follow it. Typical loop: get_guide -> get_knowledge
-> submit_trial -> audition (optional) -> wait_for_rating -> learn from the
rating and submit the next trial. With several trials waiting at once, watch
the folder for saved ratings instead (see the guide's "Receiving ratings").
Only the human rates candidates; you never write ratings. Requires the hapbeat-helper daemon running and Hapbeat Studio's
Waveform editor open on a folder."""

_DESCRIPTIONS = {
    "status": (
        "Check that Hapbeat Studio is reachable and ready. Returns the Studio "
        "version, the open folder name, clip / trial / unrated-trial counts and "
        "the perceptual dimensions in use. Call this when starting a session or "
        "when another tool reports STUDIO_NOT_READY / HELPER_NOT_RUNNING."
    ),
    "get_guide": (
        "Read this FIRST. Returns the agent guide (markdown): the hapbeat-trial@1 "
        "format that submit_trial expects, how candidates are rendered and how "
        "the human rates them. Re-read it if submit_trial rejects your trial."
    ),
    "get_catalog": (
        "Return the catalog of existing clips in the open folder (names, "
        "features, tags), for reuse or as reference material when designing a "
        "trial."
    ),
    "get_knowledge": (
        "Read the knowledge base. Without term: the term index, the perceptual "
        "dimensions and the insights document (confirmed and proposed findings). "
        "With term: that term's document (aliases are normalized) or null if it "
        "is unknown. Consult it before designing a trial for a described feel."
    ),
    "submit_trial": (
        "Submit one trial (a hapbeat-trial@1 object, see get_guide) with one or "
        "more candidate waveforms. Studio validates it, renders every candidate "
        "and stores it; the result has the trialId and per-candidate features "
        "(or a per-candidate error). A validation failure returns the parser's "
        "error text — fix the trial and resubmit. A trial id that already exists "
        "is an error. After submitting, tell the human it is ready and call "
        "wait_for_rating."
    ),
    "get_trial": (
        "Return one trial: the submitted request, the rendered candidates and "
        "the human's rating (null while unrated)."
    ),
    "list_trials": (
        "List recent trials, newest first (limit 1-100, default 20; "
        "unratedOnly=true to show only trials still waiting for the human)."
    ),
    "audition": (
        "Open the AI-trials tab in Studio's editor with the given trial and "
        "candidate selected so the human can try it. play=true also starts "
        "playback on the human's selected haptic targets (PC audio follows "
        "Studio's own mute setting); leave it false unless the human asked you "
        "to play it."
    ),
    "adopt": (
        "Adopt a candidate into the folder as a normal clip (same as the human "
        "pressing Adopt). Only do this when the human asked for it. Returns the "
        "new clipId and name."
    ),
    "propose_insight": (
        "Propose a finding for the knowledge base, backed by rated evidence "
        "(1-20 entries, each 'trialId/candidateId'; statement up to 1000 "
        "characters). It is appended to the Proposed section of insights.md for "
        "the human to confirm; you cannot write confirmed insights."
    ),
    "wait_for_rating": (
        "Wait until the human rates the trial (polls every 2 s). Returns "
        "{status: 'rated', rating} as soon as a rating exists, or "
        "{status: 'waiting'} when timeoutSec (10-1800, default 300) runs out — "
        "in that case simply call it again to keep waiting. Rating is done by "
        "the human in Studio; do not ask them to paste it."
    ),
}


def build_mcp_server(tools: HapbeatAgentTools) -> Any:
    """Create the MCP server object with every tool registered.

    Supports the ``mcp`` SDK 2.x (``MCPServer``) and 1.x (``FastMCP``), which
    share the decorator API used here.
    """
    from typing import Annotated

    from pydantic import Field

    try:
        from mcp.server.mcpserver import MCPServer as _Server
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as _Server
        from mcp.server.fastmcp.exceptions import ToolError

    server = _Server(name="hapbeat", instructions=SERVER_INSTRUCTIONS)

    async def guard(call: Awaitable[Any]) -> Any:
        try:
            return await call
        except AgentError as exc:
            raise ToolError(str(exc)) from None

    TrialId = Annotated[str, Field(description="Trial id (as returned by submit_trial)")]
    CandidateId = Annotated[str, Field(description="Candidate id within the trial")]

    @server.tool(name="status", description=_DESCRIPTIONS["status"])
    async def status() -> dict[str, Any]:
        return await guard(tools.status())

    @server.tool(name="get_guide", description=_DESCRIPTIONS["get_guide"])
    async def get_guide() -> dict[str, Any]:
        return await guard(tools.get_guide())

    @server.tool(name="get_catalog", description=_DESCRIPTIONS["get_catalog"])
    async def get_catalog() -> dict[str, Any]:
        return await guard(tools.get_catalog())

    @server.tool(name="get_knowledge", description=_DESCRIPTIONS["get_knowledge"])
    async def get_knowledge(
        term: Annotated[
            Optional[str], Field(description="Term to look up; omit for the index"),
        ] = None,
    ) -> dict[str, Any]:
        return await guard(tools.get_knowledge(term))

    @server.tool(name="submit_trial", description=_DESCRIPTIONS["submit_trial"])
    async def submit_trial(
        trial: Annotated[
            dict[str, Any],
            Field(description="hapbeat-trial@1 object; see get_guide for the format"),
        ],
    ) -> dict[str, Any]:
        return await guard(tools.submit_trial(trial))

    @server.tool(name="get_trial", description=_DESCRIPTIONS["get_trial"])
    async def get_trial(trialId: TrialId) -> dict[str, Any]:  # noqa: N803
        return await guard(tools.get_trial(trialId))

    @server.tool(name="list_trials", description=_DESCRIPTIONS["list_trials"])
    async def list_trials(
        limit: Annotated[
            Optional[int], Field(ge=1, le=100, description="Max trials (default 20)"),
        ] = None,
        unratedOnly: Annotated[  # noqa: N803
            Optional[bool], Field(description="Only trials without a rating"),
        ] = None,
    ) -> dict[str, Any]:
        return await guard(tools.list_trials(limit, unratedOnly))

    @server.tool(name="audition", description=_DESCRIPTIONS["audition"])
    async def audition(
        trialId: TrialId,  # noqa: N803
        candidateId: CandidateId,  # noqa: N803
        play: Annotated[
            Optional[bool], Field(description="Also start playback (default false)"),
        ] = None,
    ) -> dict[str, Any]:
        return await guard(tools.audition(trialId, candidateId, play))

    @server.tool(name="adopt", description=_DESCRIPTIONS["adopt"])
    async def adopt(trialId: TrialId, candidateId: CandidateId) -> dict[str, Any]:  # noqa: N803
        return await guard(tools.adopt(trialId, candidateId))

    @server.tool(name="propose_insight", description=_DESCRIPTIONS["propose_insight"])
    async def propose_insight(
        statement: Annotated[str, Field(max_length=1000, description="The finding")],
        evidence: Annotated[
            list[str],
            Field(min_length=1, max_length=20, description="'trialId/candidateId' entries"),
        ],
    ) -> dict[str, Any]:
        return await guard(tools.propose_insight(statement, evidence))

    @server.tool(name="wait_for_rating", description=_DESCRIPTIONS["wait_for_rating"])
    async def wait_for_rating(
        trialId: TrialId,  # noqa: N803
        timeoutSec: Annotated[  # noqa: N803
            int,
            Field(
                ge=WAIT_FOR_RATING_MIN_S, le=WAIT_FOR_RATING_MAX_S,
                description="Seconds to wait before returning 'waiting'",
            ),
        ] = WAIT_FOR_RATING_DEFAULT_S,
    ) -> dict[str, Any]:
        return await guard(tools.wait_for_rating(trialId, timeoutSec))

    return server


def run(port: int) -> int:
    """Serve MCP over stdio until the client closes it."""
    link = HelperLink(port)
    server = build_mcp_server(HapbeatAgentTools(link.request))
    server.run()  # stdio transport
    return 0
