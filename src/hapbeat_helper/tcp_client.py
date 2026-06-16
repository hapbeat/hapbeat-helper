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
import struct
import time
from typing import Optional

logger = logging.getLogger(__name__)

# SO_LINGER struct: (l_onoff=1, l_linger=0) → close() sends RST instead
# of FIN. Why: ESP32 lwIP keeps the per-client socket fd allocated until
# the firmware loop() reaches its disconnect-cleanup branch, which is
# *not* reached during OTA (line 1586 short-circuits to processOtaData).
# Without RST, helper's close() drops to FIN_WAIT_2 waiting for the
# firmware's FIN, which only arrives much later (or never if loop is
# busy). Many such half-closed sockets pile up on the device's lwIP
# pool (LWIP_MAX_SOCKETS ≈ 10) and eventually starve OTA.
#
# RST is rude — any unread RX bytes on the device side are discarded.
# Acceptable for our request/response queries: helper always reads the
# response before close(). Acceptable for OTA cleanup on failure: we're
# tearing down anyway. NOT acceptable mid-OTA-success — but the success
# path closes only after both sides have drained their respective
# expected exchanges, so applying it uniformly is safe.
_SO_LINGER_RST = struct.pack("ii", 1, 0)

DEFAULT_PORT = 7701
# Aggressive timeouts (2026-05-02): Studio users were reporting
# "詰まり" — multi-second freezes when a click triggered a TCP path
# while a previous connection was still being torn down on the device.
# Old defaults (3 s connect + 3 s handshake + 2 retries × 1 s sleep)
# accumulated to ~20 s worst-case before the helper gave up; now the
# whole chain fits in ~6 s and the user sees a clear "device unresponsive"
# faster than they could finish manually power-cycling the device.
CONNECT_TIMEOUT = 1.5
READ_TIMEOUT = 1.5
# 2026-05-08: 「Helper 起動前に Hapbeat が Wi-Fi に居ると初回 deploy が
# 失敗する」報告。原因はファーム側の単一 s_client スロットが何かに
# 占有されており、kernel は accept しても application 層が新規接続を
# 拾えず、handshake が無応答になるパターン。
#
# Firmware は IDLE_TIMEOUT_MS=5s で stuck client を解放する
# (device-firmware/src/tcp_server.cpp 1455-) が、現在の retry は
# 0.3s × 1 で ~3s しか待てない → ファームの recovery を待ちきれない。
#
# Handshake 失敗 (= TCP 接続は成立したのに JSON 応答が来ない) の場合に
# 限り、IDLE_TIMEOUT を超える長めの sleep + 1 回の最終 retry を入れる。
# Connect 失敗 (network unreachable など) はファーム recovery で
# 直る性質ではないので、現行通り短い retry のまま。
RECOVERY_SLEEP = 6.0  # firmware IDLE_TIMEOUT_MS (5s) + 余裕


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

    def connect(self, retries: int = 1, recover: bool = True) -> bool:
        # `recover`: run the stuck-slot recovery path (RECOVERY_SLEEP + 1 retry)
        # on a handshake failure. Default True for writes (set_* / OTA / deploy)
        # where slot recovery matters. Pass False for HIGH-FREQUENCY READ POLLS
        # (get_info / get_sensor_reading at ~1 Hz): a buggy/slow device that
        # never replies would otherwise pin an executor thread for ~10 s
        # (1.5 connect + 1.5 handshake + 6 sleep + retry) EVERY poll, so demand
        # outruns the shared ThreadPoolExecutor and a backlog builds up that
        # delays even UDP play/stream sends (helper "slows down over time" until
        # restart). A missed read poll just retries on the next tick.
        # `handshake_failure_seen` は「TCP は通ったが get_info 応答無し」を
        # 1 回でも観測したかを保持。観測したら最後に IDLE_TIMEOUT を超える
        # sleep + 追加 1 回の retry を試す (stuck slot recovery)。
        handshake_failure_seen = False

        # Routine connect/handshake chatter is DEBUG — at the ~1 Hz config-poll
        # cadence (Studio mapping/MQTT tabs) it otherwise floods the log, and the
        # single-TCP-slot contention that occasionally trips the recovery path is
        # expected and self-healing. Only a genuine give-up is WARNING (below).
        for attempt in range(retries + 1):
            try:
                logger.debug(
                    "TCP connect %s:%d (attempt %d/%d)",
                    self.ip, self.port, attempt + 1, retries + 1,
                )
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(CONNECT_TIMEOUT)
                s.connect((self.ip, self.port))
                s.settimeout(READ_TIMEOUT)
                self.sock = s

                self.send_json({"cmd": "get_info"})
                resp = self.read_response(timeout=READ_TIMEOUT)
                if resp and resp.get("status") == "ok":
                    logger.debug(
                        "TCP handshake ok (%s: %s)",
                        self.ip, resp.get("name", "?"),
                    )
                    return True

                logger.debug("TCP handshake failed (%s): %s", self.ip, resp)
                handshake_failure_seen = True
                self.close()
            except OSError as exc:
                logger.debug("TCP connect error (%s): %s", self.ip, exc)
                self.close()

            # Short pause before retry — long enough that the firmware
            # has a chance to FIN-clean the previous slot, short enough
            # that the user doesn't perceive a freeze.
            if attempt < retries:
                time.sleep(0.3)

        # Stuck-slot recovery path: handshake が無応答だったら
        # firmware の idle timeout 経過を待って 1 回だけ再試行する。
        # 高頻度 read poll では recover=False で丸ごとスキップ (上記コメント)。
        if handshake_failure_seen and recover:
            logger.debug(
                "TCP recovery: waiting %.1fs for firmware idle-timeout "
                "to release stuck client (%s:%d)",
                RECOVERY_SLEEP, self.ip, self.port,
            )
            time.sleep(RECOVERY_SLEEP)
            try:
                logger.debug(
                    "TCP connect %s:%d (recovery attempt)",
                    self.ip, self.port,
                )
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(CONNECT_TIMEOUT)
                s.connect((self.ip, self.port))
                s.settimeout(READ_TIMEOUT)
                self.sock = s
                self.send_json({"cmd": "get_info"})
                resp = self.read_response(timeout=READ_TIMEOUT)
                if resp and resp.get("status") == "ok":
                    logger.debug(
                        "TCP handshake ok after recovery (%s: %s)",
                        self.ip, resp.get("name", "?"),
                    )
                    return True
                logger.debug(
                    "TCP recovery handshake also failed (%s): %s",
                    self.ip, resp,
                )
                self.close()
            except OSError as exc:
                logger.debug(
                    "TCP recovery connect error (%s): %s", self.ip, exc,
                )
                self.close()

        logger.warning("TCP connect failed (%s:%d)", self.ip, self.port)
        return False

    def close(self) -> None:
        # SO_LINGER(on=1, linger=0) → close() sends RST. This avoids
        # FIN_WAIT_2 piling up while the firmware's loop() takes its
        # time getting around to the disconnect-cleanup branch. See
        # _SO_LINGER_RST comment for full rationale. Failing to set
        # the option (e.g. socket already torn down) is harmless — fall
        # back to plain shutdown/close.
        if self.sock:
            try:
                self.sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, _SO_LINGER_RST,
                )
            except OSError:
                # Some platforms / states reject this — drop to FIN path.
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
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

    def send_raw(self, data: bytes, *, timeout: float | None = None) -> None:
        if not self.sock:
            raise OSError("not connected")
        if timeout is not None:
            prev = self.sock.gettimeout()
            self.sock.settimeout(timeout)
            try:
                self.sock.sendall(data)
            finally:
                self.sock.settimeout(prev)
        else:
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
