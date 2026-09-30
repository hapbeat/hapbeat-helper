"""Command-line entry for ``hapbeat-helper``.

Subcommands:

- ``start [--port 7703]``                 start the daemon (foreground)
- ``status``                              probe ws://localhost:7703
- ``version``                             print version
- ``stop``                                stop the auto-started helper
- ``logs [-f] [-n N]``                    show log file + tail recent lines
- ``ota <target> <bin>``                  push a firmware app image over Wi-Fi
- ``mcp [--port 7703]``                   MCP server (stdio) for AI agents, relayed via the daemon
- ``materials <ingest|list|show|set-license|credits|where>``  material ledger (sound sources + licenses)
- ``install-service``                     register as OS auto-start service (Task Scheduler on Windows / launchd on macOS)
- ``uninstall-service``                   remove the OS service registration
- ``service-status``                      show OS service registration state
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
import sys
import threading
import time
from pathlib import Path

from hapbeat_helper import __version__
from hapbeat_helper.server import HelperServer, WS_PORT
from hapbeat_helper import update_check

logger = logging.getLogger("hapbeat-helper")

# 設定・状態ファイルの置き場は update_check と共有する (状態ファイルもここ)。
_config_dir = update_check.config_dir


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _shutdown_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Best-effort, time-BOUNDED teardown on Ctrl+C.

    Graceful asyncio teardown can stall indefinitely on a daemon: zeroconf's
    blocking ``unregister_all_services``, websockets' ``wait_closed()``, or a
    cancelled task parked on a still-blocked ``run_in_executor`` thread can all
    hang. So Ctrl+C must NEVER leave the process wedged.

    Strategy: a watchdog timer force-exits as a hard backstop; the cancel/close
    work is bounded by a short timeout. SIGINT is ignored only DURING this brief
    window (to avoid the Windows ProactorEventLoop ``__del__`` "NoneType has no
    attribute 'close'" noise from a half-closed loop) — the watchdog guarantees
    it can't trap the user.
    """
    # Hard backstop — if anything below stalls, terminate anyway.
    watchdog = threading.Timer(3.0, lambda: os._exit(0))
    watchdog.daemon = True
    watchdog.start()

    prev_handler = None
    try:
        prev_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass  # not the main thread / unsupported — best effort
    try:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            # Bounded: do NOT wait forever for a task stuck on a blocked
            # executor thread — the watchdog/os._exit handles that case.
            loop.run_until_complete(asyncio.wait(pending, timeout=2.0))
        loop.run_until_complete(loop.shutdown_asyncgens())
    except Exception:
        pass  # teardown is best-effort; the close() below is what matters
    finally:
        try:
            loop.close()
        except Exception:
            pass
        if prev_handler is not None:
            try:
                signal.signal(signal.SIGINT, prev_handler)
            except (ValueError, OSError):
                pass
    watchdog.cancel()


def _apply_update_check_flag(args: argparse.Namespace) -> None:
    """``--no-update-check`` を env に写して opt-out を一元化する。"""
    if getattr(args, "no_update_check", False):
        os.environ["HAPBEAT_NO_UPDATE_CHECK"] = "1"


def _cmd_start(args: argparse.Namespace) -> int:
    _setup_logging(args.verbose)
    _apply_update_check_flag(args)
    from hapbeat_helper import materials

    config = materials.load_config()
    # Downloads watcher is opt-in (config materials_watch_downloads = true).
    watch_dir = (
        materials.default_downloads_dir()
        if materials.watch_downloads_enabled(config) else None
    )
    server = HelperServer(
        port=args.port,
        materials_dir=materials.default_materials_dir(config),
        materials_watch_dir=watch_dir,
    )
    print(f"hapbeat-helper {__version__} starting on ws://localhost:{args.port}")
    print("Press Ctrl+C to stop.")
    # 起動をブロックせずに release feed を見に行き、新しい版があれば 1 行だけ
    # 出す (起動ごと。DEC-053 §5.1 B)。オフラインなら黙る。
    update_check.notify_in_background(__version__, stream=sys.stdout)
    # Own the event loop (instead of asyncio.run) so _shutdown_loop can
    # guarantee a complete close() on Ctrl+C — see its docstring for the
    # Windows ProactorEventLoop __del__ noise this prevents.
    rc = 0
    interrupted = False
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.run())
    except KeyboardInterrupt:
        print("\nshutting down…")
        interrupted = True
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        rc = 1
    finally:
        _shutdown_loop(loop)
        asyncio.set_event_loop(None)
    if interrupted:
        # The daemon ran, so blocking-TCP threads may still sit in the default
        # ThreadPoolExecutor whose atexit join would hang a normal exit. Force
        # an immediate exit — everything user-visible is already torn down.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    return rc


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


def _cmd_version(args: argparse.Namespace) -> int:
    _apply_update_check_flag(args)
    print(f"hapbeat-helper {__version__}")
    notice = update_check.pending_notice(__version__)
    if notice:
        print(notice)
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


def _cmd_ota(args: argparse.Namespace) -> int:
    """Push a firmware app image to one device.

    Exit codes: 0 success / 1 OTA failure / 2 bad argument or target.
    """
    _setup_logging(args.verbose)
    if not args.verbose:
        # The one-line progress display is the output here; INFO records from
        # the transfer would break it apart mid-line. -v restores them.
        logging.getLogger().setLevel(logging.WARNING)
    from hapbeat_helper import ota_client

    bin_bytes, err = ota_client.load_ota_image(args.bin)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    if ota_client.daemon_reachable(args.port):
        return asyncio.run(ota_client.run_ota_via_ws(
            args.port, args.target, bin_bytes, args.verbose,
        ))

    # No daemon: we can still stream directly, but device discovery lives in
    # the daemon, so a name cannot be resolved here.
    if not ota_client.is_ip_address(args.target):
        print(
            f"error: helper is not running on port {args.port}, so the device "
            f"name {args.target!r} cannot be resolved. Start it with "
            "`hapbeat-helper start`, or pass the device's IP address.",
            file=sys.stderr,
        )
        return 2
    return ota_client.run_ota_direct(args.target, bin_bytes, args.verbose)


def _cmd_mcp(args: argparse.Namespace) -> int:
    """Serve the MCP tools over stdio. stdout is the protocol channel, so
    everything human-readable here goes to stderr."""
    import importlib.util

    if importlib.util.find_spec("mcp") is None:
        print(
            "error: the MCP extra is not installed. Install it with:\n"
            '  pipx install --force "hapbeat-helper[mcp]"\n'
            '  (from a clone: pipx install --force --editable ".[mcp]")',
            file=sys.stderr,
        )
        return 2
    _setup_logging(args.verbose)  # basicConfig's default stream is stderr
    from hapbeat_helper import mcp_server

    return mcp_server.run(args.port)


def _cmd_config_show(_args: argparse.Namespace) -> int:
    cfg = _config_dir()
    print(f"config dir: {cfg}")
    cfg_file = cfg / "config.toml"
    if cfg_file.exists():
        print(cfg_file.read_text(encoding="utf-8"))
    else:
        print("(no config file yet — defaults are in use)")
    return 0


# ── materials ────────────────────────────────────────────────


def _material_store():
    from hapbeat_helper import materials

    return materials.MaterialStore(materials.default_materials_dir())


def _license_brief(entry: dict) -> str:
    lic = entry["license"]
    flag = "  [要確認]" if entry["needsReview"] else ""
    return f"{entry['site']} · {lic['id']}{flag}"


def _cmd_materials_ingest(args: argparse.Namespace) -> int:
    from hapbeat_helper import materials

    store = _material_store()
    directory = Path(args.dir).expanduser() if args.dir else materials.default_downloads_dir()
    if not directory.is_dir():
        print(f"error: not a directory: {directory}", file=sys.stderr)
        return 2
    since_ts = None if args.since is None else time.time() - args.since * 86400
    result = store.ingest_dir(directory, since_ts=since_ts, dry_run=args.dry_run)
    prefix = "(dry run) " if args.dry_run else ""
    print(
        f"{prefix}{directory}: new {len(result.new)} / duplicate "
        f"{len(result.duplicates)} / needs review {len(result.needs_review)}"
    )
    review = {r["sha256"] for r in result.needs_review}
    for rec in result.new:
        src = rec["originalName"]
        if rec["archive"]:
            src = f"{rec['archive']['originalName']}:{rec['archive']['member']}"
        flag = "  [要確認]" if rec["sha256"] in review else ""
        print(f"  + {src} -> {rec['storePath']}  ({rec['site']}){flag}")
    for dup in result.duplicates:
        src = Path(dup["path"]).name + (f":{dup['member']}" if dup["member"] else "")
        print(f"  = {src} (already {dup['storePath']})")
    for err in result.errors:
        print(f"  ! {err['path']}: {err['error']}", file=sys.stderr)
    if not args.dry_run:
        print(f"materials dir: {store.root}")
    return 1 if result.errors else 0


def _cmd_materials_list(args: argparse.Namespace) -> int:
    entries = _material_store().list_materials(
        needs_review=args.needs_review, site=args.site,
    )
    for e in entries:
        print(f"{e['storePath']}  {_license_brief(e)}")
    print(f"({len(entries)} materials)")
    return 0


def _cmd_materials_show(args: argparse.Namespace) -> int:
    from hapbeat_helper import materials

    target = args.target
    if materials.is_sha256(target.lower()):
        sha = target.lower()
    else:
        path = Path(target).expanduser()
        if not path.is_file():
            print(f"error: not a file or sha256: {target}", file=sys.stderr)
            return 2
        sha = materials.sha256_file(path)
    result = _material_store().resolve(sha)
    print(json.dumps({"sha256": sha, **result}, ensure_ascii=False, indent=2))
    return 0


def _cmd_materials_set_license(args: argparse.Namespace) -> int:
    from hapbeat_helper import materials

    attribution = args.attribution
    if attribution is None:
        attribution = args.license_id.upper().startswith("CC-BY")
    lic = {
        "id": args.license_id,
        "name": args.name or args.license_id,
        "url": args.url,
        "creditText": args.credit,
        "attributionRequired": attribution,
        "verified": args.verified,
    }
    store = _material_store()
    if args.site:
        store.set_site_license(args.site.lower(), lic)
        print(f"sites.json: {args.site.lower()} -> {args.license_id}")
    else:
        sha = args.sha.lower()
        if not materials.is_sha256(sha):
            print(f"error: not a sha256: {args.sha}", file=sys.stderr)
            return 2
        store.set_override(sha, lic)
        print(f"override: {sha} -> {args.license_id}")
    return 0


def _cmd_materials_credits(args: argparse.Namespace) -> int:
    from hapbeat_helper import materials

    items: list[tuple[str, str]] = []
    for raw in args.paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            files = sorted(
                f for f in path.rglob("*")
                if f.is_file() and f.suffix.lower() in materials.AUDIO_EXTS
            )
        elif path.is_file():
            files = [path]
        else:
            print(f"error: no such file or directory: {raw}", file=sys.stderr)
            return 2
        items += [(f.name, materials.sha256_file(f)) for f in files]
    sys.stdout.write(_material_store().credits_markdown(items, "hapbeat-helper"))
    return 0


def _cmd_materials_where(_args: argparse.Namespace) -> int:
    print(_material_store().root)
    return 0


def _add_verbose(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="debug logging",
    )


def _add_no_update_check(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-update-check", action="store_true",
        help="skip the release-feed lookup (same as HAPBEAT_NO_UPDATE_CHECK=1)",
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
            "  ota              push a firmware app image to one device\n"
            "  mcp              MCP server (stdio) for AI agents (needs the [mcp] extra)\n"
            "  materials        material ledger: where sound files came from + licenses\n"
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
    _add_no_update_check(p_start)
    p_start.add_argument(
        "--port", type=int, default=WS_PORT,
        help=f"WebSocket port (default: {WS_PORT})",
    )
    p_start.set_defaults(func=_cmd_start)

    p_status = sub.add_parser("status", help="check whether helper is running")
    _add_verbose(p_status)
    p_status.add_argument("--port", type=int, default=WS_PORT)
    p_status.set_defaults(func=_cmd_status)

    p_version = sub.add_parser("version", help="print version (and any newer release)")
    _add_no_update_check(p_version)
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

    p_ota = sub.add_parser(
        "ota",
        help="push a firmware app image (.bin) to one device over Wi-Fi",
        description=(
            "Push a firmware app image to a single device. TARGET is an IP "
            "address or a device name (names need a running helper to "
            "resolve). BIN must be an app-only image such as "
            "firmware_app_ota.bin; a merged serial image is rejected."
        ),
    )
    _add_verbose(p_ota)
    p_ota.add_argument("target", help="device IP address or device name")
    p_ota.add_argument("bin", help="path to the OTA app image (.bin)")
    p_ota.add_argument(
        "--port", type=int, default=WS_PORT,
        help=f"WebSocket port of the running helper (default: {WS_PORT})",
    )
    p_ota.set_defaults(func=_cmd_ota)

    p_mcp = sub.add_parser(
        "mcp",
        help="run the MCP server (stdio) that lets AI agents drive Studio's AI trials",
        description=(
            "Serve MCP tools over stdio for a local AI agent (Claude Code, "
            "Codex). Requests are relayed through the running helper daemon to "
            "the Hapbeat Studio tab that has the Waveform editor open on a "
            "folder. Register it with your agent rather than running it by hand."
        ),
    )
    _add_verbose(p_mcp)
    p_mcp.add_argument(
        "--port", type=int, default=WS_PORT,
        help=f"WebSocket port of the running helper (default: {WS_PORT})",
    )
    p_mcp.set_defaults(func=_cmd_mcp)

    p_mat = sub.add_parser(
        "materials",
        help="material ledger: record where downloaded sounds came from and their licenses",
        description=(
            "Keep a ledger of downloaded sound files (source site, page URL, "
            "license), linked by SHA-256 so renames do not lose it. Files are "
            "copied into the materials dir; the originals stay where they are."
        ),
    )
    sub_mat = p_mat.add_subparsers(dest="materials_cmd")

    p_ingest = sub_mat.add_parser(
        "ingest", help="copy new audio / zip downloads into the ledger",
    )
    p_ingest.add_argument(
        "--dir", help="folder to scan (default: ~/Downloads, top level only)",
    )
    p_ingest.add_argument(
        "--since", type=float, metavar="DAYS",
        help="only files modified in the last DAYS days "
             "(default: since the last ingest; first run 30)",
    )
    p_ingest.add_argument(
        "--dry-run", action="store_true", help="show what would be ingested; write nothing",
    )
    p_ingest.set_defaults(func=_cmd_materials_ingest)

    p_list = sub_mat.add_parser("list", help="list ingested materials")
    p_list.add_argument("--needs-review", action="store_true", help="only those needing review")
    p_list.add_argument("--site", metavar="DOMAIN", help="only this site")
    p_list.set_defaults(func=_cmd_materials_list)

    p_mshow = sub_mat.add_parser("show", help="resolve the source of a file or sha256")
    p_mshow.add_argument("target", metavar="FILE_OR_SHA")
    p_mshow.set_defaults(func=_cmd_materials_show)

    p_setlic = sub_mat.add_parser(
        "set-license",
        help="set a site's license rule (sites.json) or one file's license (override)",
    )
    which = p_setlic.add_mutually_exclusive_group(required=True)
    which.add_argument("--site", metavar="DOMAIN")
    which.add_argument("--sha", metavar="SHA256")
    p_setlic.add_argument(
        "--license-id", required=True, metavar="ID",
        help="e.g. CC-BY-4.0 / CC0-1.0 / site-terms / unknown",
    )
    p_setlic.add_argument("--name", help="license name (default: the id)")
    p_setlic.add_argument("--url", help="terms URL")
    p_setlic.add_argument("--credit", metavar="TEXT", help="credit text, e.g. OtoLogic")
    p_setlic.add_argument(
        "--attribution", action=argparse.BooleanOptionalAction, default=None,
        help="attribution required (default: yes for CC-BY*, no otherwise)",
    )
    p_setlic.add_argument(
        "--verified", action="store_true", help="checked against the site's own terms",
    )
    p_setlic.set_defaults(func=_cmd_materials_set_license)

    p_credits = sub_mat.add_parser(
        "credits", help="print CREDITS.md for audio files / folders",
    )
    p_credits.add_argument("paths", nargs="+", metavar="PATH")
    p_credits.set_defaults(func=_cmd_materials_credits)

    p_where = sub_mat.add_parser("where", help="print the materials dir")
    p_where.set_defaults(func=_cmd_materials_where)

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


def _make_console_lossy() -> None:
    """Never let an unencodable character kill a command.

    A Japanese Windows console is cp932, which has no U+2014 EM DASH. Both
    our own help text and the messages the daemon/firmware hand back (e.g.
    ``phase=stuck: ... — recovered``) contain one, and printing it raised
    UnicodeEncodeError, taking down the whole command. Degrade the
    character instead of the command.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # not a reconfigurable text stream (redirect / capture)


def main(argv: list[str] | None = None) -> int:
    _make_console_lossy()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
