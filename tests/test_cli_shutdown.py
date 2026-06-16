"""Ctrl+C teardown must be time-BOUNDED — the daemon must never wedge on exit.

A cancelled task parked on a blocked run_in_executor thread (or zeroconf's
blocking unregister / websockets wait_closed) used to hang `_shutdown_loop`
forever, made worse by ignoring SIGINT (a second Ctrl+C couldn't break out).
This guards that `_shutdown_loop` returns within a few seconds regardless.
"""
import asyncio
import time
from unittest import mock

import hapbeat_helper.cli as cli


def test_shutdown_loop_is_bounded_even_if_a_task_hangs_on_cancel():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def hangs_on_cancel():
            try:
                await asyncio.Future()  # run "forever"
            except asyncio.CancelledError:
                await asyncio.sleep(30)  # cleanup that won't finish (the wedge)
                raise

        task = loop.create_task(hangs_on_cancel())
        loop.run_until_complete(asyncio.sleep(0.05))  # let it start

        # Patch os._exit so the watchdog backstop can't kill the test process.
        with mock.patch.object(cli.os, "_exit"):
            t0 = time.monotonic()
            cli._shutdown_loop(loop)
            elapsed = time.monotonic() - t0

        assert elapsed < 4.5, f"_shutdown_loop took {elapsed:.1f}s — not bounded"
        del task  # avoid keeping the hung coroutine referenced
    finally:
        asyncio.set_event_loop(None)
