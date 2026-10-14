"""Command-line entry for ``hapbeat-helper``.

Subcommands:

- ``start [--port 7703]``                 start the daemon (foreground)
- ``status``                              probe ws://localhost:7703
- ``version``                             print version
- ``stop``                                stop the auto-started helper
- ``logs [-f] [-n N]``                    show log file + tail recent lines
- ``ota <target> <bin>``                  push a firmware app image over Wi-Fi
- ``mcp [--port 7703]``                   MCP server (stdio) for AI agents, relayed via the daemon
- ``materials <ingest|derive|list|show|set-license|credits|where>``  material ledger (sound sources + licenses)
- ``scene build --session <session.json>``  recording -> Saved/HapticViewer/ for Studio's Scene tab
- ``cues validate <cue-table.json>``      check a cue table (schema + cue-table spec section 6)
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


def _raise_priority_if_lowered() -> None:
    """Run at normal CPU priority even when started below it.

    Windows starts Task Scheduler tasks below normal priority unless the task
    says otherwise, and the auto-start task registered before 0.5.1 did not. A
    below-normal daemon on a busy PC goes unscheduled for seconds at a time: the
    event loop stalls with no CPU use of its own, PONGs go unanswered, devices
    drop offline and stream packets arrive late or not at all (2026-10-09 log:
    stalls up to 23.9 s while the helper itself was idle).
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        # Without these, the -1 pseudo-handle goes out as a 32-bit int and
        # every call fails on 64-bit Python.
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetPriorityClass.argtypes = [wintypes.HANDLE]
        kernel32.GetPriorityClass.restype = wintypes.DWORD
        kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetPriorityClass.restype = wintypes.BOOL
        normal = 0x00000020  # NORMAL_PRIORITY_CLASS
        lowered = (0x00004000, 0x00000040)  # BELOW_NORMAL / IDLE
        handle = kernel32.GetCurrentProcess()
        if kernel32.GetPriorityClass(handle) in lowered and kernel32.SetPriorityClass(handle, normal):
            logger.info("raised process priority to normal (it was started below normal)")
    except Exception:  # noqa: BLE001 — never block startup on this
        pass


def _cmd_start(args: argparse.Namespace) -> int:
    _setup_logging(args.verbose)
    _apply_update_check_flag(args)
    _raise_priority_if_lowered()
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


def _is_http_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def _cmd_materials_ingest(args: argparse.Namespace) -> int:
    from hapbeat_helper import materials

    store = _material_store()
    given = any(v is not None for v in (args.url, args.page, args.original_name, args.note))
    if args.files:
        if args.dir or args.since is not None:
            print("error: FILES cannot be combined with --dir / --since", file=sys.stderr)
            return 2
        if args.original_name is not None and len(args.files) != 1:
            print("error: --original-name needs exactly one FILE", file=sys.stderr)
            return 2
        for flag, value in (("--url", args.url), ("--page", args.page)):
            if value is not None and not _is_http_url(value):
                print(f"error: {flag} must be an http(s) URL: {value}", file=sys.stderr)
                return 2
        paths = [Path(f).expanduser() for f in args.files]
        for path in paths:
            ext = path.suffix.lower()
            if not path.is_file() or (
                ext not in materials.AUDIO_EXTS and ext not in materials.ARCHIVE_EXTS
            ):
                print(f"error: not an audio or zip file: {path}", file=sys.stderr)
                return 2
        source = materials.IngestSource(
            url=args.url, page=args.page, original_name=args.original_name, note=args.note,
        )
        result = store.ingest_paths(paths, dry_run=args.dry_run, source=source)
        label = paths[0].name if len(paths) == 1 else f"{len(paths)} files"
    else:
        if given:
            print(
                "error: --url / --page / --original-name / --note need FILES",
                file=sys.stderr,
            )
            return 2
        directory = Path(args.dir).expanduser() if args.dir else materials.default_downloads_dir()
        if not directory.is_dir():
            print(f"error: not a directory: {directory}", file=sys.stderr)
            return 2
        since_ts = None if args.since is None else time.time() - args.since * 86400
        result = store.ingest_dir(directory, since_ts=since_ts, dry_run=args.dry_run)
        label = str(directory)
    prefix = "(dry run) " if args.dry_run else ""
    print(
        f"{prefix}{label}: new {len(result.new)} / duplicate "
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


def _sha_of(target: str) -> str | None:
    """sha256 given directly, or of the file at *target* (None when neither)."""
    from hapbeat_helper import materials

    if materials.is_sha256(target.lower()):
        return target.lower()
    path = Path(target).expanduser()
    if not path.is_file():
        return None
    return materials.sha256_file(path)


def _cmd_materials_derive(args: argparse.Namespace) -> int:
    path = Path(args.file).expanduser()
    if not path.is_file():
        print(f"error: not a file: {args.file}", file=sys.stderr)
        return 2
    store = _material_store()
    sha = _sha_of(str(path))
    parents: list[str] = []
    for raw in args.parents:
        parent = _sha_of(raw)
        if parent is None:
            print(f"error: --from is not a file or sha256: {raw}", file=sys.stderr)
            return 2
        if parent == sha:
            print(f"error: --from is the file itself: {raw}", file=sys.stderr)
            return 2
        # A parent the ledger does not know would leave the lineage dangling.
        if store.resolve(parent)["kind"] == "unknown":
            print(
                f"error: --from is not in the ledger (ingest or derive it first): {raw}",
                file=sys.stderr,
            )
            return 2
        if parent not in parents:
            parents.append(parent)
    added = store.register_derived(sha, parents, args.tool, name=path.name, note=args.note)
    state = "derived" if added else "already registered"
    print(f"{state}: {path.name} ({sha[:12]}) <- " + ", ".join(p[:12] for p in parents))
    return 0


def _cmd_materials_list(args: argparse.Namespace) -> int:
    entries = _material_store().list_materials(
        needs_review=args.needs_review, site=args.site,
    )
    for e in entries:
        print(f"{e['storePath']}  {_license_brief(e)}")
    print(f"({len(entries)} materials)")
    return 0


def _cmd_materials_show(args: argparse.Namespace) -> int:
    target = args.target
    sha = _sha_of(target)
    if sha is None:
        print(f"error: not a file or sha256: {target}", file=sys.stderr)
        return 2
    store = _material_store()
    result = store.resolve(sha)
    out = {"sha256": sha, **result}
    if result["kind"] == "derived":
        # How this file was made (its own records; the originals are resolved above).
        out["derivedFrom"] = [
            {k: rec.get(k) for k in ("parents", "tool", "name", "note", "createdAt")}
            for rec in store.derivations(sha)
        ]
    print(json.dumps(out, ensure_ascii=False, indent=2))
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
        "redistributionNote": args.redistribution_note,
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


# ── scene / cues ─────────────────────────────────────────────


def _cmd_scene_build(args: argparse.Namespace) -> int:
    """Exit codes: 0 built / 1 bad session, missing input or ffmpeg failure / 2 bad argument."""
    from hapbeat_helper import scene_build

    session = Path(args.session).expanduser()
    root = Path(args.project_root).expanduser() if args.project_root else Path.cwd()
    if not session.is_file():
        print(f"error: not a file: {args.session}", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2
    try:
        result = scene_build.build(session, root, ffmpeg=args.ffmpeg, keep_frames=args.keep_frames)
    except scene_build.SceneBuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(f"built {result.out_dir}: {result.frames} frames, {result.events} events")
    return 0


def _cmd_cues_validate(args: argparse.Namespace) -> int:
    """Exit codes: 0 valid / 1 problems found / 2 unreadable input."""
    from hapbeat_helper import cue_table

    path = Path(args.table).expanduser()
    try:
        table = cue_table.load_json(path)
    except (OSError, ValueError) as exc:
        print(f"error: cannot read {args.table}: {exc}", file=sys.stderr)
        return 2
    root = Path(args.project_root).expanduser() if args.project_root else Path.cwd()
    lib_path = None
    if args.viewer_lib:
        lib_path = Path(args.viewer_lib).expanduser()
    elif args.project_root:
        candidate = root / "Saved" / "HapticViewer" / "viewer-lib.json"
        if candidate.is_file():
            lib_path = candidate
    clips_dir = sounds_dir = None
    if lib_path is not None:
        try:
            paths = cue_table.load_json(lib_path).get("paths")
            clips_dir, sounds_dir = root / paths["clips"], root / paths["sounds"]
        except (OSError, ValueError, AttributeError, KeyError, TypeError) as exc:
            print(f"error: cannot read paths.clips / paths.sounds from {lib_path}: {exc}", file=sys.stderr)
            return 2
    else:
        print("note: WAV checks skipped (no --viewer-lib, no viewer-lib.json under --project-root)")
    problems = cue_table.validate_cue_table(table, clips_dir, sounds_dir)
    for problem in problems:
        print(problem)
    if problems:
        return 1
    cues = table.get("cues", {}) if isinstance(table, dict) else {}
    print(f"OK ({len(cues)} cues)")
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
        "ingest", help="copy new audio / zip downloads (or the given FILES) into the ledger",
        description=(
            "Without FILES, scan a folder and read the source from the browser's "
            "download mark (Zone.Identifier). With FILES (audio or zip), register "
            "exactly those; --url / --page / --original-name / --note give the "
            "source for files fetched without a browser (curl, scripts) and take "
            "precedence over the download mark. The site is the domain of --page "
            "(else --url)."
        ),
    )
    p_ingest.add_argument(
        "files", nargs="*", metavar="FILES", help="audio / zip files to register",
    )
    p_ingest.add_argument(
        "--dir", help="folder to scan (default: ~/Downloads, top level only)",
    )
    p_ingest.add_argument("--url", help="download URL of FILES (recorded as hostUrl)")
    p_ingest.add_argument(
        "--page", help="material page URL of FILES (recorded as referrerUrl; decides the site)",
    )
    p_ingest.add_argument(
        "--original-name", metavar="NAME",
        help="file name / title at the source (one FILE only; the store keeps the local name)",
    )
    p_ingest.add_argument("--note", metavar="TEXT", help="free text kept with each record")
    p_ingest.add_argument(
        "--since", type=float, metavar="DAYS",
        help="only files modified in the last DAYS days "
             "(default: since the last ingest; first run 30)",
    )
    p_ingest.add_argument(
        "--dry-run", action="store_true", help="show what would be ingested; write nothing",
    )
    p_ingest.set_defaults(func=_cmd_materials_ingest)

    p_derive = sub_mat.add_parser(
        "derive",
        help="record that FILE was made from other files (cut, normalized, mixed...)",
        description=(
            "Register FILE as derived from each --from (a file or sha256 already in "
            "the ledger). The same FILE + parents is not recorded twice."
        ),
    )
    p_derive.add_argument("file", metavar="FILE")
    p_derive.add_argument(
        "--from", dest="parents", action="append", required=True, metavar="FILE_OR_SHA",
        help="source file or sha256 (repeat for several)",
    )
    p_derive.add_argument(
        "--tool", required=True, help="what made it, e.g. 'ffmpeg+numpy (agent)'",
    )
    p_derive.add_argument("--note", metavar="TEXT", help="what was done")
    p_derive.set_defaults(func=_cmd_materials_derive)

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
        "--redistribution-note", metavar="TEXT",
        help="limits on redistribution, shown in CREDITS.md",
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

    p_scene = sub.add_parser(
        "scene",
        help="Scene recordings for Hapbeat Studio's Scene tab (try: scene build)",
    )
    sub_scene = p_scene.add_subparsers(dest="scene_cmd")
    p_sbuild = sub_scene.add_parser(
        "build",
        help="session.json + screen -> <project>/Saved/HapticViewer/",
        description=(
            "Read a recording's session.json (hapbeat-contracts "
            "specs/scene-recording.md section 2), check it, and write "
            "full_replay.mp4, viewer-data.json and viewer-lib.json to "
            "<project-root>/Saved/HapticViewer/. Other files there are kept. "
            "Paths in session.json are relative to the project root. "
            "The frames folder is deleted after a successful build unless "
            "--keep-frames."
        ),
    )
    p_sbuild.add_argument("--session", required=True, help="the recording's session.json")
    p_sbuild.add_argument(
        "--project-root", metavar="DIR", help="game project root (default: the current directory)",
    )
    p_sbuild.add_argument(
        "--ffmpeg", metavar="PATH", help="ffmpeg executable with libx264 (default: ffmpeg on PATH)",
    )
    p_sbuild.add_argument(
        "--keep-frames", action="store_true", help="keep the PNG frames folder after the build",
    )
    p_sbuild.set_defaults(func=_cmd_scene_build)

    p_cues = sub.add_parser("cues", help="cue table tools (try: cues validate)")
    sub_cues = p_cues.add_subparsers(dest="cues_cmd")
    p_cvalidate = sub_cues.add_parser(
        "validate",
        help="check a cue table (schema + hapbeat-contracts specs/cue-table.md section 6)",
        description=(
            "Print each problem on its own line (exit 1), or 'OK (<n> cues)' (exit 0). "
            "The WAV checks run only when the clip / sound folders are known: "
            "from --viewer-lib, or <project-root>/Saved/HapticViewer/viewer-lib.json. "
            "Its paths are relative to the project root (default: the current directory)."
        ),
    )
    p_cvalidate.add_argument("table", metavar="CUE_TABLE", help="the cue table JSON")
    p_cvalidate.add_argument("--project-root", metavar="DIR", help="game project root")
    p_cvalidate.add_argument(
        "--viewer-lib", metavar="PATH", help="viewer-lib.json whose paths.clips / paths.sounds hold the WAVs",
    )
    p_cvalidate.set_defaults(func=_cmd_cues_validate)

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
