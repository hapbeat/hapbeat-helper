# hapbeat-helper

Local daemon that bridges **Hapbeat Studio** (Web SPA at `https://devtools.hapbeat.com`)
to Hapbeat hardware on the local network.

The browser cannot do mDNS, UDP broadcast, or raw TCP sockets directly.
`hapbeat-helper` runs in the background, exposes a WebSocket on
`ws://localhost:7703`, and relays Studio requests to the devices using
UDP (port 7700) and TCP (port 7701).

```
Studio (https://devtools.hapbeat.com)
        │  ws://localhost:7703 (JSON)
        ▼
hapbeat-helper (this daemon)
        │  UDP 7700 (PLAY / STOP / PING / streaming)
        │  TCP 7701 (config / kit deploy)
        │  mDNS (_hapbeat._udp.local.)
        ▼
   Hapbeat devices
```

## Install

`hapbeat-helper` is distributed as a Python CLI that runs in its own
isolated environment. The recommended installer is **pipx** — it puts
each Python tool in a separate venv and exposes the entry point on your
PATH, so you don't need to think about Python versions or dependency
conflicts.

### Step 1 — Install pipx (once per machine)

#### macOS

```bash
brew install pipx
pipx ensurepath
```

#### Windows

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
# Close and reopen your terminal so the new PATH takes effect.
pipx --version    # should print the version
```

> **Windows tip:** if `pipx` is still "not recognized" after reopening
> the shell, use `py -m pipx ...` for everything below (it works
> identically). The bare `pipx` command becomes available once
> `%APPDATA%\Python\Python3xx\Scripts` is on your `Path`.
>
> **OneDrive / cloud-synced home directory:** if your `C:\Users\<you>\`
> is synced by OneDrive, pipx may fail with `WinError 448 — untrusted
> mount point`. Move pipx out of the synced tree by setting these
> environment variables (User scope) and reopening the shell:
>
> ```powershell
> [Environment]::SetEnvironmentVariable('PIPX_HOME',    'C:\pipx\home', 'User')
> [Environment]::SetEnvironmentVariable('PIPX_BIN_DIR', 'C:\pipx\bin',  'User')
> ```

### Step 2 — Install hapbeat-helper

Once pipx is on your PATH:

```bash
pipx install hapbeat-helper
```

That's it. `hapbeat-helper` will be available in any new terminal.

### Local development (from a clone of this repo)

```bash
# editable install via pipx (changes in src/ are picked up live)
pipx install -e .

# or — preferred during active dev — a plain venv:
python -m venv .venv
# macOS:
.venv/bin/pip install -e ".[dev]"
.venv/bin/hapbeat-helper start
# Windows:
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\hapbeat-helper start
```

### Updating

```bash
pipx upgrade hapbeat-helper
```

If you installed editable from a clone (`pipx install -e .` or
`pip install -e ".[dev]"`), updates are automatic — just `git pull` and
restart the daemon. The Python package picks up changes in `src/` on
the next process start.

> **WS protocol mismatch?** When Studio reports
> `ERROR: unknown type: <message>` in the log drawer, your helper is
> older than the Studio build. `git pull && restart` (or `pipx upgrade`).

#### Update notices

When a newer release exists on PyPI, `hapbeat-helper start` prints a single
line about it, and `hapbeat-helper version` always reports it. It is one
scrolling line with nothing to dismiss, so it simply prints on every start
rather than being suppressed after the first sighting.

The lookup reads a static release feed
(`https://devtools.hapbeat.com/releases.json`), times out after 3 seconds and
stays completely silent on failure, so it is a no-op on isolated networks.
Disable it with `--no-update-check` or `HAPBEAT_NO_UPDATE_CHECK=1`.

### Uninstalling

```bash
pipx uninstall hapbeat-helper
```

## Run

### Option A — Auto-start service (recommended)

Register hapbeat-helper as an OS-level service so it starts automatically
every time you log in. After this one-time setup you never need to open a
terminal again:

```bash
hapbeat-helper install-service
```

To check the registration state:

```bash
hapbeat-helper service-status
```

To remove the auto-start registration:

```bash
hapbeat-helper uninstall-service
```

Platform notes:
- **macOS** — creates `~/Library/LaunchAgents/com.hapbeat.helper.plist` (launchd)
- **Windows** — drops a hidden VBS shim into the Startup folder (`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\hapbeat-helper.vbs`) that launches the daemon at login with no console window. stdout/stderr → `%LOCALAPPDATA%\hapbeat-helper\hapbeat-helper.log`

### Option B — Foreground (dev / debug)

```bash
hapbeat-helper start
```

Then open https://devtools.hapbeat.com — Studio will connect automatically.

Press `Ctrl+C` to stop.

### Other commands

```bash
hapbeat-helper status      # check whether a daemon is reachable on 7703
hapbeat-helper version     # print version (and any newer release)
hapbeat-helper config show # show config path
```

### Firmware OTA from the CLI

Push a firmware app image to one device over Wi-Fi, without opening Studio:

```bash
hapbeat-helper ota 192.168.0.48 dist/necklace_v3/firmware_app_ota.bin
hapbeat-helper ota duo-01 dist/necklace_v3/firmware_app_ota.bin   # by device name
```

The target is an IP address or a device name; names are resolved through the
running daemon, so an IP is required when no daemon is up. Pass the app-only
image (`firmware_app_ota.bin`) — a merged serial image is rejected before
anything is sent. Exit code is 0 on success, 1 on OTA failure, 2 on a bad
argument or an unresolvable target.

## AI agent integration (MCP)

`hapbeat-helper mcp` is a local [MCP](https://modelcontextprotocol.io/) server
(stdio) that lets an AI agent on your PC — Claude Code, Codex — drive Studio's
AI trials directly: read the guide and knowledge base, submit a trial, open it
for you to try, and wait for your rating. It is a thin relay: every tool call
goes through the running daemon to the Studio tab, and Studio does the work
(validation, rendering, knowledge-base writes). Only you rate candidates; the
agent never writes ratings. File-based exchange (`inbox/`) keeps working.

```
Claude Code / Codex ──stdio──▶ hapbeat-helper mcp
                                  │ ws://127.0.0.1:7703
                                  ▼
                            hapbeat-helper daemon ──relay──▶ Studio tab (Waveform editor, folder open)
```

### Install

The MCP SDK is an optional extra:

```bash
pipx install --force "hapbeat-helper[mcp]"
# from a clone of this repo:
pipx install --force --editable ".[mcp]"
```

Without the extra, `hapbeat-helper mcp` prints these commands and exits with
code 2.

### Register with your agent

Claude Code:

```bash
claude mcp add hapbeat -s user -- hapbeat-helper mcp
```

Codex — add to `~/.codex/config.toml`:

```toml
[mcp_servers.hapbeat]
command = "hapbeat-helper"
args = ["mcp"]
```

Add `--port N` to the args if your daemon is not on 7703.

### Prerequisites while using it

1. The daemon is running (`hapbeat-helper start` or `install-service`).
2. Hapbeat Studio is open with the **Waveform editor** showing a folder. That
   tab registers itself as the agent endpoint; if several tabs qualify, the one
   that registered last receives the requests.

Otherwise tools fail with `HELPER_NOT_RUNNING: ...` (no daemon) or
`STUDIO_NOT_READY: ...` (no Studio tab ready). Requests time out after 30 s
(`submit_trial`: 120 s) with `TIMEOUT: ...`.

### Tools

| Tool | Arguments | Purpose |
|---|---|---|
| `status` | — | Studio version, open folder, clip / trial / unrated counts, dimensions |
| `get_guide` | — | Agent guide (read first): trial format and workflow |
| `get_catalog` | — | Existing clips in the folder |
| `get_knowledge` | `term?` | Term index + dimensions + insights, or one term's document |
| `submit_trial` | `trial` (`hapbeat-trial@1` object) | Validate, render and store a trial; returns `trialId` and per-candidate features |
| `get_trial` | `trialId` | Request, candidates and rating (`null` while unrated) |
| `list_trials` | `limit?` (1–100, default 20), `unratedOnly?` | Recent trials |
| `audition` | `trialId`, `candidateId`, `play?` | Open the trial in the editor's AI-trials tab; `play: true` also starts playback |
| `adopt` | `trialId`, `candidateId` | Adopt a candidate as a clip (same as the Adopt button) |
| `propose_insight` | `statement` (≤ 1000 chars), `evidence` (1–20 × `trialId/candidateId`) | Append to the Proposed section of `insights.md` |
| `wait_for_rating` | `trialId`, `timeoutSec?` (10–1800, default 300) | Poll every 2 s; `{status: "rated", rating}` or `{status: "waiting"}` (call again) |

### Relay messages

Same `{"type": ..., "payload": {...}}` envelope as the rest of the WS API.
These messages are routed only between the MCP client and the endpoint tab —
never broadcast, never passed to the device handlers.

| type | Direction | payload | Helper behaviour |
|---|---|---|---|
| `agent_endpoint_register` | Studio → helper | `{ studioVersion, folderName }` | Make the sender the agent endpoint (last wins); reply `agent_endpoint_registered` `{}` |
| `agent_endpoint_unregister` | Studio → helper | `{}` | Clear the endpoint if the sender is it |
| `agent_request` | MCP → helper → Studio | `{ requestId, method, params }` | Forward unchanged to the endpoint and remember the requester. No endpoint → `agent_response { requestId, ok: false, error: "STUDIO_NOT_READY: ..." }` |
| `agent_response` | Studio → helper → MCP | `{ requestId, ok, result?, error? }` | Forward to the requester and forget the request. Accepted only from the tab the request went to; dropped if the requester is gone |

- Endpoint disconnects → every request still pending on it is answered with
  `ok: false, error: "STUDIO_DISCONNECTED"`. Requester disconnects → its
  pending requests are dropped.
- `requestId` must match `^[A-Za-z0-9_-]{1,64}$` (otherwise an `error`
  message is returned and nothing is forwarded). A duplicate pending id is
  answered with `DUPLICATE_REQUEST_ID`; a payload over 4 MB (JSON, UTF-8) with
  `PAYLOAD_TOO_LARGE`.

## Verify

Quick smoke test using `websocat`:

```bash
echo '{"type":"ping","payload":{}}' | websocat ws://localhost:7703
echo '{"type":"list_devices","payload":{}}' | websocat ws://localhost:7703
```

## Troubleshooting

- **Studio reports "Helper 未接続"** — run `hapbeat-helper install-service` (once)
  or start manually with `hapbeat-helper start`.
- **Browser cannot connect to `ws://localhost:7703` from HTTPS Studio** —
  Chrome and Edge allow this by default. Firefox requires
  `network.websocket.allowInsecureFromHTTPS = true` in `about:config`.
- **No devices found** — confirm the Hapbeat devices and this PC are on the
  same Wi-Fi network. Some hotspot/AP modes block UDP broadcast and mDNS.
- **Port 7700 / 7703 already in use** — stop any running `hapbeat-manager`
  (it owns the same ports). The two cannot run at the same time.
- **Windows: `pipx install` fails with `WinError 448 — untrusted mount
  point`** — your home directory is under OneDrive (or another reparse
  point). pipx finished installing the package but cannot finalize the
  shim under `~/.local/bin/`. Either run `hapbeat-helper.exe` from that
  path directly, or relocate pipx outside the synced tree:

  ```powershell
  [Environment]::SetEnvironmentVariable('PIPX_HOME',    'C:\pipx\home', 'User')
  [Environment]::SetEnvironmentVariable('PIPX_BIN_DIR', 'C:\pipx\bin',  'User')
  # Reopen the shell, then:
  py -m pipx ensurepath
  py -m pipx install hapbeat-helper
  ```
- **`pipx` not recognized after `pip install --user pipx`** — the user
  Scripts dir is not on `Path` yet. Run `py -m pipx ensurepath` and open
  a new terminal. As a fallback, every `pipx X` call also works as
  `py -m pipx X`.

## ドキュメント

公式ドキュメントは [https://devtools.hapbeat.com/docs/helper/](https://devtools.hapbeat.com/docs/helper/) を参照してください。
インストール手順 / CLI リファレンス / セキュリティ解説などをまとめています。

## License

MIT
