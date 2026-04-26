"""Command-line entry for ``hapbeat-helper``.

Subcommands:

- ``start [--foreground] [--port 7703]``  start the daemon
- ``status``                              probe ws://localhost:7703
- ``version``                             print version
- ``stop``                                placeholder (foreground only for now)
- ``logs``                                placeholder

OS-level service install (launchd / systemd / Windows Service) is
deferred — the MVP only supports ``start --foreground``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import socket
import sys
from pathlib import Path

from hapbeat_helper import __version__
from hapbeat_helper.server import HelperServer, WS_PORT

logger = logging.getLogger("hapbeat-helper")


def _config_dir() -> Path:
    if sys.platform == "win32":
        import os
        base = Path(os.environ.get("APPDATA", str(Path.home())))
        return base / "hapbeat-helper"
    return Path.home() / ".config" / "hapbeat-helper"


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _cmd_start(args: argparse.Namespace) -> int:
    _setup_logging(args.verbose)
    if not args.foreground:
        print(
            "MVP only supports foreground mode; pass --foreground.",
            file=sys.stderr,
        )
        return 2

    server = HelperServer(port=args.port)
    print(f"hapbeat-helper {__version__} starting on ws://localhost:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\nshutting down…")
        return 0
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """TCP-probe the WebSocket port. Cheap reachability check."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.5)
    try:
        sock.connect(("127.0.0.1", args.port))
        print(f"hapbeat-helper: reachable on ws://localhost:{args.port}")
        return 0
    except OSError:
        print(
            f"hapbeat-helper: not running (no listener on {args.port})",
            file=sys.stderr,
        )
        return 1
    finally:
        sock.close()


def _cmd_version(_args: argparse.Namespace) -> int:
    print(f"hapbeat-helper {__version__}")
    return 0


def _cmd_stop(_args: argparse.Namespace) -> int:
    print(
        "stop: not implemented in MVP — "
        "press Ctrl+C in the foreground terminal.",
        file=sys.stderr,
    )
    return 2


def _cmd_logs(_args: argparse.Namespace) -> int:
    print("logs: not implemented in MVP.", file=sys.stderr)
    return 2


def _cmd_config_show(_args: argparse.Namespace) -> int:
    cfg = _config_dir()
    print(f"config dir: {cfg}")
    cfg_file = cfg / "config.toml"
    if cfg_file.exists():
        print(cfg_file.read_text(encoding="utf-8"))
    else:
        print("(no config file yet — defaults are in use)")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hapbeat-helper",
        description=(
            "Local daemon that bridges Hapbeat Studio (web) to "
            "Hapbeat devices on the local network."
        ),
    )
    p.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="cmd")

    p_start = sub.add_parser("start", help="start the daemon")
    p_start.add_argument(
        "--foreground", action="store_true",
        help="run in the foreground (required for MVP)",
    )
    p_start.add_argument(
        "--port", type=int, default=WS_PORT,
        help=f"WebSocket port (default: {WS_PORT})",
    )
    p_start.set_defaults(func=_cmd_start)

    p_status = sub.add_parser("status", help="check whether helper is running")
    p_status.add_argument("--port", type=int, default=WS_PORT)
    p_status.set_defaults(func=_cmd_status)

    p_version = sub.add_parser("version", help="print version")
    p_version.set_defaults(func=_cmd_version)

    p_stop = sub.add_parser("stop", help="(MVP: not implemented)")
    p_stop.set_defaults(func=_cmd_stop)

    p_logs = sub.add_parser("logs", help="(MVP: not implemented)")
    p_logs.set_defaults(func=_cmd_logs)

    p_config = sub.add_parser("config", help="config helpers")
    sub_cfg = p_config.add_subparsers(dest="config_cmd")
    p_show = sub_cfg.add_parser("show", help="show config path / contents")
    p_show.set_defaults(func=_cmd_config_show)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
