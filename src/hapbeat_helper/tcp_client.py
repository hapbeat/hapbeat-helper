"""Synchronous TCP client for Hapbeat devices (port 7701).

Helper edition: only the lightweight :class:`TcpRawConnection` from the
manager is kept — it has no reader thread and no Qt signals, so we can
call it cleanly from asyncio via ``run_in_executor``.

OTA is intentionally out of scope: Studio drives firmware writes through
Web Serial / esptool-js directly.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PORT = 7701
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 2.0


class TcpRawConnection:
    """Simple TCP connection for synchronous request/response use.

    ``connect()`` performs a handshake (``get_info``) to confirm the
    firmware accepted the connection, retrying briefly if the server is
    still cleaning up a previous client.
    """

    def __init__(self, ip: str, port: int = DEFAULT_PORT) -> None:
        self.ip = ip
        self.port = port
        self.sock: Optional[socket.socket] = None

    def connect(self, retries: int = 2) -> bool:
        for attempt in range(retries + 1):
            try:
                logger.info(
                    "TCP connect %s:%d (attempt %d/%d)",
                    self.ip, self.port, attempt + 1, retries + 1,
                )
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(CONNECT_TIMEOUT)
                s.connect((self.ip, self.port))
                s.settimeout(READ_TIMEOUT)
                self.sock = s

                self.send_json({"cmd": "get_info"})
                resp = self.read_response(timeout=3.0)
                if resp and resp.get("status") == "ok":
                    logger.info(
                        "TCP handshake ok (%s: %s)",
                        self.ip, resp.get("name", "?"),
                    )
                    return True

                logger.info("TCP handshake failed (%s): %s", self.ip, resp)
                self.close()
            except OSError as exc:
                logger.info("TCP connect error (%s): %s", self.ip, exc)
                self.close()

            if attempt < retries:
                time.sleep(1.0)

        logger.warning("TCP connect failed (%s:%d)", self.ip, self.port)
        return False

    def close(self) -> None:
        # Issue an explicit shutdown before close. The firmware's
        # tcp_server.cpp has a single global `s_client` slot and only
        # accepts a *new* connection when `s_client.connected()`
        # returns false (tcp_server.cpp:1233). Without an explicit
        # shutdown, the FIN may sit buffered locally for a while,
        # which leaves the device's `s_client` in the connected state
        # — so the next click finds the device "busy" and the new TCP
        # connect appears to hang/timeout. shutdown(SHUT_RDWR) flushes
        # the FIN immediately so the device can clean up between
        # back-to-back requests. (User-reported "詰まり" 2026-05-01.)
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                # Already closed or never connected — ignore.
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def send_json(self, cmd: dict) -> None:
        if not self.sock:
            raise OSError("not connected")
        line = json.dumps(cmd, separators=(",", ":")) + "\n"
        self.sock.sendall(line.encode("utf-8"))

    def send_raw(self, data: bytes) -> None:
        if not self.sock:
            raise OSError("not connected")
        self.sock.sendall(data)

    def read_response(self, timeout: float = 5.0) -> Optional[dict]:
        if not self.sock:
            return None
        return _read_json_response(self.sock, timeout)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _read_json_response(
    sock: socket.socket, timeout: float = 5.0,
) -> Optional[dict]:
    """Read a single JSON line from *sock* with timeout."""
    sock.settimeout(timeout)
    buf = b""
    try:
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf += chunk
    except (socket.timeout, OSError):
        return None
    finally:
        try:
            sock.settimeout(READ_TIMEOUT)
        except OSError:
            pass

    line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
    if not line.startswith("{"):
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None
