# Hapbeat Helper — context for AI coding agents

Single self-contained reference so an AI coding agent can OPERATE this tool
correctly from one file. This is a **CLI daemon** (background infra with a CLI +
WebSocket JSON API) — NOT a code library you `import`. Distribution name:
`hapbeat-helper`.

- last-verified-against: source under `src/hapbeat_helper/` (no release tag baked in)
- Source of truth is the code: CLI in `src/hapbeat_helper/cli.py`, WebSocket
  command surface in `src/hapbeat_helper/server.py` (`HelperServer._dispatch`),
  ports in `server.py` (`WS_PORT`) + `udp_listener.py` (`HAPBEAT_UDP_PORT`),
  service install in `src/hapbeat_helper/service/`. If this file disagrees with
  the code, the code wins.
- Canonical docs: https://devtools.hapbeat.com/docs/tools/helper/

## What it is

A local daemon that bridges **Hapbeat Studio** (the web SPA at
`https://devtools.hapbeat.com`) to Hapbeat hardware on the LAN. The browser
cannot do mDNS, UDP broadcast, or raw TCP — Helper does those and relays Studio
requests over the wire. It exposes a WebSocket on `ws://localhost:7703` and
speaks UDP 7700 (PLAY / STOP / PING / streaming) and TCP 7701 (config / kit
deploy) to devices.

It does NOT: own serial (Studio drives Web Serial directly), pick which device
to target (Studio sends explicit `targets`/`ip`), author kits, or store
device-selection state. The wire format and event id are defined by
**hapbeat-contracts** — Helper only relays.

## Install (pipx)

```bash
pipx install hapbeat-helper      # isolated venv, entry point on PATH
pipx upgrade hapbeat-helper      # update
pipx uninstall hapbeat-helper
```
Local dev from a clone: `pipx install -e .` (editable), or a plain venv
`pip install -e ".[dev]"`. Prerequisite: pipx (`py -m pip install --user pipx;
py -m pipx ensurepath` on Windows, `brew install pipx; pipx ensurepath` on
macOS). Python tool — the isolated venv means no Python-version juggling.

## Run

```bash
hapbeat-helper start            # foreground (Ctrl+C to stop); --port N --verbose
hapbeat-helper install-service  # register OS auto-start AND start now
hapbeat-helper service-status   # not_registered / stopped / running
hapbeat-helper uninstall-service
```
After it is running, open `https://devtools.hapbeat.com` — Studio connects
automatically. Auto-start: macOS uses a launchd agent
(`~/Library/LaunchAgents/com.hapbeat.helper.plist`); Windows uses a Task
Scheduler logon task (`HapbeatHelper`), falling back to a Startup-folder VBS
shim on older Windows.

## CLI (verbatim from `cli.py`)

```
hapbeat-helper start [--port 7703] [-v/--verbose]
hapbeat-helper status [--port 7703]      # TCP-probe ws://localhost:7703
hapbeat-helper version
hapbeat-helper stop                      # stop the auto-started instance
hapbeat-helper logs [-n N] [-f/--follow] # show + tail the auto-start log file
hapbeat-helper install-service
hapbeat-helper uninstall-service
hapbeat-helper service-status
hapbeat-helper config show               # print config dir path + contents
```
Also runnable as `python -m hapbeat_helper`.

## WebSocket JSON API

Connect to `ws://localhost:7703`. Every message is
`{"type": "...", "payload": {...}}`. On connect, Helper sends
`{"type":"helper_hello","payload":{"version":...}}` then a `device_list`.
Device-bound messages take destination IPs via `payload.targets` (list) or
`payload.ip` / `payload.target` (single).

Smoke test (`websocat`):
```bash
echo '{"type":"ping","payload":{}}'         | websocat ws://localhost:7703
echo '{"type":"list_devices","payload":{}}' | websocat ws://localhost:7703
```

Request `type` values handled by `_dispatch` (verbatim):
- session/discovery: `ping` → `pong`; `list_devices` / `rescan` → `device_list`
- playback (UDP; devices self-filter on `target` regardless of routing):
  `preview_event` (alias `play_event`), `stop_event` — `payload`: `event_id`,
  `target`, `gain`, optional `targets`/`ip`.
  Destination order: explicit `targets`/`ip` → online devices whose address
  matches `target` (unicast) → broadcast. Broadcast is the last resort, never a
  skip: a dropped STOP would leave a looping clip running. Never both at once.
- TCP config (→ device TCP 7701): `write_ui_config`, `set_wifi`, `set_name`,
  `set_address`, `set_oled_brightness`, `reboot`, `clear_wifi`,
  `connect_wifi_profile`, `remove_wifi_profile`, `kit_delete`
- SoftAP: `get_ap_status`, `enter_ap_mode`, `enter_sta_mode`, `set_ap_pass`,
  `clear_ap_pass`
- queries (→ typed `*_result`): `get_info`, `get_wifi_status`,
  `list_wifi_profiles`, `get_oled_brightness`, `get_debug_dump`, `kit_list`,
  `query_space`, `query_volume`, `ping_device`, `get_sensor_mapping`,
  `get_sensor_reading`, `scan_wifi`
- node-roles config (DEC-034): `set_broker_host`, `set_espnow_channel`,
  `set_gain`, `set_input_level`, `set_broker_config`, `set_sensor_mapping`,
  `set_alert_mode`, `set_recv_topics`
- logs: `subscribe_logs` / `unsubscribe_logs` → `device_log` lines
- firmware/kit: `ota_data` (`payload.bin_base64`) → `ota_progress`/`ota_result`;
  `deploy_kit_data` (`payload.zip_base64` + explicit `targets`) →
  `deploy_progress`/`deploy_result`
- streaming: `stream_begin` / `stream_data` / `stream_end`

Unknown types reply `{"type":"error","payload":{"message":"unknown type: ..."}}`.

## Ports

| port | proto | who | what |
|---|---|---|---|
| 7703 | WS | Studio ↔ Helper | JSON command/event channel (`WS_PORT`) |
| 7700 | UDP | Helper ↔ device | PLAY / STOP / PING / streaming (`HAPBEAT_UDP_PORT`) |
| 7701 | TCP | Helper ↔ device | config / queries / kit deploy / OTA / log tail |

Helper is the **sole owner of UDP 7700** — `hapbeat-manager` claims the same
ports, so the two cannot run at once.

## Config / data locations

- Config dir: Windows `%APPDATA%\hapbeat-helper`, otherwise
  `~/.config/hapbeat-helper`. Optional `config.toml`; `config show` prints
  `(no config file yet — defaults are in use)` when absent (it runs fine with
  no config file).
- Auto-start log: Windows `%LOCALAPPDATA%\hapbeat-helper\hapbeat-helper.log`,
  macOS `~/Library/Logs/hapbeat-helper.log`.

## Common errors and fixes

- **Studio shows "Helper 未接続"** → start it: `hapbeat-helper start` (or
  `install-service`).
- **`ERROR: unknown type: <message>`** in Studio's log drawer → the helper is
  older than the Studio build; `pipx upgrade hapbeat-helper` (or `git pull` +
  restart for an editable install).
- **`UDP port 7700 is unavailable. Is hapbeat-manager already running?`** →
  `RuntimeError` at startup; stop `hapbeat-manager` (it owns 7700/7703).
- **No devices found** → device + PC must share the Wi-Fi network; some
  hotspot/AP modes block UDP broadcast and mDNS.
- **Firefox cannot reach `ws://localhost:7703` from HTTPS Studio** → set
  `network.websocket.allowInsecureFromHTTPS = true` (Chrome/Edge work as-is).
- **Windows `pipx install` fails `WinError 448 — untrusted mount point`** →
  OneDrive-synced home; set `PIPX_HOME` / `PIPX_BIN_DIR` outside the synced tree.

## More detail

When this single file is not enough, an agent can fetch:

- **Complete reference in one text file (recommended next step):** https://devtools.hapbeat.com/_llms-txt/helper.txt
- **Concepts** (shared by every SDK): event id <-> kit https://devtools.hapbeat.com/docs/concepts/event-id-and-kit/ - command vs clip https://devtools.hapbeat.com/docs/concepts/fire-vs-clip/ - targeting https://devtools.hapbeat.com/docs/concepts/group-player-addressing/
- Human docs: https://devtools.hapbeat.com/docs/tools/helper/ - Portal: https://devtools.hapbeat.com/
