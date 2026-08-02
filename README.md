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
line about it, and `hapbeat-helper version` always reports it. **The same
version is only announced once** — pinning a version while you work never
means dismissing the same message on every start.

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
