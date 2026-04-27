"""WebSocket server bridging Hapbeat Studio (web) to local hardware.

Runs on ``ws://localhost:7703`` and accepts JSON messages of the form
``{"type": "...", "payload": {...}}``. The protocol is the same one
``hapbeat-manager`` exposed; see the project README for the full table.

Targets
-------
Manager used to own the device-selection state and pick targets itself
("send to all selected devices"). Studio is now the single source of
truth for the active selection and includes ``targets: [ip, ...]`` (or a
single ``ip`` / ``target``) in every device-bound message. Helper just
relays — it never picks a device on the user's behalf.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import shutil
import socket
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

from hapbeat_helper import protocol
from hapbeat_helper.device_registry import DeviceRegistry, HapbeatDevice
from hapbeat_helper.mdns_scanner import MdnsScanner
from hapbeat_helper.pack_normalize import normalize_pack
from hapbeat_helper.tcp_client import TcpRawConnection
from hapbeat_helper.udp_listener import UdpListener

logger = logging.getLogger(__name__)

WS_PORT = 7703
HOST = "localhost"


def _device_to_dict(ip: str, dev: HapbeatDevice) -> dict:
    return {
        "name": dev.name,
        "ipAddress": ip,
        "address": dev.address,
        "firmwareVersion": dev.firmware,
        "online": dev.is_online,
        "serialConnected": False,  # Helper does not own serial
        "volumeLevel": dev.volume_level,
        "volumeWiper": dev.volume_wiper,
        "volumeSteps": dev.volume_steps,
    }


def _resolve_targets(payload: dict, registry: DeviceRegistry) -> list[str]:
    """Pick the destination IPs for a message.

    Preference order:
      1. ``payload['targets']`` — explicit list from Studio
      2. ``payload['ip']`` / ``payload['target']`` — single IP
      3. all known devices (broadcast-fallback)
    """
    raw_list = payload.get("targets")
    if isinstance(raw_list, list) and raw_list:
        return [str(ip) for ip in raw_list if ip]
    one = payload.get("ip") or payload.get("target")
    if one:
        return [str(one)]
    return registry.get_all_ips()


class HelperServer:
    """The WebSocket server + the subsystems it relays to.

    Owns:
      - :class:`UdpListener` (UDP 7700)
      - :class:`MdnsScanner` (zeroconf)
      - :class:`DeviceRegistry`

    Lifecycle: ``await server.run()`` blocks until cancelled.
    """

    def __init__(self, port: int = WS_PORT, host: str = HOST) -> None:
        self.port = port
        self.host = host
        self.registry = DeviceRegistry()
        self.udp = UdpListener()
        self.mdns = MdnsScanner()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._clients: set[Any] = set()
        self._stream_seq = 0
        self._scan_task: Optional[asyncio.Task] = None
        # ip -> background log-tail thread state. One subscriber per
        # device is enough for the MVP; if a second client subscribes
        # for the same IP we just rely on the first thread fanning out.
        self._log_threads: dict[str, threading.Thread] = {}
        self._log_stop_flags: dict[str, threading.Event] = {}

    # ── Run / shutdown ───────────────────────────────────────

    async def run(self) -> None:
        import websockets

        self._loop = asyncio.get_running_loop()

        if not self.udp.start():
            raise RuntimeError(
                "UDP port 7700 is unavailable. "
                "Is hapbeat-manager already running?"
            )

        # Wire subsystems into the registry / WS broadcast pipeline
        self.udp.add_pong_listener(self._on_pong)
        self.mdns.add_found_listener(self._on_mdns_found)
        self.mdns.add_removed_listener(self._on_mdns_removed)
        self.registry.add_change_listener(self._on_registry_change)

        self.mdns.start()

        # Periodic broadcast PING — keeps device list fresh and finds
        # devices that lack mDNS (e.g. SoftAP).
        self._scan_task = asyncio.create_task(self._scan_loop())

        async with websockets.serve(
            self._handler, self.host, self.port,
            # Default is 1 MB; firmware images are several MB. Bump
            # generously so OTA can be sent in a single message.
            max_size=64 * 1024 * 1024,
        ) as server:
            logger.info(
                "Hapbeat Helper listening on ws://%s:%d",
                self.host, self.port,
            )
            try:
                await asyncio.Future()  # run forever
            except asyncio.CancelledError:
                pass
            finally:
                if self._scan_task is not None:
                    self._scan_task.cancel()
                self.mdns.stop()
                self.udp.stop()
                server.close()

    async def _scan_loop(self) -> None:
        try:
            while True:
                self.udp.send_broadcast_ping()
                await asyncio.sleep(5.0)
        except asyncio.CancelledError:
            pass

    # ── Subsystem callbacks (run on background threads) ──────

    def _on_pong(self, pong: dict, ip: str) -> None:
        info = {
            "ip": ip,
            "name": pong.get("device_name", ""),
            "address": pong.get("address", ""),
            "firmware": pong.get("firmware_version", ""),
            "discovered_via": "udp_broadcast",
        }
        for key in ("volume_level", "volume_wiper", "volume_steps"):
            if key in pong:
                info[key] = pong[key]
        self.registry.upsert_device(info)

        # If volume info is present, also push a volume_changed event so
        # Studio can react instantly to physical knob movement.
        if "volume_level" in pong or "volume_wiper" in pong:
            self._post_to_loop(self._broadcast({
                "type": "volume_changed",
                "payload": {
                    "ip": ip,
                    "level": pong.get("volume_level"),
                    "wiper": pong.get("volume_wiper"),
                    "steps": pong.get("volume_steps"),
                },
            }))

    def _on_mdns_found(self, info: dict) -> None:
        self.registry.upsert_device(info)

    def _on_mdns_removed(self, ip: str) -> None:
        # Don't yank the device card on a single mDNS dropout — let the
        # liveness threshold handle it. Just refresh.
        self._post_to_loop(self._broadcast(self._device_list_msg()))

    def _on_registry_change(self) -> None:
        self._post_to_loop(self._broadcast(self._device_list_msg()))

    # ── WS handlers ──────────────────────────────────────────

    async def _handler(self, ws) -> None:
        self._clients.add(ws)
        remote = getattr(ws, "remote_address", None)
        logger.info("Studio connected: %s", remote)
        try:
            await ws.send(json.dumps(self._device_list_msg()))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    await ws.send(json.dumps({
                        "type": "error",
                        "payload": {"message": "invalid JSON"},
                    }))
                    continue
                try:
                    await self._dispatch(ws, msg)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("dispatch error")
                    await ws.send(json.dumps({
                        "type": "error",
                        "payload": {"message": str(exc)},
                    }))
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._clients.discard(ws)
            logger.info("Studio disconnected: %s", remote)

    async def _dispatch(self, ws, msg: dict) -> None:
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})

        if msg_type == "ping":
            await ws.send(json.dumps({"type": "pong", "payload": {}}))

        elif msg_type == "list_devices":
            await ws.send(json.dumps(self._device_list_msg()))

        elif msg_type == "preview_event":
            await self._handle_preview_event(ws, payload)

        elif msg_type == "stop_event":
            await self._handle_stop_event(ws, payload)

        elif msg_type == "write_ui_config":
            await self._handle_tcp_command(
                ws, payload, {"cmd": "write_ui_config",
                              "config": payload.get("config")},
            )

        elif msg_type == "set_wifi":
            await self._handle_tcp_command(
                ws, payload,
                {"cmd": "set_wifi",
                 "ssid": payload.get("ssid", ""),
                 "pass": payload.get("password", payload.get("pass", ""))},
            )

        elif msg_type == "set_name":
            await self._handle_tcp_command(
                ws, payload,
                {"cmd": "set_name", "name": payload.get("name", "")},
            )

        elif msg_type == "set_address":
            await self._handle_tcp_command(
                ws, payload,
                {"cmd": "set_address",
                 "address": payload.get("address", "")},
            )

        elif msg_type == "set_group":
            await self._handle_tcp_command(
                ws, payload,
                {"cmd": "set_group",
                 "group": int(payload.get("group", 0))},
            )

        elif msg_type == "reboot":
            await self._handle_tcp_command(
                ws, payload, {"cmd": "reboot"},
            )

        elif msg_type == "clear_wifi":
            await self._handle_tcp_command(
                ws, payload, {"cmd": "clear_wifi"},
            )

        elif msg_type == "get_info":
            await self._handle_query(
                ws, payload, "get_info", "get_info_result",
                lambda r: {
                    "name": r.get("name"),
                    "mac": r.get("mac"),
                    "fw": r.get("fw"),
                    "group": r.get("group"),
                    "wifi_connected": r.get("wifi_connected"),
                },
            )

        elif msg_type == "get_wifi_status":
            await self._handle_query(
                ws, payload, "get_wifi_status", "wifi_status_result",
                lambda r: {
                    "connected": r.get("connected"),
                    "ssid": r.get("ssid"),
                    "ip": r.get("ip"),
                    "rssi": r.get("rssi"),
                    "channel": r.get("channel"),
                },
            )

        elif msg_type == "list_wifi_profiles":
            await self._handle_query(
                ws, payload, "list_wifi_profiles", "wifi_profiles_result",
                lambda r: {
                    "profiles": r.get("profiles", []),
                    "count": r.get("count", len(r.get("profiles", []))),
                    "max": r.get("max", 5),
                },
            )

        elif msg_type == "connect_wifi_profile":
            await self._handle_tcp_command(
                ws, payload,
                {"cmd": "connect_wifi_profile",
                 "index": int(payload.get("index", 0))},
            )

        elif msg_type == "remove_wifi_profile":
            await self._handle_tcp_command(
                ws, payload,
                {"cmd": "remove_wifi_profile",
                 "index": int(payload.get("index", 0))},
            )

        elif msg_type == "get_debug_dump":
            # The debug dump response is a fat dict — pass it through
            # verbatim so the UI can render every field without the
            # helper having to know the schema.
            await self._handle_passthrough_query(
                ws, payload, {"cmd": "get_debug_dump"},
                "debug_dump_result",
            )

        elif msg_type == "kit_list":
            await self._handle_passthrough_query(
                ws, payload, {"cmd": "kit_list"}, "kit_list_result",
            )

        elif msg_type == "kit_delete":
            await self._handle_tcp_command(
                ws, payload,
                {"cmd": "kit_delete",
                 "kit_id": payload.get("kit_id", "")},
            )

        elif msg_type == "play_event":
            # Alias of preview_event, kept for parity with manager test page.
            await self._handle_preview_event(ws, payload)

        elif msg_type == "ping_device":
            await self._handle_ping_device(ws, payload)

        elif msg_type == "subscribe_logs":
            await self._handle_subscribe_logs(ws, payload)

        elif msg_type == "unsubscribe_logs":
            await self._handle_unsubscribe_logs(ws, payload)

        elif msg_type == "ota_data":
            await self._handle_ota_data(ws, payload)

        elif msg_type == "query_space":
            await self._handle_query(
                ws, payload, "space_query", "space_result",
                lambda r: {
                    "total_bytes": r.get("total", 0),
                    "used_bytes": r.get("used", 0),
                    "free_bytes": r.get("free", 0),
                },
            )

        elif msg_type == "query_volume":
            await self._handle_volume_query(ws, payload)

        elif msg_type == "deploy_kit_data":
            await self._handle_deploy_kit_data(ws, payload)

        elif msg_type == "stream_begin":
            await self._handle_stream_begin(ws, payload)

        elif msg_type == "stream_data":
            await self._handle_stream_data(ws, payload)

        elif msg_type == "stream_end":
            await self._handle_stream_end(ws, payload)

        else:
            await ws.send(json.dumps({
                "type": "error",
                "payload": {"message": f"unknown type: {msg_type}"},
            }))

    # ── Message handlers ─────────────────────────────────────

    async def _handle_preview_event(self, ws, payload: dict) -> None:
        event_id = payload.get("event_id", "")
        target = payload.get("target", "")
        gain = float(payload.get("gain", 1.0))
        if not event_id:
            await ws.send(json.dumps({
                "type": "error",
                "payload": {"message": "preview_event: event_id required"},
            }))
            return

        seq = int(time.monotonic_ns() // 1000) & 0xFFFF
        pkt = protocol.build_play(seq, event_id, target=target, gain=gain)
        # PLAY is sent as broadcast — devices self-filter by address.
        self.udp.send_raw(pkt, "<broadcast>")
        await ws.send(json.dumps({
            "type": "write_result",
            "payload": {"success": True, "message": "play sent"},
        }))

    async def _handle_stop_event(self, ws, payload: dict) -> None:
        event_id = payload.get("event_id", "")
        target = payload.get("target", "")
        seq = int(time.monotonic_ns() // 1000) & 0xFFFF
        if event_id:
            pkt = protocol.build_stop(seq, event_id, target=target)
        else:
            pkt = protocol.build_stop_all(seq, target=target)
        self.udp.send_raw(pkt, "<broadcast>")
        await ws.send(json.dumps({
            "type": "write_result",
            "payload": {"success": True, "message": "stop sent"},
        }))

    async def _handle_tcp_command(
        self, ws, payload: dict, cmd: dict,
    ) -> None:
        targets = _resolve_targets(payload, self.registry)
        if not targets:
            await ws.send(json.dumps({
                "type": "write_result",
                "payload": {
                    "success": False,
                    "error": "no_device",
                    "message": "no targets",
                },
            }))
            return

        # Run TCP I/O off the asyncio loop.
        loop = asyncio.get_running_loop()
        results = await loop.run_in_executor(
            None, _send_tcp_to_many, targets, cmd,
        )
        ok_count = sum(1 for r in results if r.get("success"))
        await ws.send(json.dumps({
            "type": "write_result",
            "payload": {
                "success": ok_count > 0,
                "device_confirmed": True,
                "message": f"{ok_count}/{len(targets)} ok",
                "results": results,
            },
        }))

    async def _handle_query(
        self, ws, payload: dict, cmd_name: str, response_type: str,
        extract_fields,
    ) -> None:
        targets = _resolve_targets(payload, self.registry)
        target = targets[0] if targets else ""
        if not target:
            await ws.send(json.dumps({
                "type": response_type,
                "payload": {"device": "", "error": "no_target"},
            }))
            return
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, _send_tcp_query, target, cmd_name,
        )
        if result and result.get("status") == "ok":
            payload_out = {"device": target, **extract_fields(result)}
            await ws.send(json.dumps({
                "type": response_type,
                "payload": payload_out,
            }))
        else:
            await ws.send(json.dumps({
                "type": response_type,
                "payload": {"device": target, "error": "query failed"},
            }))

    async def _handle_passthrough_query(
        self, ws, payload: dict, cmd: dict, response_type: str,
    ) -> None:
        """TCP-send *cmd* and forward the entire response dict back.

        Used for richly-typed device responses (kit_list, debug dump)
        where the helper has no business filtering fields.
        """
        targets = _resolve_targets(payload, self.registry)
        target = targets[0] if targets else ""
        if not target:
            await ws.send(json.dumps({
                "type": response_type,
                "payload": {"device": "", "error": "no_target"},
            }))
            return
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, _send_tcp_passthrough, target, cmd,
        )
        await ws.send(json.dumps({
            "type": response_type,
            "payload": {"device": target, **(result or {"error": "no response"})},
        }))

    async def _handle_ping_device(self, ws, payload: dict) -> None:
        """Send a UDP PING and await one PONG via the listener.

        Latency is measured via `UdpListener.send_ping` + the existing
        seq-correlation in the recv loop.
        """
        targets = _resolve_targets(payload, self.registry)
        target = targets[0] if targets else ""
        if not target:
            await ws.send(json.dumps({
                "type": "ping_result",
                "payload": {"device": "", "error": "no_target"},
            }))
            return

        loop = asyncio.get_running_loop()
        rtt_future: asyncio.Future[float] = loop.create_future()

        def on_rtt(ip: str, rtt_ms: float) -> None:
            if ip != target or rtt_future.done():
                return
            loop.call_soon_threadsafe(
                lambda: rtt_future.done() or rtt_future.set_result(rtt_ms),
            )

        # One-shot listener; removed after this ping resolves.
        self.udp.add_rtt_listener(on_rtt)
        try:
            self.udp.send_ping(target)
            try:
                rtt = await asyncio.wait_for(rtt_future, timeout=2.0)
                await ws.send(json.dumps({
                    "type": "ping_result",
                    "payload": {"device": target, "rtt_ms": round(rtt, 2)},
                }))
            except asyncio.TimeoutError:
                await ws.send(json.dumps({
                    "type": "ping_result",
                    "payload": {"device": target, "error": "timeout"},
                }))
        finally:
            # No remove API on UdpListener; clear by resetting the list.
            try:
                self.udp._rtt_callbacks.remove(on_rtt)
            except (ValueError, AttributeError):
                pass

    async def _handle_ota_data(self, ws, payload: dict) -> None:
        """Receive a base64-encoded firmware image and stream it to one device.

        Progress is broadcast as `ota_progress` events while the worker
        runs. Final outcome arrives as `ota_result`.
        """
        targets = _resolve_targets(payload, self.registry)
        target = targets[0] if targets else ""
        bin_b64 = payload.get("bin_base64", "")
        if not target or not bin_b64:
            await ws.send(json.dumps({
                "type": "ota_result",
                "payload": {
                    "device": target,
                    "success": False,
                    "message": "missing target or bin_base64",
                },
            }))
            return

        try:
            bin_bytes = base64.b64decode(bin_b64)
        except (ValueError, TypeError) as exc:
            await ws.send(json.dumps({
                "type": "ota_result",
                "payload": {
                    "device": target,
                    "success": False,
                    "message": f"bad base64: {exc}",
                },
            }))
            return

        await ws.send(json.dumps({
            "type": "ota_progress",
            "payload": {
                "device": target,
                "phase": "begin",
                "percent": 0,
                "message": f"OTA 開始 ({len(bin_bytes):,} bytes)",
            },
        }))

        loop = asyncio.get_running_loop()

        def progress(phase: str, percent: int, message: str) -> None:
            asyncio.run_coroutine_threadsafe(
                self._broadcast({
                    "type": "ota_progress",
                    "payload": {
                        "device": target, "phase": phase,
                        "percent": percent, "message": message,
                    },
                }),
                loop,
            )

        ok, msg = await loop.run_in_executor(
            None, _do_ota_to_device, target, bin_bytes, progress,
        )
        await self._broadcast({
            "type": "ota_result",
            "payload": {"device": target, "success": ok, "message": msg},
        })

    async def _handle_subscribe_logs(self, ws, payload: dict) -> None:
        """Open a long-lived TCP connection and tail firmware logs.

        Each `{type:"log","msg":...}` line the device sends is wrapped
        in a `device_log` ws message and broadcast to every connected
        client (other Studio tabs, etc.).
        """
        targets = _resolve_targets(payload, self.registry)
        target = targets[0] if targets else ""
        if not target:
            await ws.send(json.dumps({
                "type": "log_subscription",
                "payload": {"device": "", "ok": False, "error": "no_target"},
            }))
            return

        if target in self._log_threads:
            await ws.send(json.dumps({
                "type": "log_subscription",
                "payload": {"device": target, "ok": True, "already": True},
            }))
            return

        stop = threading.Event()
        self._log_stop_flags[target] = stop
        loop = self._loop

        def relay(line: str) -> None:
            if loop is None:
                return
            asyncio.run_coroutine_threadsafe(
                self._broadcast({
                    "type": "device_log",
                    "payload": {"device": target, "msg": line},
                }),
                loop,
            )

        t = threading.Thread(
            target=_log_tail_worker,
            args=(target, stop, relay),
            daemon=True,
            name=f"log-tail-{target}",
        )
        self._log_threads[target] = t
        t.start()
        await ws.send(json.dumps({
            "type": "log_subscription",
            "payload": {"device": target, "ok": True},
        }))

    async def _handle_unsubscribe_logs(self, ws, payload: dict) -> None:
        targets = _resolve_targets(payload, self.registry)
        target = targets[0] if targets else ""
        stop = self._log_stop_flags.pop(target, None)
        self._log_threads.pop(target, None)
        if stop is not None:
            stop.set()
        await ws.send(json.dumps({
            "type": "log_subscription",
            "payload": {"device": target, "ok": True, "stopped": True},
        }))

    async def _handle_volume_query(self, ws, payload: dict) -> None:
        targets = _resolve_targets(payload, self.registry)
        target = targets[0] if targets else ""
        if not target:
            await ws.send(json.dumps({
                "type": "volume_result",
                "payload": {"device": "", "error": "no_target"},
            }))
            return
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, _send_tcp_query, target, "get_volume",
        )
        if result and result.get("status") == "ok":
            self.registry.update_volume(
                target,
                result.get("volume_level"),
                result.get("volume_wiper"),
                result.get("volume_steps"),
            )
            await ws.send(json.dumps({
                "type": "volume_result",
                "payload": {
                    "device": target,
                    "volume_level": result.get("volume_level"),
                    "volume_wiper": result.get("volume_wiper"),
                    "volume_steps": result.get("volume_steps"),
                },
            }))
        else:
            await ws.send(json.dumps({
                "type": "volume_result",
                "payload": {"device": target, "error": "query failed"},
            }))

    async def _handle_deploy_kit_data(self, ws, payload: dict) -> None:
        kit_id = payload.get("kit_id", "")
        zip_b64 = payload.get("zip_base64", "")
        targets = _resolve_targets(payload, self.registry)
        if not zip_b64 or not targets:
            await ws.send(json.dumps({
                "type": "deploy_result",
                "payload": {
                    "success": False,
                    "message": "missing zip_base64 or targets",
                },
            }))
            return

        loop = asyncio.get_running_loop()

        def _extract_and_normalize() -> Optional[Path]:
            try:
                zip_bytes = base64.b64decode(zip_b64)
                tmp_dir = tempfile.mkdtemp(prefix="hb_deploy_")
                with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                    zf.extractall(tmp_dir)
                pack_dir = Path(tmp_dir)
                # Pack may be nested one directory deep.
                subs = [p for p in pack_dir.iterdir() if p.is_dir()]
                if subs:
                    pack_dir = subs[0]
                normalize_pack(pack_dir)
                return pack_dir
            except Exception:  # noqa: BLE001
                logger.exception("deploy_kit_data: extract failed")
                return None

        pack_dir = await loop.run_in_executor(None, _extract_and_normalize)
        if pack_dir is None:
            await ws.send(json.dumps({
                "type": "deploy_result",
                "payload": {"success": False,
                            "message": "failed to extract zip"},
            }))
            return

        await ws.send(json.dumps({
            "type": "deploy_result",
            "payload": {"success": True, "message": "deploy started",
                        "kit_id": kit_id},
        }))

        # Send to each target sequentially in a worker thread so the
        # WebSocket handler stays responsive. Progress is pushed via
        # `deploy_progress` messages from a thread-safe callback.
        async def _run_deploy():
            for ip in targets:
                # The callback is invoked from a worker thread, so we
                # can't await on it directly — schedule the broadcast
                # on the asyncio loop with run_coroutine_threadsafe.
                def make_progress(ip_inner: str):
                    def cb(pct: int, msg: str) -> None:
                        coro = self._broadcast({
                            "type": "deploy_progress",
                            "payload": {
                                "ip": ip_inner,
                                "kit_id": kit_id,
                                "percent": pct,
                                "message": msg,
                            },
                        })
                        try:
                            asyncio.run_coroutine_threadsafe(coro, loop)
                        except RuntimeError:
                            pass
                    return cb

                ok, msg = await loop.run_in_executor(
                    None, _deploy_kit_to_device,
                    ip, pack_dir, kit_id, make_progress(ip),
                )
                await self._broadcast({
                    "type": "deploy_result",
                    "payload": {
                        "success": ok,
                        "ip": ip,
                        "kit_id": kit_id,
                        "message": msg,
                    },
                })
            shutil.rmtree(pack_dir.parent, ignore_errors=True)

        asyncio.create_task(_run_deploy())

    # ── Streaming ────────────────────────────────────────────

    async def _handle_stream_begin(self, ws, payload: dict) -> None:
        targets = _resolve_targets(payload, self.registry)
        if not targets:
            await ws.send(json.dumps({
                "type": "stream_ack",
                "payload": {"status": "no_target"},
            }))
            return
        self._stream_seq = 0
        pkt = protocol.build_stream_begin(
            seq=self._stream_seq,
            sample_rate=int(payload.get("sample_rate", 16000)),
            channels=int(payload.get("channels", 1)),
            fmt=1 if payload.get("format", "adpcm") == "adpcm" else 0,
            total_samples=int(payload.get("total_samples", 0)),
            gain=float(payload.get("gain", 1.0)),
        )
        for ip in targets:
            self.udp.send_raw(pkt, ip)
        await ws.send(json.dumps({
            "type": "stream_ack",
            "payload": {"status": "ok", "targets": targets},
        }))

    async def _handle_stream_data(self, ws, payload: dict) -> None:
        targets = _resolve_targets(payload, self.registry)
        if not targets:
            return
        offset = int(payload.get("offset", 0))
        data_b64 = payload.get("data", "")
        audio = base64.b64decode(data_b64) if data_b64 else b""
        self._stream_seq += 1
        pkt = protocol.build_stream_data(
            seq=self._stream_seq, offset=offset, data=audio,
        )
        for ip in targets:
            self.udp.send_raw(pkt, ip)

    async def _handle_stream_end(self, ws, payload: dict) -> None:
        targets = _resolve_targets(payload, self.registry)
        if not targets:
            return
        self._stream_seq += 1
        pkt = protocol.build_stream_end(seq=self._stream_seq)
        for ip in targets:
            self.udp.send_raw(pkt, ip)

    # ── Helpers ──────────────────────────────────────────────

    def _device_list_msg(self) -> dict:
        devices = self.registry.get_all_devices()
        return {
            "type": "device_list",
            "payload": {
                "devices": [
                    _device_to_dict(ip, dev) for ip, dev in devices.items()
                ],
            },
        }

    async def _broadcast(self, msg: dict) -> None:
        import websockets
        text = json.dumps(msg) if isinstance(msg, dict) else msg
        for ws in list(self._clients):
            try:
                await ws.send(text)
            except websockets.exceptions.ConnectionClosed:
                self._clients.discard(ws)
            except Exception:  # noqa: BLE001
                self._clients.discard(ws)

    def _post_to_loop(self, coro) -> None:
        """Schedule *coro* on the asyncio loop from a background thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:
            pass


# ── Background-thread helpers (no asyncio) ───────────────────

def _send_tcp_to_many(targets: list[str], cmd: dict) -> list[dict]:
    """Send *cmd* to every IP in *targets* sequentially. Returns a list
    of ``{ip, success, response}`` results.
    """
    out: list[dict] = []
    for ip in targets:
        with TcpRawConnection(ip) as conn:
            if not conn.connect():
                out.append({"ip": ip, "success": False,
                            "response": {"error": "connect failed"}})
                continue
            try:
                conn.send_json(cmd)
                resp = conn.read_response(timeout=10.0) or {}
            except OSError as exc:
                out.append({"ip": ip, "success": False,
                            "response": {"error": str(exc)}})
                continue
            out.append({
                "ip": ip,
                "success": resp.get("status") == "ok",
                "response": resp,
            })
    return out


def _send_tcp_query(ip: str, cmd_name: str) -> Optional[dict]:
    with TcpRawConnection(ip) as conn:
        if not conn.connect():
            return None
        conn.send_json({"cmd": cmd_name})
        return conn.read_response(timeout=5.0)


def _do_ota_to_device(
    ip: str, bin_bytes: bytes, progress,
) -> tuple[bool, str]:
    """Push a firmware image to one device over TCP 7701.

    Mirrors `transport._do_ota_single` in the manager. Calls `progress(
    phase, percent, message)` as the worker advances.
    """
    file_size = len(bin_bytes)
    with TcpRawConnection(ip) as conn:
        if not conn.connect():
            return False, "connect failed"
        try:
            conn.send_json({"cmd": "ota_begin", "size": file_size})
            resp = conn.read_response(timeout=5.0)
            if not resp or resp.get("status") != "ok":
                return False, f"ota_begin nack: {resp}"

            chunk_size = 4096
            sent = 0
            for off in range(0, file_size, chunk_size):
                chunk = bin_bytes[off : off + chunk_size]
                conn.send_raw(chunk)
                sent += len(chunk)
                pct = int(sent / file_size * 95) + 1
                progress("upload", pct, f"送信中 {sent:,}/{file_size:,}")

            progress("flash", 96, "デバイス書込待ち…")
            while True:
                resp = conn.read_response(timeout=30.0)
                if resp is None:
                    return False, "device timeout"
                status = resp.get("status", "")
                if status == "ok":
                    progress("done", 100, "OTA 完了")
                    return True, resp.get("message", "OK")
                if status == "error":
                    return False, resp.get("message", "OTA error")
                if status == "progress":
                    pct = resp.get("percent", 0)
                    progress("flash", min(96 + pct // 25, 99),
                             f"書込中 {pct}%")
        except OSError as exc:
            return False, f"io error: {exc}"


def _log_tail_worker(ip: str, stop: threading.Event, relay) -> None:
    """Hold a TCP 7701 connection and forward firmware log lines.

    The firmware exposes log streaming as `{cmd: log_stream, enable: true}`;
    each subsequent `{type: "log", msg: "..."}` line is a log entry.
    """
    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3.0)
        sock.connect((ip, 7701))
        sock.settimeout(0.5)
        # Handshake (firmware always wants get_info first)
        sock.sendall(json.dumps({"cmd": "get_info"}).encode() + b"\n")
        # Best-effort: drain that response so it doesn't show up as a log line.
        _drain_one_line(sock)
        sock.sendall(json.dumps({"cmd": "log_stream", "enable": True}).encode() + b"\n")

        buf = b""
        while not stop.is_set():
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "log":
                    relay(str(obj.get("msg", "")))
    except OSError as exc:
        logger.warning("log tail (%s): %s", ip, exc)
    finally:
        if sock is not None:
            try:
                sock.sendall(
                    json.dumps({"cmd": "log_stream", "enable": False}).encode()
                    + b"\n"
                )
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass


def _drain_one_line(sock: socket.socket) -> None:
    """Best-effort: read & discard a single newline-terminated frame."""
    sock.settimeout(1.0)
    buf = b""
    try:
        while b"\n" not in buf and len(buf) < 8192:
            chunk = sock.recv(1024)
            if not chunk:
                return
            buf += chunk
    except (OSError, socket.timeout):
        pass
    finally:
        sock.settimeout(0.5)


def _send_tcp_passthrough(ip: str, cmd: dict) -> Optional[dict]:
    """Send `cmd` and return the full response dict (status field stripped)."""
    with TcpRawConnection(ip) as conn:
        if not conn.connect():
            return {"error": "connect failed"}
        conn.send_json(cmd)
        resp = conn.read_response(timeout=10.0)
        if resp is None:
            return {"error": "no response"}
        return resp


def _deploy_kit_to_device(
    ip: str, pack_dir: Path, kit_id_default: str,
    on_progress=None,
) -> tuple[bool, str]:
    """Send a Pack/Kit to one device using the firmware's TCP protocol.

    Mirrors ``transport._do_tcp_kit_transfer`` from the manager.

    Parameters
    ----------
    on_progress
        Optional callback ``(percent: int, message: str) -> None`` invoked
        after each file finishes uploading and at major lifecycle steps
        (install, commit). Caller is responsible for thread-safety; this
        function calls it synchronously from the worker thread.
    """
    manifest_path = pack_dir / "manifest.json"
    if not manifest_path.exists():
        return False, "manifest.json missing"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"manifest read failed: {exc}"
    kit_id = manifest.get("kit_id", kit_id_default or "unknown")

    files: list[tuple[str, Path]] = []
    for fp in sorted(pack_dir.rglob("*")):
        if fp.is_file():
            rel = fp.relative_to(pack_dir).as_posix()
            # stream-clips/ are SDK-side (Unity streams them at runtime).
            # The firmware does not store them; exclude to avoid wasting
            # LittleFS space and to prevent install-commit confusion.
            if rel.startswith("stream-clips/"):
                continue
            files.append((rel, fp))
    total_size = sum(fp.stat().st_size for _, fp in files)
    # Total TCP-payload chunks across the whole kit. Drives the
    # progress bar so the percent advances on every actual packet
    # send rather than only at file-boundaries.
    chunk_size = 4096
    total_chunks = sum(
        max(1, (fp.stat().st_size + chunk_size - 1) // chunk_size)
        for _, fp in files
    )

    def _progress(pct: int, msg: str) -> None:
        if on_progress is None:
            return
        try:
            on_progress(pct, msg)
        except Exception:  # noqa: BLE001
            logger.exception("deploy on_progress callback failed")

    _progress(0, f"connecting to {ip}…")

    with TcpRawConnection(ip) as conn:
        if not conn.connect():
            return False, "connect failed"
        try:
            conn.send_json({
                "cmd": "kit_install", "kit_id": kit_id,
                "file_count": len(files), "total_size": total_size,
            })
            resp = conn.read_response(timeout=10.0)
            if not resp or resp.get("status") != "ok":
                return False, f"install nack: {resp}"

            chunks_sent = 0
            for idx, (rel_path, abs_path) in enumerate(files, 1):
                size = abs_path.stat().st_size
                conn.send_json({
                    "cmd": "file_begin", "path": rel_path, "size": size,
                })
                resp = conn.read_response(timeout=5.0)
                if not resp or resp.get("status") != "ok":
                    return False, f"file_begin nack ({rel_path})"

                # Per-chunk progress: emit one update per TCP packet so
                # the UI bar moves at the actual transfer cadence. The
                # percent is `chunks_sent / total_chunks * 99` so the
                # final 1% is reserved for kit_commit.
                file_chunks = max(1, (size + chunk_size - 1) // chunk_size)
                file_chunk_idx = 0
                with open(abs_path, "rb") as f:
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        conn.send_raw(chunk)
                        chunks_sent += 1
                        file_chunk_idx += 1
                        pct = int(chunks_sent / total_chunks * 99) if total_chunks else 99
                        _progress(
                            pct,
                            f"[{idx}/{len(files)}] {rel_path} "
                            f"pkt {chunks_sent}/{total_chunks} "
                            f"({file_chunk_idx}/{file_chunks} in file)",
                        )

                resp = conn.read_response(timeout=10.0)
                if not resp or resp.get("status") != "ok":
                    return False, f"file recv nack ({rel_path})"

            _progress(99, "committing…")
            conn.send_json({"cmd": "kit_commit"})
            resp = conn.read_response(timeout=15.0)
            if not resp or resp.get("status") != "ok":
                return False, f"commit nack: {resp}"
        except OSError as exc:
            return False, f"io error: {exc}"

    _progress(100, "complete")
    return True, "ok"
