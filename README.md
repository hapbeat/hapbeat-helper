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

```bash
pipx install hapbeat-helper
```

For local development from this repo:

```bash
pipx install -e .
# or, in a venv:
pip install -e ".[dev]"
```

## Run

The MVP only supports foreground mode. Start the daemon in a terminal:

```bash
hapbeat-helper start --foreground
```

Then open https://devtools.hapbeat.com — Studio will connect automatically.

Other commands:

```bash
hapbeat-helper status      # check whether a daemon is reachable on 7703
hapbeat-helper version     # print version
hapbeat-helper config show # show config path
```

`stop` is currently a no-op for the foreground variant; press `Ctrl+C`.
OS-level service installation (launchd / systemd / Windows Service) is
planned for a future release.

## Verify

Quick smoke test using `websocat`:

```bash
echo '{"type":"ping","payload":{}}' | websocat ws://localhost:7703
echo '{"type":"list_devices","payload":{}}' | websocat ws://localhost:7703
```

## Troubleshooting

- **Studio reports "Helper 未接続"** — make sure `hapbeat-helper start --foreground`
  is running on the same machine as the browser.
- **Browser cannot connect to `ws://localhost:7703` from HTTPS Studio** —
  Chrome and Edge allow this by default. Firefox requires
  `network.websocket.allowInsecureFromHTTPS = true` in `about:config`.
- **No devices found** — confirm the Hapbeat devices and this PC are on the
  same Wi-Fi network. Some hotspot/AP modes block UDP broadcast and mDNS.
- **Port 7700 / 7703 already in use** — stop any running `hapbeat-manager`
  (it owns the same ports). The two cannot run at the same time.

## License

MIT
