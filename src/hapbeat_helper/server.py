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
import select
import shutil
import socket
import struct
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
        # Node role/transport from the mDNS TXT records (DEC-034) —
        # None when the firmware predates them (Studio defaults to
        # receiver/udp).
        "role": dev.role or None,
        "transport": dev.transport or None,
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
        # IPs for which an OTA is currently in flight. log_tail supervisor
        # uses this to suppress reconnect attempts during OTA.
        self._ota_in_progress: set[str] = set()

        # ip -> asyncio.Lock for serializing TCP traffic to that device.
        #
        # Why: firmware tcp_server.cpp uses a single `s_client` slot; when
        # a second client connects, the first is idle-displaced mid-query.
        # Studio's DeviceDetail fires 5+ queries (get_info / get_wifi_status
        # / get_ap_status / get_oled_brightness / list_wifi_profiles) the
        # moment a device is selected, and an OTA / write_ui_config / kit
        # deploy on top of that means several connections racing for the
        # same s_client slot. The losers come back as silent "displaced"
        # errors and the user sees "OTA never progressed" or random
        # query timeouts on a freshly-booted device.
        #
        # The lock makes every TCP-to-device operation queue per-IP. Two
        # devices can still be hit in parallel; only same-device traffic
        # serializes. (User report 2026-05-08: "hapbeat 起動 → helper 起動
        # で OTA が刺さる現象は再現性がある".)
        self._tcp_locks: dict[str, asyncio.Lock] = {}

    def _get_tcp_lock(self, ip: str) -> asyncio.Lock:
        lock = self._tcp_locks.get(ip)
        if lock is None:
            lock = asyncio.Lock()
            self._tcp_locks[ip] = lock
        return lock

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
        # 2s ping interval matches Manager. The registry's offline
        # threshold is 5s so a powered-off device shows up as offline
        # within ~5s in the Studio UI. We also re-broadcast the
        # device_list at the end of every scan tick so an online→offline
        # transition is pushed to clients without waiting for the next
        # registry mutation (which only fires on upserts).
        try:
            last_online_state: dict[str, bool] = {}
            while True:
                self.udp.send_broadcast_ping()
                # Also unicast-PING every known device. Wi-Fi APs deliver
                # broadcasts on the DTIM beacon at the lowest rate and many
                # power-saving stations drop them — that intermittent loss is
                # what made cards flap offline for a moment (user report
                # 2026-06-13). Unicast frames are buffered/retried by the AP,
                # so a sleepy ESP32 still gets them reliably.
                for ip in self.registry.get_all_ips():
                    try:
                        self.udp.send_ping(ip)
                    except OSError:
                        pass  # interface change mid-scan — next tick recovers
                await asyncio.sleep(2.0)
                # Detect liveness transitions and push if anything flipped.
                current = {
                    ip: d.is_online for ip, d in self.registry.get_all_devices().items()
                }
                if current != last_online_state:
                    last_online_state = current
                    self._post_to_loop(self._broadcast(self._device_list_msg()))
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
        from hapbeat_helper import __version__ as _helper_version
        self._clients.add(ws)
        remote = getattr(ws, "remote_address", None)
        logger.info("Studio connected: %s", remote)
        try:
            # Send helper hello (= version) FIRST so Studio can render it
            # before any device events arrive. Studio displays this in
            # the header next to the "Helper 接続中" indicator.
            await ws.send(json.dumps({
                "type": "helper_hello",
                "payload": {"version": _helper_version},
            }))
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
            # If this was the last WS client, tear down all log_tail
            # subscribers. Without this, a closed-tab / refreshed Studio
            # leaves _log_tail_worker threads running indefinitely; each
            # holds a TCP connection to the device with `s_log_stream=true`
            # latched on firmware. New deploy/OTA attempts then race
            # against a "stale but flagged" log slot — and the firmware's
            # displacement logic has `!s_log_stream` in its gate, so the
            # stale slot wins and refuses every subsequent SYN until power
            # cycle. (User report 2026-05-08: 「しばらく放置していると
            # TCP handshake failed が連発する」)
            if not self._clients and self._log_threads:
                logger.info(
                    "last WS client gone — stopping %d log_tail thread(s)",
                    len(self._log_threads),
                )
                # Snapshot before mutating
                ips_to_stop = list(self._log_threads.keys())
                for ip in ips_to_stop:
                    stop = self._log_stop_flags.pop(ip, None)
                    self._log_threads.pop(ip, None)
                    if stop:
                        stop.set()
            logger.info("Studio disconnected: %s", remote)

    async def _dispatch(self, ws, msg: dict) -> None:
        msg_type = msg.get("type", "")
        payload = msg.get("payload", {})

        if msg_type == "ping":
            await ws.send(json.dumps({"type": "pong", "payload": {}}))

        elif msg_type == "list_devices":
            await ws.send(json.dumps(self._device_list_msg()))

        elif msg_type == "rescan":
            # Trigger an immediate UDP broadcast PING + push the latest
            # device_list so the requester sees a fresh snapshot without
            # waiting for the next 2s scan tick.
            try:
                self.udp.send_broadcast_ping()
            except Exception as exc:  # noqa: BLE001
                logger.warning("rescan: broadcast_ping failed: %s", exc)
            # Give devices ~250ms to PONG, then broadcast device_list.
            await asyncio.sleep(0.25)
            await self._broadcast(self._device_list_msg())

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

        elif msg_type == "set_oled_brightness":
            await self._handle_tcp_command(
                ws, payload,
                {"cmd": "set_oled_brightness",
                 "level": int(payload.get("level", 2))},
            )

        elif msg_type == "get_oled_brightness":
            await self._handle_query(
                ws, payload, "get_oled_brightness", "oled_brightness_result",
                lambda r: {"level": r.get("level", 2)},
            )

        elif msg_type == "reboot":
            await self._handle_tcp_command(
                ws, payload, {"cmd": "reboot"},
            )

        elif msg_type == "clear_wifi":
            await self._handle_tcp_command(
                ws, payload, {"cmd": "clear_wifi"},
            )

        # ── SoftAP mode handlers (added 2026-05-08) ──
        # Studio (DeviceDetail) sends `get_ap_status` immediately on every
        # device selection, so an outdated helper without these stanzas
        # spammed `ERROR: unknown type: get_ap_status` toasts every time.
        # The firmware (tcp_server.cpp) already implements all five — we
        # just need to relay them.
        elif msg_type == "get_ap_status":
            await self._handle_query(
                ws, payload, "get_ap_status", "ap_status_result",
                lambda r: {
                    "mode": r.get("mode"),
                    "ap_ssid": r.get("ap_ssid"),
                    "ap_ip": r.get("ap_ip"),
                    "ap_has_pass": r.get("ap_has_pass"),
                    "ap_client_count": r.get("ap_client_count"),
                },
            )

        elif msg_type == "enter_ap_mode":
            await self._handle_tcp_command(
                ws, payload, {"cmd": "enter_ap_mode"},
            )

        elif msg_type == "enter_sta_mode":
            await self._handle_tcp_command(
                ws, payload, {"cmd": "enter_sta_mode"},
            )

        elif msg_type == "set_ap_pass":
            await self._handle_tcp_command(
                ws, payload,
                {"cmd": "set_ap_pass",
                 "pass": payload.get("password", payload.get("pass", ""))},
            )

        elif msg_type == "clear_ap_pass":
            await self._handle_tcp_command(
                ws, payload, {"cmd": "clear_ap_pass"},
            )

        elif msg_type == "get_info":
            await self._handle_query(
                ws, payload, "get_info", "get_info_result",
                lambda r: {
                    "name": r.get("name"),
                    "mac": r.get("mac"),
                    "fw": r.get("fw"),
                    # Firmware build commit short SHA (7 chars). Added in
                    # firmware ≥ 0.1.2d* (auto-generated FIRMWARE_VERSION
                    # via scripts/build_version.py). Studio shows it next
                    # to fw in the Manage tab so two dev builds with the
                    # same FIRMWARE_VERSION are distinguishable.
                    "build": r.get("build"),
                    "group": r.get("group"),
                    "wifi_connected": r.get("wifi_connected"),
                    # Hardware board ID (e.g. band_wl_v3 / band_wl_v4 /
                    # duo_wl_v3) — Studio uses this to warn the user
                    # before flashing a build for the wrong board.
                    "board": r.get("board"),
                    # node-roles taxonomy (DEC-034). Absent → Studio
                    # treats the node as a receiver on udp.
                    "role": r.get("role"),
                    "transport": r.get("transport"),
                    "transports": r.get("transports"),
                    # role-specific config snapshot (only set for the
                    # relevant role; harmless None otherwise).
                    "espnow_channel": r.get("espnow_channel"),
                    "gain": r.get("gain"),
                    "input_level": r.get("input_level"),
                    "broker_host": r.get("broker_host"),
                    "broker_port": r.get("broker_port"),
                    "topic_root": r.get("topic_root"),
                    "static_octet": r.get("static_octet"),
                    "mqtt_port": r.get("mqtt_port"),
                    "mqtt_running": r.get("mqtt_running"),
                    # broker stats for the Studio MQTT flow chart
                    # (mqtt-transport.md §8)
                    "mqtt_clients": r.get("mqtt_clients"),
                    "mqtt_pub_count": r.get("mqtt_pub_count"),
                    "mqtt_last_topic": r.get("mqtt_last_topic"),
                    "mqtt_last_payload": r.get("mqtt_last_payload"),
                    "mqtt_last_from": r.get("mqtt_last_from"),
                    "mappings_count": r.get("mappings_count"),
                    "sensor_type": r.get("sensor_type"),
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

        # ── node-roles config (DEC-034) ──
        # Per-role setup commands relayed verbatim to the device's TCP
        # 7701 config handler. The firmware ignores commands that don't
        # apply to its role (returns an error the UI surfaces). Studio's
        # serial transport drives the identical JSON over Web Serial.
        elif msg_type == "set_broker_host":
            cmd: dict = {"cmd": "set_broker_host",
                         "host": payload.get("host", "auto")}
            # Optional client-side MQTT knobs (mqtt-transport.md §7) —
            # forward only when Studio sent them so older firmware
            # keeps seeing the legacy single-field command.
            if payload.get("port") is not None:
                cmd["port"] = payload["port"]
            if payload.get("topic_root") is not None:
                cmd["topic_root"] = payload["topic_root"]
            await self._handle_tcp_command(ws, payload, cmd)

        elif msg_type == "set_espnow_channel":
            await self._handle_tcp_command(
                ws, payload,
                {"cmd": "set_espnow_channel",
                 "channel": int(payload.get("channel", 1))},
            )

        elif msg_type == "set_gain":
            await self._handle_tcp_command(
                ws, payload,
                {"cmd": "set_gain",
                 "gain": float(payload.get("gain", 0.8))},
            )

        elif msg_type == "set_input_level":
            await self._handle_tcp_command(
                ws, payload,
                {"cmd": "set_input_level",
                 "level": int(payload.get("level", 50))},
            )

        elif msg_type == "set_broker_config":
            await self._handle_tcp_command(
                ws, payload,
                {"cmd": "set_broker_config",
                 "static_octet": int(payload.get("static_octet", 10)),
                 "port": int(payload.get("port", 1883))},
            )

        elif msg_type == "set_sensor_mapping":
            await self._handle_tcp_command(
                ws, payload,
                {"cmd": "set_sensor_mapping",
                 "mappings": payload.get("mappings", [])},
            )

        elif msg_type == "get_sensor_mapping":
            await self._handle_passthrough_query(
                ws, payload, {"cmd": "get_sensor_mapping"},
                "sensor_mapping_result",
            )

        elif msg_type == "get_sensor_reading":
            # Live sensor value for Studio's threshold-tuning view
            # (polled ~1 Hz while the mapping tab is open).
            await self._handle_passthrough_query(
                ws, payload, {"cmd": "get_sensor_reading"},
                "sensor_reading_result",
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

        elif msg_type == "scan_wifi":
            # PC-side Wi-Fi scan. Studio's onboarding assumes
            # PC + Hapbeat are on the same LAN, so the PC's own
            # neighborhood is the same set the Hapbeat will see —
            # and far more convenient than asking the firmware to
            # scan (no Serial conn needed, no LAN round-trip, and
            # it works while the Hapbeat is offline being onboarded).
            await self._handle_local_wifi_scan(ws, payload)

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
        # Echo a detailed result line. Studio surfaces this in the log
        # drawer as `[helper] play sent ...`, so include the wire
        # values that drive device-side playback.
        target_label = target if target else "<broadcast>"
        msg = (
            f"play sent — event_id={event_id} "
            f"target={target_label} gain={gain:.3f} seq={seq} "
            f"udp_bytes={len(pkt)}"
        )
        await ws.send(json.dumps({
            "type": "write_result",
            "payload": {"success": True, "message": msg},
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
        cmd_name = cmd.get("cmd", "?")
        if not targets:
            await ws.send(json.dumps({
                "type": "write_result",
                "payload": {
                    "success": False,
                    "error": "no_device",
                    "message": (
                        f"{cmd_name}: 送信先デバイスが解決できません "
                        f"(payload.ip='{payload.get('ip', '')}'). "
                        "Devices タブでデバイスを選択してから再実行してください。"
                    ),
                    "cmd": cmd_name,
                },
            }))
            return

        # Run TCP I/O off the asyncio loop. We deliberately loop over
        # targets here (rather than calling _send_tcp_to_many) so the
        # WebSocket caller can emit per-target `write_progress` events
        # in between — Studio surfaces these as a per-device progress
        # row during deploy. Total wall-clock cost is the same.
        #
        # Per-IP lock: firmware has a single TCP client slot, so we
        # serialize same-device traffic to avoid mid-query displacement.
        loop = asyncio.get_running_loop()
        total = len(targets)
        results: list[dict] = []
        for idx, ip in enumerate(targets):
            await self._broadcast({
                "type": "write_progress",
                "payload": {
                    "cmd": cmd_name,
                    "ip": ip,
                    "index": idx,
                    "total": total,
                    "phase": "sending",
                },
            })
            async with self._get_tcp_lock(ip):
                r = await loop.run_in_executor(None, _send_tcp_to_one, ip, cmd)
            results.append(r)
            resp = r.get("response") or {}
            await self._broadcast({
                "type": "write_progress",
                "payload": {
                    "cmd": cmd_name,
                    "ip": ip,
                    "index": idx,
                    "total": total,
                    "phase": "done" if r.get("success") else "failed",
                    "success": bool(r.get("success")),
                    "message": (
                        resp.get("message")
                        or resp.get("error")
                        or resp.get("status")
                        or ""
                    ),
                },
            })
        ok_count = sum(1 for r in results if r.get("success"))

        # Build a verbose, per-target reason string so the Studio log
        # drawer surfaces *why* something failed instead of the bare
        # "0/1 ok". Failures usually fall into one of three buckets:
        #   1. connect failed   → device unreachable on TCP 7701
        #   2. OSError on send  → connection dropped mid-write
        #   3. status != "ok"   → firmware rejected the command
        # All three need different fixes, so distinguishing them in
        # the log saves debugging time.
        detail_lines: list[str] = []
        for r in results:
            ip = r.get("ip", "?")
            resp = r.get("response") or {}
            if r.get("success"):
                # Echo the firmware's own message so the user sees
                # what the device confirmed.
                detail_lines.append(
                    f"  ✓ {ip}: {resp.get('message') or resp.get('status') or 'ok'}"
                )
            else:
                err = resp.get("error") or resp.get("message") or "unknown"
                status = resp.get("status")
                detail_lines.append(
                    f"  ✗ {ip}: {err}"
                    + (f" (status={status})" if status and status != "ok" else "")
                )

        summary = f"{cmd_name}: {ok_count}/{len(targets)} ok"
        full_message = summary + ("\n" + "\n".join(detail_lines) if detail_lines else "")

        await ws.send(json.dumps({
            "type": "write_result",
            "payload": {
                "success": ok_count > 0,
                "device_confirmed": True,
                "message": full_message,
                "summary": summary,
                "cmd": cmd_name,
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
        # Per-IP lock — see _handle_tcp_command for rationale.
        async with self._get_tcp_lock(target):
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
        # Per-IP lock — see _handle_tcp_command for rationale.
        async with self._get_tcp_lock(target):
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
            self.udp.remove_rtt_listener(on_rtt)

    async def _handle_ota_data(self, ws, payload: dict) -> None:
        """Receive a base64-encoded firmware image and stream it to one or
        more devices sequentially.

        Each target gets its own ``ota_progress`` events keyed on
        ``device: <ip>``; final outcome per target is broadcast as
        ``ota_result``.  When the payload carries ``targets: [...]`` (or
        a single ``ip`` / ``target``) we run the OTAs **one after the
        other** — Wi-Fi/router buffers can't reliably absorb two
        simultaneous 1.7 MB streams, and a failure on device A
        shouldn't kill the queue for device B.
        """
        targets = _resolve_targets(payload, self.registry)
        bin_b64 = payload.get("bin_base64", "")
        if not targets or not bin_b64:
            # Best-effort: still emit a result so Studio's spinner clears.
            await ws.send(json.dumps({
                "type": "ota_result",
                "payload": {
                    "device": targets[0] if targets else "",
                    "success": False,
                    "message": "missing target or bin_base64",
                },
            }))
            return

        try:
            bin_bytes = base64.b64decode(bin_b64)
        except (ValueError, TypeError) as exc:
            for ip in targets:
                await ws.send(json.dumps({
                    "type": "ota_result",
                    "payload": {
                        "device": ip,
                        "success": False,
                        "message": f"bad base64: {exc}",
                    },
                }))
            return

        loop = asyncio.get_running_loop()
        total_targets = len(targets)
        if total_targets > 1:
            logger.info(
                "OTA batch start: %d targets %s (%d bytes each)",
                total_targets, targets, len(bin_bytes),
            )
            await self._broadcast({
                "type": "ota_batch",
                "payload": {
                    "phase": "begin",
                    "targets": list(targets),
                    "total": total_targets,
                },
            })

        for idx, target in enumerate(targets):
            await ws.send(json.dumps({
                "type": "ota_progress",
                "payload": {
                    "device": target,
                    "phase": "begin",
                    "percent": 0,
                    "message": (
                        f"OTA 開始 ({idx + 1}/{total_targets} — {len(bin_bytes):,} bytes)"
                        if total_targets > 1
                        else f"OTA 開始 ({len(bin_bytes):,} bytes)"
                    ),
                },
            }))

            # Bind the closure to *this* target so per-target progress
            # events stay tagged with the right IP across the loop.
            def make_progress(ip: str):
                def _p(phase: str, percent: int, message: str) -> None:
                    asyncio.run_coroutine_threadsafe(
                        self._broadcast({
                            "type": "ota_progress",
                            "payload": {
                                "device": ip, "phase": phase,
                                "percent": percent, "message": message,
                            },
                        }),
                        loop,
                    )
                return _p
            progress = make_progress(target)

            # NOTE: do NOT explicitly pause the log_tail supervisor here.
            # The supervisor's own loop already checks
            # ``target in self._ota_in_progress`` before reconnecting and
            # naturally waits out the OTA.

            # Per-IP lock — OTA holds the device's TCP slot for the full
            # transfer. Without serialization, a stray get_info /
            # get_ap_status query from Studio would race for the same
            # slot and either kick OTA off or itself fail.
            self._ota_in_progress.add(target)
            ok = False
            msg = ""
            try:
                async with self._get_tcp_lock(target):
                    # Safety net: if the executor blocks indefinitely
                    # (e.g. Windows WinError 10054 / sendall race), the
                    # lock would never be released and subsequent OTA /
                    # commands would hang forever until helper restart.
                    # 600 s is well above the worst-case transfer time
                    # (~1.7 MB / 5 KB/s).
                    try:
                        ok, msg = await asyncio.wait_for(
                            loop.run_in_executor(
                                None, _do_ota_to_device,
                                target, bin_bytes, progress,
                            ),
                            timeout=600.0,
                        )
                    except asyncio.TimeoutError:
                        ok = False
                        msg = (
                            "phase=stuck: OTA executor blocked >600 s — "
                            "recovered"
                        )
                        logger.error(
                            "OTA executor stuck for %s >600 s — forcing recovery",
                            target,
                        )
            finally:
                self._ota_in_progress.discard(target)

            await self._broadcast({
                "type": "ota_result",
                "payload": {
                    "device": target, "success": ok, "message": msg,
                },
            })

            if total_targets > 1:
                logger.info(
                    "OTA batch %d/%d done (%s): success=%s",
                    idx + 1, total_targets, target, ok,
                )

        if total_targets > 1:
            await self._broadcast({
                "type": "ota_batch",
                "payload": {
                    "phase": "done",
                    "targets": list(targets),
                    "total": total_targets,
                },
            })

    async def _handle_local_wifi_scan(self, ws, payload: dict) -> None:
        """Run an OS-native Wi-Fi scan and ship the result back.

        Returns ``{type: "scan_wifi_result", payload: {networks, error?}}``
        with networks shaped like the Serial-side scan
        (`{ssid, rssi, channel?, auth?}`) so Studio can render either
        source through the same UI.
        """
        del payload  # nothing to read — scan is OS-global
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _local_wifi_scan)
        await ws.send(json.dumps({
            "type": "scan_wifi_result",
            "payload": result,
        }))

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

        # Serial pseudo-devices ("serial:<mac>") stream their logs over the
        # USB serial conn, not TCP 7701. A log-tail to that string just
        # NXDOMAINs (`getaddrinfo failed`) and the self-healing supervisor
        # retries it in a tight loop forever (user report 2026-06-13). Refuse
        # anything that isn't a real IPv4 device address.
        if target.startswith("serial:") or not _is_ipv4(target):
            await ws.send(json.dumps({
                "type": "log_subscription",
                "payload": {"device": target, "ok": False, "error": "no_tcp_log"},
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

        # Self-healing supervisor: the firmware has a single TCP client
        # slot, so a log_tail subscription gets displaced whenever Studio
        # issues a regular command (write_ui_config, OTA, kit deploy,
        # get_info, etc.). Without restart, the user loses log forwarding
        # forever until they manually re-open LogDrawer. This loop
        # detects natural exits (= displaced or peer dropped) and
        # restarts the underlying TCP after a short backoff. Stops only
        # when the explicit `stop` event is set (= unsubscribe / WS
        # close handler).
        def _supervised_worker() -> None:
            # Start at 1s (was 0.3s): on a single-slot device the tail is
            # displaced by every poll, so a tight 0.3s restart just ping-pongs
            # against the poller and churns the device's TCP. A 1–4s backoff
            # lets the poll finish before the tail re-grabs the slot.
            backoff = 1.0
            while not stop.is_set():
                try:
                    _log_tail_worker(target, stop, relay)
                except Exception:  # noqa: BLE001
                    logger.exception("log tail (%s) crashed; restarting", target)
                if stop.is_set():
                    break
                # OTA 中は log_tail の再接続を抑止して静かに待機する。
                # OTA 完了後に 1 回だけ再接続させる。
                if target in self._ota_in_progress:
                    while not stop.is_set() and target in self._ota_in_progress:
                        time.sleep(0.5)
                    if not stop.is_set():
                        logger.info("log tail (%s) OTA 完了 — 再接続", target)
                    backoff = 0.3
                else:
                    # Short backoff before reconnecting; longer if firmware
                    # is busy (give the displacing command time to finish).
                    time.sleep(backoff)
                    backoff = min(backoff * 1.5, 4.0)
                    if not stop.is_set():
                        # debug, not info: displacement-driven restarts are
                        # routine churn, not something the user needs to watch.
                        logger.debug("log tail (%s) auto-restart", target)
            # Natural exit — clean up dict entries so subsequent
            # subscribe_logs requests can re-create the thread.
            self._log_stop_flags.pop(target, None)
            self._log_threads.pop(target, None)

        t = threading.Thread(
            target=_supervised_worker,
            daemon=True,
            name=f"log-tail-sup-{target}",
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
        # Per-IP lock — see _handle_tcp_command for rationale.
        async with self._get_tcp_lock(target):
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

                # Per-IP lock — kit deploy holds the device's TCP slot
                # for the full transfer (multiple file_begin / chunks /
                # kit_commit). See _handle_tcp_command for rationale.
                async with self._get_tcp_lock(ip):
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

def _send_tcp_to_one(ip: str, cmd: dict) -> dict:
    """Send *cmd* to a single IP. Extracted from `_send_tcp_to_many` so
    callers that want per-target progress can loop in their own context
    (e.g. emitting ``write_progress`` push events between targets).
    Returns ``{ip, success, response}``.
    """
    cmd_name = cmd.get("cmd", "?")
    with TcpRawConnection(ip) as conn:
        t0 = time.monotonic()
        try:
            connected = conn.connect()
        except OSError as exc:
            return {"ip": ip, "success": False,
                    "response": {
                        "error": f"connect raised {type(exc).__name__}: {exc}",
                        "phase": "connect",
                        "cmd": cmd_name,
                    }}
        if not connected:
            # Most common path: TCP 7701 closed (no firmware listening /
            # firewall) or device is in a transient post-Wi-Fi-switch
            # state where the listening socket hasn't been re-bound yet.
            return {"ip": ip, "success": False,
                    "response": {
                        "error": (
                            f"TCP 7701 connect failed to {ip} "
                            f"({(time.monotonic() - t0) * 1000:.0f} ms). "
                            "デバイスがオンラインに見えても TCP サーバが "
                            "起動していない場合があります — 電源を一度 OFF→ON してください。"
                        ),
                        "phase": "connect",
                        "cmd": cmd_name,
                    }}
        try:
            conn.send_json(cmd)
            resp = conn.read_response(timeout=10.0) or {}
        except OSError as exc:
            return {"ip": ip, "success": False,
                    "response": {
                        "error": f"send/read raised {type(exc).__name__}: {exc}",
                        "phase": "io",
                        "cmd": cmd_name,
                    }}
        if not resp:
            return {"ip": ip, "success": False,
                    "response": {
                        "error": (
                            "device disconnected before reply "
                            "(connection accepted but TCP server closed it)"
                        ),
                        "phase": "no_reply",
                        "cmd": cmd_name,
                    }}
        return {
            "ip": ip,
            "success": resp.get("status") == "ok",
            "response": resp,
        }


def _send_tcp_query(ip: str, cmd_name: str) -> Optional[dict]:
    with TcpRawConnection(ip) as conn:
        if not conn.connect():
            return None
        conn.send_json({"cmd": cmd_name})
        return conn.read_response(timeout=5.0)


def _drain_pending_lines(
    sock: socket.socket, buf: bytes, *, max_bytes: int = 65536,
) -> tuple[list[dict], bytes, bool]:
    """Non-blocking drain of any complete JSON lines on *sock*.

    Returns ``(parsed_lines, remaining_buf, eof)``.  ``eof`` is True if
    the peer closed (recv returned empty).  Never blocks — uses
    ``select`` with timeout=0.
    """
    parsed: list[dict] = []
    eof = False
    total = 0
    while total < max_bytes:
        try:
            ready, _, _ = select.select([sock], [], [], 0)
        except (OSError, ValueError):
            break
        if not ready:
            break
        try:
            chunk = sock.recv(4096)
        except (BlockingIOError, socket.timeout):
            break
        except OSError:
            break
        if not chunk:
            eof = True
            break
        buf += chunk
        total += len(chunk)

    while b"\n" in buf:
        line_b, buf = buf.split(b"\n", 1)
        line = line_b.decode("utf-8", errors="replace").strip()
        if not line.startswith("{"):
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return parsed, buf, eof


def _do_ota_to_device(
    ip: str, bin_bytes: bytes, progress,
) -> tuple[bool, str]:
    """Push a firmware image to one device over TCP 7701.

    Streaming is interleaved with non-blocking recv so we can surface the
    *device-confirmed* progress (firmware emits ``{"status":"progress",
    "percent":N}`` every 5 %) instead of just helper's send-buffer
    progress.  This catches the misleading "28 % then stuck" symptom:
    helper's local TCP send buffer happily absorbs the first ~half MB
    even when the device is not draining its RX, so the old
    bytes-sent-only progress could climb to ~28 % while the device was
    actually stuck at 0 %.  We now abort early with ``phase=stall`` when
    the device fails to confirm any progress within
    ``NO_DEVICE_PROGRESS_TIMEOUT`` seconds.
    """
    file_size = len(bin_bytes)
    chunk_size = 4096
    # Device must report at least one progress update within this many
    # seconds of streaming start, otherwise we abort.
    INITIAL_DEVICE_PROGRESS_GRACE = 8.0
    # After we've seen *some* device progress, abort if the percent
    # value doesn't change for this long.
    NO_DEVICE_PROGRESS_TIMEOUT = 20.0
    # Log INFO line each time these many percent of *sent* bytes pass.
    LOG_EVERY_PCT = 5

    with TcpRawConnection(ip) as conn:
        t0 = time.monotonic()
        try:
            connected = conn.connect()
        except OSError as exc:
            return False, (
                f"phase=connect: TCP 7701 → {ip} raised "
                f"{type(exc).__name__}: {exc}. "
                "デバイスがオンライン表示でも TCP server が立ち上がっていない "
                "ことがあります — 電源を OFF→ON してから再試行してください。"
            )
        if not connected:
            return False, (
                f"phase=connect: TCP 7701 → {ip} 接続失敗 "
                f"({(time.monotonic() - t0) * 1000:.0f} ms). "
                "デバイスがオンライン表示でも TCP server が立ち上がっていない "
                "ことがあります — 電源を OFF→ON してから再試行してください。"
            )
        sock = conn.sock
        if sock is None:
            return False, "phase=connect: socket missing after connect"

        try:
            conn.send_json({"cmd": "ota_begin", "size": file_size})
            resp = conn.read_response(timeout=5.0)
            if not resp or resp.get("status") != "ok":
                return False, f"phase=ota_begin: nack {resp}"
            logger.info("OTA %s: ota_begin ok, streaming %d bytes", ip, file_size)

            sent = 0
            recv_buf = b""
            device_pct = 0
            stream_start = time.monotonic()
            last_device_pct_change = stream_start
            last_logged_pct_bucket = 0

            for off in range(0, file_size, chunk_size):
                chunk = bin_bytes[off : off + chunk_size]
                try:
                    conn.send_raw(chunk, timeout=10.0)
                except OSError as exc:
                    return False, (
                        f"phase=stream-send: {type(exc).__name__}: {exc} "
                        f"(sent={sent:,}/{file_size:,}, device_pct={device_pct})"
                    )
                sent += len(chunk)
                sent_pct = int(sent / file_size * 95) + 1

                # Drain any device responses that arrived while we sent.
                lines, recv_buf, eof = _drain_pending_lines(sock, recv_buf)
                if eof:
                    return False, (
                        f"phase=stream-recv: device closed connection "
                        f"(sent={sent:,}/{file_size:,}, device_pct={device_pct})"
                    )
                for obj in lines:
                    status = obj.get("status", "")
                    if status == "progress":
                        new_pct = int(obj.get("percent", 0))
                        if new_pct > device_pct:
                            device_pct = new_pct
                            last_device_pct_change = time.monotonic()
                    elif status == "error":
                        return False, (
                            f"phase=stream: device error: "
                            f"{obj.get('message', 'unknown')}"
                        )
                    elif status == "ok":
                        # Unexpected early completion — but treat as success.
                        progress("done", 100, obj.get("message", "OK"))
                        return True, obj.get("message", "OK")

                # Stall detection.
                now = time.monotonic()
                if device_pct == 0 and (now - stream_start) > INITIAL_DEVICE_PROGRESS_GRACE:
                    return False, (
                        f"phase=stall: chunk 送信開始から {now - stream_start:.0f}s 経つが "
                        f"device は 0% のまま (sent={sent:,}/{file_size:,}). "
                        "TCP buffer に積まれているだけで device 側 processOtaData が "
                        "走っていない可能性が高い。device 電源 OFF→ON 推奨。"
                    )
                if device_pct > 0 and (now - last_device_pct_change) > NO_DEVICE_PROGRESS_TIMEOUT:
                    return False, (
                        f"phase=stall: device は {device_pct}% で "
                        f"{now - last_device_pct_change:.0f}s 進まず "
                        f"(sent={sent:,}/{file_size:,}). flash が固まっている可能性。"
                    )

                # 5%-bucket INFO log so foreground monitoring can pinpoint
                # where streaming actually stalls.
                pct_bucket = (sent_pct // LOG_EVERY_PCT) * LOG_EVERY_PCT
                if pct_bucket > last_logged_pct_bucket:
                    logger.info(
                        "OTA %s: sent=%d%% device=%d%% (%d/%d bytes)",
                        ip, sent_pct, device_pct, sent, file_size,
                    )
                    last_logged_pct_bucket = pct_bucket

                # UI: prefer device-confirmed pct so user doesn't get
                # misled by helper buffer fill.  Fall back to sent_pct
                # only before first device confirmation.
                shown = device_pct if device_pct > 0 else 1
                progress(
                    "upload", shown,
                    f"送信 {sent_pct}% / device {device_pct}% "
                    f"({sent:,}/{file_size:,})",
                )

            logger.info(
                "OTA %s: streaming done (sent=%d bytes, device_pct=%d)",
                ip, sent, device_pct,
            )
            progress("flash", max(device_pct, 96), "デバイス書込待ち…")

            # Verify phase — drain remaining buffered lines first, then
            # block for ok / error.
            verify_deadline = time.monotonic() + 30.0
            while time.monotonic() < verify_deadline:
                # Process any already-buffered lines first.
                lines, recv_buf, eof = _drain_pending_lines(sock, recv_buf)
                if eof:
                    return False, "phase=verify: device closed connection"
                handled_inline = False
                for obj in lines:
                    handled_inline = True
                    status = obj.get("status", "")
                    if status == "ok":
                        progress("done", 100, "OTA 完了")
                        return True, obj.get("message", "OK")
                    if status == "error":
                        return False, (
                            f"phase=verify: {obj.get('message', 'OTA error')}"
                        )
                    if status == "progress":
                        pct = int(obj.get("percent", 0))
                        if pct > device_pct:
                            device_pct = pct
                        progress(
                            "flash", min(96 + pct // 25, 99),
                            f"書込中 {pct}%",
                        )
                if handled_inline:
                    continue
                # Nothing buffered — block for one more line.
                resp = conn.read_response(timeout=2.0)
                if resp is None:
                    continue  # keep polling until verify_deadline
                status = resp.get("status", "")
                if status == "ok":
                    progress("done", 100, "OTA 完了")
                    return True, resp.get("message", "OK")
                if status == "error":
                    return False, f"phase=verify: {resp.get('message', 'OTA error')}"
                if status == "progress":
                    pct = int(resp.get("percent", 0))
                    if pct > device_pct:
                        device_pct = pct
                    progress(
                        "flash", min(96 + pct // 25, 99),
                        f"書込中 {pct}%",
                    )
            return False, (
                "phase=verify: デバイスからの ok/error が 30 秒以内に来ません。"
                " Update.end が失敗している可能性があります — シリアルログで "
                "[OTA] エラー行を確認してください。"
            )
        except OSError as exc:
            return False, f"phase=io: {type(exc).__name__}: {exc}"


def _is_ipv4(s: str) -> bool:
    """True for a dotted-quad IPv4 literal (rejects "serial:<mac>", hosts)."""
    try:
        socket.inet_aton(s)
    except OSError:
        return False
    return s.count(".") == 3


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
        # A connection reset/abort is the EXPECTED outcome when a regular
        # command (get_info / get_sensor_reading poll, kit deploy, …)
        # displaces this tail on the device's single TCP slot. Log it quietly
        # so normal operation doesn't produce a 10054 warning storm; only
        # genuinely unexpected errors stay at warning level.
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            logger.debug("log tail (%s) displaced: %s", ip, exc)
        else:
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
            # SO_LINGER(on=1, linger=0): RST on close, freeing both ends
                # immediately. Same rationale as TcpRawConnection.close —
                # without this, a displaced log_tail leaves a FIN_WAIT_2
                # entry on helper and a still-allocated socket fd on the
                # device's lwIP pool. Repeated displacement (during OTA
                # supervisor restarts, kit deploys, etc.) silently exhausts
                # the pool, blocking new accepts. (Diagnosed 2026-05-09:
                # netstat showed ESTABLISHED + FIN_WAIT_2 piling up to
                # 192.168.0.108:7701 even after helper Ctrl+C.)
            try:
                sock.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER,
                    struct.pack("ii", 1, 0),
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


def _looks_like_manifest(filename: str) -> bool:
    """True if *filename* matches the kit-manifest discovery pattern.

    Convention (2026-05-17, instructions-kitname-manifest-rename):
    kits ship `<kit-name>-manifest.json`. We accept any `*manifest*.json`
    (case-insensitive) as a manifest candidate so the legacy
    `manifest.json` filename also matches.
    """
    n = filename.lower()
    return n.endswith(".json") and "manifest" in n


def _find_kit_manifest(pack_dir: Path) -> Path | None:
    """Return the kit manifest path inside *pack_dir*, or None if missing.

    Preferred name: `<pack_dir.name>-manifest.json` exact match.
    Fallback: first `*manifest*.json` (case-insensitive) at the kit root.
    """
    preferred = pack_dir / f"{pack_dir.name}-manifest.json"
    if preferred.exists():
        return preferred
    for child in sorted(pack_dir.iterdir()):
        if child.is_file() and _looks_like_manifest(child.name):
            return child
    return None


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
    # Manifest file naming (2026-05-17): kits ship `<kit-name>-manifest.json`
    # so multiple kits stay identifiable in OS Explorer. We try the preferred
    # name first, then fall back to any `*manifest*.json` (covers legacy
    # `manifest.json` and any custom suffix). Mirrors the SDK & Studio
    # discovery rule from `instructions-kitname-manifest-rename-202605161800.md`.
    manifest_path = _find_kit_manifest(pack_dir)
    if manifest_path is None:
        return False, "manifest missing (looked for <name>-manifest.json / *manifest*.json)"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"manifest read failed: {exc}"
    # Schema 2.0.0 (DEC-028): the kit's identity field is `name` — the
    # legacy `kit_id` field was removed. We still honor the WS-supplied
    # `kit_id_default` first because the caller (Studio) sends it
    # explicitly and that's the value the device will store; manifest
    # `name` is the fallback when the caller didn't pass one.
    kit_id = kit_id_default or manifest.get("name") or "unknown"

    files: list[tuple[str, Path]] = []
    for fp in sorted(pack_dir.rglob("*")):
        if fp.is_file():
            rel = fp.relative_to(pack_dir).as_posix()
            # stream-clips/ are SDK-side (Unity streams them at runtime).
            # The firmware does not store them; exclude to avoid wasting
            # LittleFS space and to prevent install-commit confusion.
            if rel.startswith("stream-clips/"):
                continue
            # Normalize the manifest filename on the wire. Device firmware
            # reads `manifest.json` from its LittleFS (kit_loader.cpp); the
            # `<kit-name>-` prefix is a host-side identifier only.
            if fp == manifest_path:
                rel = "manifest.json"
            elif _looks_like_manifest(fp.name) and fp.parent == pack_dir:
                # Stray secondary manifest sitting next to the chosen one
                # — skip so the device doesn't receive two manifests in
                # one kit (would leave LittleFS in an ambiguous state).
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


# ── Local Wi-Fi scan (OS-native) ─────────────────────────────────

def _local_wifi_scan() -> dict:
    """Cross-platform Wi-Fi scan via OS CLI.

    Returns ``{networks: [{ssid, rssi, channel?, auth?}, ...], error?}``.
    The list is de-duped by SSID (keeping the strongest BSSID per SSID
    to mirror what the firmware-side scan does) and sorted strongest-first.
    Empty list is a valid response — we return ``error`` only when the
    underlying CLI is missing or the OS isn't supported.
    """
    import platform
    import subprocess

    sysname = platform.system()
    try:
        if sysname == "Windows":
            networks = _scan_windows_netsh()
        elif sysname == "Darwin":
            networks = _scan_macos_airport()
        elif sysname == "Linux":
            networks = _scan_linux_nmcli()
        else:
            return {"networks": [], "error": f"unsupported OS: {sysname}"}
    except FileNotFoundError as exc:
        return {"networks": [], "error": f"scanner not installed: {exc}"}
    except subprocess.SubprocessError as exc:
        return {"networks": [], "error": f"scan failed: {exc}"}
    except OSError as exc:
        return {"networks": [], "error": f"scan os-error: {exc}"}

    # Dedupe by SSID, keep strongest signal
    by_ssid: dict[str, dict] = {}
    for n in networks:
        ssid = (n.get("ssid") or "").strip()
        if not ssid:
            continue
        prev = by_ssid.get(ssid)
        if prev is None or n.get("rssi", -200) > prev.get("rssi", -200):
            by_ssid[ssid] = n
    out = sorted(
        by_ssid.values(),
        key=lambda n: -(n.get("rssi") or -200),
    )
    return {"networks": out}


def _scan_windows_netsh() -> list[dict]:
    """Parse `netsh wlan show networks mode=Bssid` output.

    Both English ("SSID", "Signal", "Authentication", "Channel") and
    Japanese ("シグナル", "認証", "チャネル") locale labels are matched
    so the same code works on Windows en-US and ja-JP installs.
    """
    import subprocess

    out = subprocess.check_output(
        ["netsh", "wlan", "show", "networks", "mode=Bssid"],
        timeout=10,
        stderr=subprocess.STDOUT,
    )
    # netsh respects the system code page; on Japanese Windows that's
    # cp932, on English en-US it's cp1252. Try utf-8 first then fall back.
    text = ""
    for enc in ("utf-8", "cp932", "cp1252", "latin-1"):
        try:
            text = out.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return []

    nets: list[dict] = []
    cur: Optional[dict] = None
    for raw in text.splitlines():
        line = raw.strip()
        # `SSID 1 : MyNetwork` — top of an SSID block. Inner BSSID
        # lines also contain " : " but start with "BSSID", so the
        # `startswith("SSID ")` guard distinguishes them.
        if line.startswith("SSID ") and " : " in line:
            if cur is not None and cur.get("ssid"):
                nets.append(cur)
            cur = {"ssid": line.split(" : ", 1)[1].strip(), "rssi": -100}
            continue
        if cur is None:
            continue
        if " : " not in line:
            continue
        key, val = (s.strip() for s in line.split(" : ", 1))
        klow = key.lower()
        if klow.startswith("authentication") or "認証" in key:
            cur["auth"] = val
        elif klow.startswith("signal") or "シグナル" in key:
            try:
                pct = int(val.rstrip("%").strip())
                # Map percent to a rough dBm value: 100% ≈ -50 dBm,
                # 0% ≈ -100 dBm. Same approximation Windows uses
                # internally for the WLAN_SIGNAL_QUALITY field.
                cur["rssi"] = max(-100, min(-30, pct // 2 - 100))
            except ValueError:
                pass
        elif klow.startswith("channel") or "チャネル" in key:
            try:
                cur["channel"] = int(val.split()[0])
            except (ValueError, IndexError):
                pass
    if cur is not None and cur.get("ssid"):
        nets.append(cur)
    return nets


def _scan_macos_airport() -> list[dict]:
    """Scan Wi-Fi networks on macOS.

    Strategy:
    1. Try `airport -s` (works on macOS ≤ 13; deprecated in 14+ Sonoma).
    2. If airport returns nothing, fall back to `system_profiler SPAirPortDataType`
       which works on macOS 14+ without admin privileges (no passwords exposed).
    """
    import subprocess

    airport = (
        "/System/Library/PrivateFrameworks/Apple80211.framework"
        "/Versions/Current/Resources/airport"
    )
    try:
        out = subprocess.check_output(
            [airport, "-s"], timeout=10,
        ).decode("utf-8", errors="replace")
        nets: list[dict] = []
        # Skip header line, parse columns: SSID BSSID RSSI CHANNEL HT SECURITY
        for line in out.splitlines()[1:]:
            if not line.strip():
                continue
            # SSID may contain spaces; rsplit from the right is safer.
            parts = line.rsplit(None, 6)
            if len(parts) < 7:
                continue
            ssid = parts[0]
            try:
                rssi = int(parts[2])
                channel = int(parts[3].split(",")[0])
            except (ValueError, IndexError):
                continue
            nets.append({
                "ssid": ssid,
                "rssi": rssi,
                "channel": channel,
                "auth": parts[6],
            })
        if nets:
            return nets
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass

    # Fallback: system_profiler (macOS 14+ Sonoma compatible)
    return _scan_macos_system_profiler()


def _scan_macos_system_profiler() -> list[dict]:
    """Parse `system_profiler SPAirPortDataType` output.

    Works on macOS 14+ where `airport -s` no longer reliably returns results.
    Parses the human-readable text output to extract SSID and RSSI.
    Returns an empty list (never raises) — caller treats empty as
    "scan unavailable, user should type SSID manually".
    """
    import subprocess

    try:
        out = subprocess.check_output(
            ["system_profiler", "SPAirPortDataType"],
            timeout=15,
        ).decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return []

    nets: list[dict] = []
    current_ssid: str | None = None
    in_other_networks = False

    for line in out.splitlines():
        stripped = line.strip()
        # Section header for "Other Local Wi-Fi Networks"
        if "Other Local Wi-Fi Networks" in stripped or "Other Wi-Fi Networks" in stripped:
            in_other_networks = True
            continue
        # SSID lines look like: "NetworkName:" at indentation level 12+
        if in_other_networks:
            # A new network entry ends with ':'
            if stripped.endswith(":") and ":" not in stripped[:-1]:
                current_ssid = stripped[:-1]
                continue
            # RSSI line: "Signal / Noise: -65 dBm / -90 dBm"
            if current_ssid and ("Signal" in stripped or "RSSI" in stripped):
                m_rssi = None
                import re
                m = re.search(r'(-\d+)\s*dBm', stripped)
                if m:
                    m_rssi = int(m.group(1))
                nets.append({
                    "ssid": current_ssid,
                    "rssi": m_rssi or -80,
                    "channel": 0,
                    "auth": "",
                })
                current_ssid = None

    return nets


def _scan_linux_nmcli() -> list[dict]:
    """Parse `nmcli -t -f SSID,SIGNAL,CHAN,SECURITY dev wifi list`."""
    import subprocess

    out = subprocess.check_output(
        [
            "nmcli", "-t", "-f", "SSID,SIGNAL,CHAN,SECURITY",
            "dev", "wifi", "list",
        ],
        timeout=10,
    ).decode("utf-8", errors="replace")
    nets: list[dict] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        cols = line.split(":")
        if len(cols) < 4:
            continue
        ssid = cols[0]
        if not ssid:
            continue
        try:
            pct = int(cols[1])
            rssi = max(-100, min(-30, pct // 2 - 100))
        except ValueError:
            rssi = -100
        try:
            channel = int(cols[2])
        except ValueError:
            channel = None
        nets.append({
            "ssid": ssid, "rssi": rssi,
            **({"channel": channel} if channel is not None else {}),
            "auth": cols[3] or None,
        })
    return nets
