"""Command-line entry for ``hapbeat-helper``.

Subcommands:

- ``start [--port 7703]``                 start the daemon (foreground)
- ``status``                              probe ws://localhost:7703
- ``version``                             print version
- ``stop``                                stop the auto-started helper
- ``logs [-f] [-n N]``                    show log file + tail recent lines
- ``install-service``                     register as OS auto-start service (Task Scheduler on Windows / launchd on macOS)
- ``uninstall-service``                   remove the OS service registration
- ``service-status``                      show OS service registration state
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
    try:
        from hapbeat_helper.service import get_service_manager
        mgr = get_service_manager()
        if hasattr(mgr, "stop"):
            mgr.stop()
            return 0
    except NotImplementedError:
        pass
    print(
        "stop: no auto-started instance found. "
        "If you launched in foreground, press Ctrl+C there.",
        file=sys.stderr,
    )
    return 1


def _cmd_logs(args: argparse.Namespace) -> int:
    """Print the log file path and tail recent lines.

    With --follow / -f, stream new lines until Ctrl+C.
    """
    try:
        from hapbeat_helper.service import get_service_manager
        mgr = get_service_manager()
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not hasattr(mgr, "log_path"):
        print(
            "logs: this platform does not redirect stdout to a file. "
            "Run `hapbeat-helper start` to see logs in the "
            "current terminal.",
            file=sys.stderr,
        )
        return 1
    log = mgr.log_path()
    print(f"# {log}")
    if not log.exists():
        print(
            "(log file does not exist yet — has the service been started?)",
            file=sys.stderr,
        )
        return 1

    # Print last N lines.
    try:
        with log.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in lines[-args.lines:]:
            sys.stdout.write(line)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not args.follow:
        return 0

    # Tail mode (cross-platform): poll for new bytes.
    import time
    sys.stdout.flush()
    try:
        with log.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)  # to EOF
            while True:
                chunk = f.read()
                if chunk:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        return 0


def _cmd_install_service(_args: argparse.Namespace) -> int:
    try:
        from hapbeat_helper.service import get_service_manager
        mgr = get_service_manager()
        mgr.install()
        return 0
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_uninstall_service(_args: argparse.Namespace) -> int:
    try:
        from hapbeat_helper.service import get_service_manager
        mgr = get_service_manager()
        mgr.uninstall()
        return 0
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_service_status(_args: argparse.Namespace) -> int:
    try:
        from hapbeat_helper.service import get_service_manager
        mgr = get_service_manager()
        state = mgr.status()
        labels = {
            "not_registered": "not registered",
            "stopped": "registered, stopped",
            "running": "registered, running",
        }
        print(f"hapbeat-helper service: {labels.get(state, state)}")
        return 0 if state == "running" else 1
    except NotImplementedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_config_show(_args: argparse.Namespace) -> int:
    cfg = _config_dir()
    print(f"config dir: {cfg}")
    cfg_file = cfg / "config.toml"
    if cfg_file.exists():
        print(cfg_file.read_text(encoding="utf-8"))
    else:
        print("(no config file yet — defaults are in use)")
    return 0


def _add_verbose(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="debug logging",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hapbeat-helper",
        description=(
            "Local daemon that bridges Hapbeat Studio (web) to "
            "Hapbeat devices on the local network."
        ),
        epilog=(
            "commands:\n"
            "  start            start in foreground (Ctrl+C to stop)\n"
            "  stop             stop the running helper\n"
            "                   macOS: unloads launchd job (restarts at next login)\n"
            "                   Windows: kills process (task remains registered)\n"
            "  status           check ws://localhost:7703 reachability\n"
            "  version          show installed version\n"
            "  logs             show/follow the auto-start log file\n"
            "  install-service  register & start at OS login\n"
            "  uninstall-service  remove registration and stop\n"
            "  service-status   show registration state\n"
            "  config show      show config file path and contents\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_verbose(p)
    sub = p.add_subparsers(dest="cmd")

    p_start = sub.add_parser(
        "start",
        help="start the daemon in the foreground (Ctrl+C to stop)",
    )
    _add_verbose(p_start)
    p_start.add_argument(
        "--port", type=int, default=WS_PORT,
        help=f"WebSocket port (default: {WS_PORT})",
    )
    p_start.set_defaults(func=_cmd_start)

    p_status = sub.add_parser("status", help="check whether helper is running")
    _add_verbose(p_status)
    p_status.add_argument("--port", type=int, default=WS_PORT)
    p_status.set_defaults(func=_cmd_status)

    p_version = sub.add_parser("version", help="print version")
    p_version.set_defaults(func=_cmd_version)

    p_stop = sub.add_parser(
        "stop",
        help="stop the running helper (macOS: bootout; Windows: taskkill)",
    )
    p_stop.set_defaults(func=_cmd_stop)

    p_logs = sub.add_parser(
        "logs",
        help="show log file path + tail recent lines (-f to follow)",
    )
    p_logs.add_argument(
        "-n", "--lines", type=int, default=50,
        help="number of lines to show from the end (default: 50)",
    )
    p_logs.add_argument(
        "-f", "--follow", action="store_true",
        help="stream new log lines until Ctrl+C",
    )
    p_logs.set_defaults(func=_cmd_logs)

    p_install = sub.add_parser(
        "install-service",
        help="register hapbeat-helper as an OS auto-start service (launchd on macOS / Startup-folder VBS shim on Windows); starts immediately",
    )
    p_install.set_defaults(func=_cmd_install_service)

    p_uninstall = sub.add_parser(
        "uninstall-service",
        help="remove the OS service registration",
    )
    p_uninstall.set_defaults(func=_cmd_uninstall_service)

    p_svc_status = sub.add_parser(
        "service-status",
        help="show OS service registration state (not_registered / stopped / running)",
    )
    p_svc_status.set_defaults(func=_cmd_service_status)

    p_config = sub.add_parser("config", help="config subcommands (try: config show)")
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
