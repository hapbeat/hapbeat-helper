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
- ``scene session [--config hapbeat-scene.json]``  recording log -> session.json (optionally --build)
- ``scene check --session <session.json>``  a recording against the cue table
- ``scene guide [--engine E]`` / ``scene template <engine> --out DIR``  AI-agent guide / log writers
- ``cues validate <cue-table.json>``      check a cue table (schema + cue-table spec section 6)
- ``cues scan --unity <project>``         Unity Event Maps + triggers -> event inventory
- ``cues init --inventory <file> --out <table>``  first-draft cue table (cue-table spec section 10)
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


def _scene_report(
    args: argparse.Namespace, code: int, errors: list[str], warnings: list[str],
    check=None, events: int = 0, table=None, extra: dict | None = None, done: str = "",
) -> int:
    """Print a scene session / check result and return ``code``.

    Text: ``error:`` / ``warning:`` lines on stderr and one summary line on stdout (none for exit 2).
    ``--json``: one object ``{ok, errors, warnings, coverage, takes, ...extra}`` on stdout and nothing else.
    """
    from hapbeat_helper import scene_check

    coverage = check.coverage if check is not None else {}
    takes = check.takes if check is not None else []
    if args.json:
        out = {"ok": code == 0, "errors": errors, "warnings": warnings, "coverage": coverage, "takes": takes}
        out.update(extra or {})
        print(json.dumps(out, ensure_ascii=False))
        return code
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    if code == 2:
        return code
    if code == 0:
        if table is None:
            print(f"OK: {events} events, no cue table yet{done}")
        else:
            fired, total = scene_check.cue_counts(coverage, table)
            print(f"OK: {events} events, {fired}/{total} cues fired{done}")
    else:
        print(f"FAILED: {len(errors)} error(s)")
    return code


def _load_cue_table(path: Path, lib: dict, root: Path) -> tuple[object, list[str]]:
    """``(table, problems)``, the WAVs checked in the lib's clip / sound folders under ``root``;
    raises OSError / ValueError when the file cannot be read."""
    from hapbeat_helper import cue_table

    table = cue_table.load_json(path)
    paths = lib["paths"]
    problems = cue_table.validate_cue_table(table, root / paths["clips"], root / paths["sounds"])
    return table, [f"cue table {path}: {p}" for p in problems]


def _cmd_scene_session(args: argparse.Namespace) -> int:
    """Exit codes: 0 written (and built) / 1 problems in the recording or the build / 2 bad argument or unreadable input."""
    from hapbeat_helper import scene_build, scene_check, scene_log, scene_project, scene_session

    def fail(message: str) -> int:
        return _scene_report(args, 2, [message], [])

    if not args.build and (args.ffmpeg or args.keep_frames):
        return fail("--ffmpeg and --keep-frames need --build")
    config = Path(args.config).expanduser() if args.config else Path.cwd() / scene_project.CONFIG_NAME
    try:
        cfg = scene_project.load_project(config)
    except scene_project.SceneProjectError as exc:
        return fail(str(exc))
    root = config.parent
    if args.lib_only:
        viewer = root / scene_build.VIEWER_DIR
        viewer.mkdir(parents=True, exist_ok=True)
        scene_build.write_json(viewer / "viewer-lib.json", cfg["lib"])
        if args.json:
            print(json.dumps({"ok": True, "errors": [], "warnings": [], "viewer_lib": str(viewer / "viewer-lib.json")}))
        else:
            print(f"wrote {viewer / 'viewer-lib.json'}")
        return 0
    log_path = Path(args.log).expanduser() if args.log else root / cfg.get("log", scene_project.DEFAULT_LOG)
    out = Path(args.out).expanduser() if args.out else root / "Saved" / "HapticRecord" / "session.json"
    table_path = Path(args.cues).expanduser() if args.cues else root / cfg["lib"]["paths"]["cues"]
    try:
        # No cue table yet (before Studio makes one): checked without it (specs/scene-recording.md section 7).
        table, table_problems = (
            (None, []) if not args.cues and not table_path.exists()
            else _load_cue_table(table_path, cfg["lib"], root)
        )
    except (OSError, ValueError) as exc:
        return fail(f"cannot read the cue table {table_path}: {exc}")
    try:
        log = scene_log.read_log(log_path)
    except OSError as exc:
        return fail(f"cannot read the log {log_path}: {exc}")
    if table_problems:
        return _scene_report(args, 1, table_problems, list(log.warnings))

    made = scene_session.make_session(cfg, log, root)
    errors, warnings = list(made.errors), list(made.warnings)
    check = None
    if made.session is not None:
        check = scene_check.check_session(made.session, table, require_all=args.require_all)
        errors += check.errors
        warnings += check.warnings
    if errors:
        return _scene_report(args, 1, errors, warnings, check)

    out.parent.mkdir(parents=True, exist_ok=True)
    scene_build.write_json(out, made.session)
    extra: dict = {"session": str(out)}
    done = f"; wrote {out}"
    if args.build:
        try:
            built = scene_build.build(out, root, ffmpeg=args.ffmpeg, keep_frames=args.keep_frames)
        except scene_build.SceneBuildError as exc:
            return _scene_report(args, 1, [str(exc)], warnings, check, extra=extra)
        warnings += built.warnings
        extra["built"] = str(built.out_dir)
        done += f"; built {built.out_dir} ({built.frames} frames)"
    return _scene_report(
        args, 0, [], warnings, check, events=len(made.session["events"]), table=table, extra=extra, done=done,
    )


def _cmd_scene_check(args: argparse.Namespace) -> int:
    """Exit codes: 0 ok / 1 problems / 2 bad argument or unreadable input."""
    from hapbeat_helper import cue_table, scene_build, scene_check

    def fail(message: str) -> int:
        return _scene_report(args, 2, [message], [])

    session_path = Path(args.session).expanduser()
    root = Path(args.project_root).expanduser() if args.project_root else Path.cwd()
    if not root.is_dir():
        return fail(f"not a directory: {root}")
    try:
        session = cue_table.load_json(session_path)
    except (OSError, ValueError) as exc:
        return fail(f"cannot read {args.session}: {exc}")
    problems = scene_build.validate_session(session)
    if problems:
        return _scene_report(args, 1, problems, [])
    table_path = Path(args.cues).expanduser() if args.cues else root / session["lib"]["paths"]["cues"]
    try:
        # No cue table yet (before Studio makes one): checked without it (specs/scene-recording.md section 7).
        table, table_problems = (
            (None, []) if not args.cues and not table_path.exists()
            else _load_cue_table(table_path, session["lib"], root)
        )
    except (OSError, ValueError) as exc:
        return fail(f"cannot read the cue table {table_path}: {exc}")
    if table_problems:
        return _scene_report(args, 1, table_problems, [])
    check = scene_check.check_session(session, table, require_all=args.require_all)
    code = 1 if check.errors else 0
    return _scene_report(
        args, code, check.errors, check.warnings, check, events=len(session["events"]), table=table,
    )


def _cmd_scene_guide(args: argparse.Namespace) -> int:
    from hapbeat_helper import scene_guide

    sys.stdout.write(scene_guide.guide(args.engine))
    return 0


def _cmd_scene_template(args: argparse.Namespace) -> int:
    """Exit codes: 0 written (existing files are kept unless --force) / 2 cannot write."""
    from hapbeat_helper import scene_guide

    try:
        written, skipped = scene_guide.write_templates(args.engine, Path(args.out).expanduser(), force=args.force)
    except OSError as exc:
        print(f"error: cannot write to {args.out}: {exc}", file=sys.stderr)
        return 2
    for path in written:
        print(f"wrote {path}")
    for path in skipped:
        print(f"kept {path} (exists; --force to overwrite)")
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


def _cmd_cues_scan(args: argparse.Namespace) -> int:
    """Exit codes: 0 written / 2 bad argument, unreadable input or a binary asset."""
    from hapbeat_helper import cue_inventory, unity_scan

    project = Path(args.unity).expanduser()
    out_dir = Path(args.out).expanduser() if args.out else project / "Saved" / "HapticScan"
    inv_path, wiring_path = out_dir / cue_inventory.INVENTORY_NAME, out_dir / "wiring.md"

    def fail(message: str) -> int:
        if args.json:
            print(json.dumps({
                "ok": False, "inventory": None, "wiring": None, "events": 0, "maps": 0, "warnings": [],
                "error": message,
            }, ensure_ascii=False))
        else:
            print(f"error: {message}", file=sys.stderr)
        return 2

    if not project.is_dir():
        return fail(f"not a directory: {project}")
    try:
        result = unity_scan.scan(project, Path(args.sdk_root).expanduser() if args.sdk_root else None)
        cue_inventory.save_inventory(inv_path, result.inventory)
        wiring_path.write_text(result.wiring, encoding="utf-8", newline="\n")
    except (unity_scan.UnityScanError, cue_inventory.InventoryError, OSError) as exc:
        return fail(str(exc))
    inv = result.inventory
    used = sum(1 for m in inv["maps"] if m["referenced"])
    if args.json:
        print(json.dumps({
            "ok": True, "inventory": str(inv_path), "wiring": str(wiring_path), "events": len(inv["events"]),
            "maps": len(inv["maps"]), "warnings": result.warnings,
        }, ensure_ascii=False))
        return 0
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(
        f"wrote {inv_path} ({len(inv['events'])} events from {used} of {len(inv['maps'])} Event Maps) "
        f"and {wiring_path}"
    )
    return 0


def _cmd_cues_init(args: argparse.Namespace) -> int:
    """Exit codes: 0 written (or nothing to add) / 1 rule problems (nothing written) / 2 bad argument or unreadable input."""
    import copy

    from hapbeat_helper import cue_init, cue_inventory, cue_table, scene_build, scene_project

    report: dict = {
        "ok": False, "table": None, "config": None, "added": {"cues": [], "variants": [], "clips": []},
        "bindings": [], "undecided": [], "renamed": [], "warnings": [], "errors": [],
    }

    def emit(code: int) -> int:
        report["ok"] = code == 0
        if args.json:
            print(json.dumps(report, ensure_ascii=False))
        else:
            for warning in report["warnings"]:
                print(f"warning: {warning}", file=sys.stderr)
            for error in report["errors"]:
                print(f"error: {error}", file=sys.stderr)
        return code

    def fail(message: str) -> int:
        report["errors"].append(message)
        return emit(2)

    def rel(path: Path) -> str:
        return Path(os.path.relpath(path, root)).as_posix()

    root = (Path(args.project_root).expanduser() if args.project_root else Path.cwd()).resolve()
    if not root.is_dir():
        return fail(f"not a directory: {root}")
    config = Path(args.config).expanduser().resolve() if args.config else root / scene_project.CONFIG_NAME
    cfg = None
    if config.is_file():
        try:
            cfg = scene_project.load_project(config)
        except scene_project.SceneProjectError as exc:
            return fail(str(exc))
    table_path = Path(args.out or args.into).expanduser().resolve()
    report["table"] = str(table_path)
    base = None
    if args.out and table_path.exists():
        return fail(f"{table_path} exists (use --into to add to it)")
    if args.into:
        try:
            base = cue_table.load_json(table_path)
        except (OSError, ValueError) as exc:
            return fail(f"cannot read {table_path}: {exc}")
        if not isinstance(base, dict):
            return fail(f"{table_path} is not a cue table (a JSON object)")
    if args.kit and not cue_table.KIT_NAME.match(args.kit):
        return fail(f"--kit must match {cue_table.KIT_NAME.pattern}")
    if args.project and not scene_build.PROJECT_NAME.match(args.project):
        return fail(f"--project must match {scene_build.PROJECT_NAME.pattern}")

    inventory = session = None
    named = None
    if args.inventory:
        try:
            inventory = cue_inventory.load_inventory(Path(args.inventory).expanduser())
        except cue_inventory.InventoryError as exc:
            return fail(str(exc))
        named = inventory.get("project")
    elif args.from_session:
        try:
            session = cue_table.load_json(Path(args.from_session).expanduser())
        except (OSError, ValueError) as exc:
            return fail(f"cannot read {args.from_session}: {exc}")
        problems = scene_build.validate_session(session)
        if problems:
            return fail(f"invalid session {args.from_session}: " + "; ".join(problems))
        named = session["project"]
    elif args.loop and not args.add:
        return fail("--loop goes with --add")
    project = named or args.project or (cfg["project"] if cfg else None)
    kit = args.kit or (f"{project}-kit" if project else None)
    if base is None and kit is None:
        return fail("no project name for the new table's kit: pass --project or --kit")

    if inventory is not None:
        if cfg is not None:
            clips_dir = root / cfg["lib"]["paths"]["clips"]
            sounds_dir = root / cfg["lib"]["paths"]["sounds"]
        else:
            if not project:
                return fail(f"no project name for {config.name}: pass --project")
            clips_dir = root / args.clips if args.clips else table_path.parent / "Clips"
            sounds_dir = root / args.sounds if args.sounds else table_path.parent / "Sounds"
        source_root = Path(args.source_root).expanduser().resolve() if args.source_root else root
        try:
            plan = cue_init.plan_inventory(
                inventory, base, kit=kit or "", source_root=source_root, clips_dir=clips_dir.resolve(),
                bindings=cfg.get("bindings", []) if cfg else [],
            )
        except cue_init.CueInitError as exc:
            return fail(str(exc))
    else:
        names: list[list[str]] = []
        for raw in (args.add, args.loop):
            items = [n.strip() for n in (raw or "").split(",") if n.strip()]
            bad = [n for n in items if not cue_table.CUE_NAME.match(n)]
            if bad:
                return fail(f"cue names must match {cue_table.CUE_NAME.pattern}: {', '.join(bad)}")
            names.append(items)
        if session is not None:
            plan = cue_init.plan_session(base, session, kit or "")
        else:
            plan = cue_init.plan_add(base, kit or "", names[0], names[1])

    report["added"], report["undecided"], report["renamed"] = plan.added, plan.undecided, plan.renamed
    report["warnings"] = list(plan.warnings)
    report["errors"] = list(plan.errors)
    unfired: list[str] = []
    rests = [f"{r['cue']}: the rest of the target ({', '.join(r['rest'])}) is not in the table" for r in plan.target_rest]
    report["warnings"] += rests
    if inventory is not None:
        report["warnings"] += [f"inventory: {w}" for w in inventory.get("warnings", []) if "levelMap" in w]
        for m in inventory.get("maps", []):
            if not m["referenced"]:
                report["warnings"].append(f"Event Map {m['path']} is not referenced by any scene / prefab / controller")
        for ev in inventory["events"]:
            if not ev.get("where"):
                unfired.append(f"{ev['source']} ({ev['name']})")
        report["warnings"] += [f"{u}: nothing fires it in the scenes / prefabs (where is empty)" for u in unfired]

    new_cfg = None
    if inventory is not None and not plan.errors:
        if cfg is not None:
            new_cfg = copy.deepcopy(cfg)
        else:
            new_cfg = scene_project.new_project(
                project, args.title or project, rel(table_path), rel(clips_dir), rel(sounds_dir),
            )
        scene_project.set_bindings(new_cfg, plan.bindings)
        report["errors"] += [f"{config.name}: {p}" for p in scene_project.validate_project(new_cfg)]
        report["bindings"] = plan.bindings
        report["config"] = str(config)
    if report["errors"]:
        return emit(1)

    if not args.dry_run:
        try:
            cue_init.write_plan(plan, table_path)
            if new_cfg is not None and new_cfg != cfg:
                scene_project.save_project(config, new_cfg)
        except (cue_init.CueInitError, scene_project.SceneProjectError, OSError) as exc:
            return fail(str(exc))
    if args.json:
        return emit(0)
    emit(0)
    verb = "would write" if args.dry_run else "wrote"
    added = plan.added
    counts = f"+{len(added['cues'])} cues, +{len(added['variants'])} variants, +{len(added['clips'])} clips"
    print(f"{verb} {table_path} ({counts})" if plan.table_changed else f"no change to {table_path}")
    if new_cfg is not None:
        if new_cfg != cfg:
            print(f"{verb} {config} ({len(plan.bindings)} bindings)")
        else:
            print(f"no change to {config}")
    to_decide = [f"{u['cue']}: haptics undecided ({u['reason']})" for u in plan.undecided]
    to_decide += [f"{r['from']} -> {r['to']} (renamed: {', '.join(r['sources'])})" for r in plan.renamed]
    to_decide += [w for w in plan.warnings if " limited to " in w or "levelMap not written" in w]
    to_decide += rests
    if inventory is not None:
        to_decide += [f"inventory: {w}" for w in inventory.get("warnings", []) if "levelMap" in w]
        to_decide += [f"Event Map {m['path']} is not referenced" for m in inventory.get("maps", []) if not m["referenced"]]
        to_decide += [f"{u}: where is empty (fired from code?)" for u in unfired]
    if to_decide:
        print("to decide:")
        for line in to_decide:
            print(f"  {line}")
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

    p_ssession = sub_scene.add_parser(
        "session",
        help="recording log + hapbeat-scene.json -> session.json (checked against the cue table)",
        description=(
            "Read the recording's log (hapbeat-contracts specs/scene-recording.md "
            "section 5) with the project's hapbeat-scene.json (section 6), check it "
            "against the cue table (section 7) and write session.json (section 2). "
            "Nothing is written when there is an error. The project root is the "
            "folder of the config; its paths are relative to it."
        ),
    )
    p_ssession.add_argument(
        "--config", metavar="PATH", help="the project's hapbeat-scene.json (default: ./hapbeat-scene.json)",
    )
    p_ssession.add_argument(
        "--log", metavar="FILE",
        help="the log: its own file or an engine log with HAPBEAT_SCENE lines (default: the config's log)",
    )
    p_ssession.add_argument(
        "--out", metavar="FILE", help="session.json to write (default: <root>/Saved/HapticRecord/session.json)",
    )
    p_ssession.add_argument(
        "--cues", metavar="TABLE", help="the cue table (default: <root>/<lib.paths.cues>)",
    )
    p_ssession.add_argument(
        "--require-all", action="store_true", help="every cue and variant of the table must fire",
    )
    p_ssession.add_argument(
        "--build", action="store_true", help="then run scene build on the written session",
    )
    p_ssession.add_argument(
        "--ffmpeg", metavar="PATH", help="with --build: ffmpeg executable with libx264 (default: ffmpeg on PATH)",
    )
    p_ssession.add_argument(
        "--keep-frames", action="store_true", help="with --build: keep the PNG frames folder",
    )
    p_ssession.add_argument(
        "--lib-only", action="store_true",
        help="only rewrite <root>/Saved/HapticViewer/viewer-lib.json from the config's lib (no log needed)",
    )
    p_ssession.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    p_ssession.set_defaults(func=_cmd_scene_session)

    p_scheck = sub_scene.add_parser(
        "check",
        help="check a session.json against the cue table",
        description=(
            "Check a recording's session.json against the cue table "
            "(hapbeat-contracts specs/scene-recording.md section 7): unknown cues "
            "and variants, level columns, the takes, and with --require-all that "
            "every cue and variant fired."
        ),
    )
    p_scheck.add_argument("--session", required=True, metavar="FILE", help="the recording's session.json")
    p_scheck.add_argument(
        "--cues", metavar="TABLE", help="the cue table (default: <project-root>/<lib.paths.cues>)",
    )
    p_scheck.add_argument(
        "--project-root", metavar="DIR", help="game project root (default: the current directory)",
    )
    p_scheck.add_argument(
        "--require-all", action="store_true", help="every cue and variant of the table must fire",
    )
    p_scheck.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    p_scheck.set_defaults(func=_cmd_scene_check)

    from hapbeat_helper.scene_guide import ENGINES, TEMPLATE_ENGINES

    p_sguide = sub_scene.add_parser(
        "guide",
        help="print the AI-agent guide: a first-draft cue table and a checked recording",
        description=(
            "Print the step-by-step guide for an AI agent: find the game's haptic events, "
            "draft the cue table (cues scan / cues init), write a recording script that "
            "fires every cue, record it and pass scene session --require-all --build."
        ),
    )
    p_sguide.add_argument("--engine", choices=ENGINES, help="only this engine's notes (default: all)")
    p_sguide.set_defaults(func=_cmd_scene_guide)

    p_stemplate = sub_scene.add_parser(
        "template",
        help="copy the recording log writer for an engine into a folder",
        description=(
            "Copy the files that write the recording log (hapbeat-contracts "
            "specs/scene-recording.md section 5) into DIR: unity = HapbeatSceneLog.cs and "
            "HapbeatSceneSdkCapture.cs, unreal = HapbeatSceneLog.h, js = hapbeat-scene-log.mjs. "
            "Existing files are kept unless --force."
        ),
    )
    p_stemplate.add_argument("engine", choices=TEMPLATE_ENGINES)
    p_stemplate.add_argument("--out", required=True, metavar="DIR", help="folder to copy into (created if needed)")
    p_stemplate.add_argument("--force", action="store_true", help="overwrite existing files")
    p_stemplate.set_defaults(func=_cmd_scene_template)

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

    p_cscan = sub_cues.add_parser(
        "scan",
        help="read a Unity project's Event Maps and triggers into an event inventory",
        description=(
            "Read the Event Maps, scenes, prefabs and Animator controllers of a Unity project "
            "(Force Text) and write inventory.json (hapbeat-contracts specs/cue-table.md section 10) "
            "and wiring.md (what fires each entry). Only the Event Maps something references become "
            "events. The SDK's script GUIDs are read from its installed *.cs.meta: Packages/manifest.json "
            "com.hapbeat.sdk, Packages/com.hapbeat.sdk, Library/PackageCache, then --sdk-root."
        ),
    )
    p_cscan.add_argument("--unity", required=True, metavar="PROJECT_DIR", help="the Unity project folder")
    p_cscan.add_argument("--out", metavar="DIR", help="output folder (default: <project>/Saved/HapticScan)")
    p_cscan.add_argument("--sdk-root", metavar="DIR", help="the Hapbeat Unity SDK package folder, when not found")
    p_cscan.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    p_cscan.set_defaults(func=_cmd_cues_scan)

    p_cinit = sub_cues.add_parser(
        "init",
        help="first cue table (+ recording bindings) from an inventory, names, or a recording",
        description=(
            "Make a first cue table by hapbeat-contracts specs/cue-table.md section 10: from an inventory "
            "(cues, haptic routes and clips with the WAVs copied into the clip folder, plus the bindings "
            "of hapbeat-scene.json), from cue names (--add, --loop: undecided cues), or from a recording "
            "(--from-session: the cues it fires). Sound effects stay undecided. --into adds only what the "
            "table does not have. Nothing is written when a rule fails (exit 1) or with --dry-run."
        ),
    )
    src = p_cinit.add_mutually_exclusive_group(required=True)
    src.add_argument("--inventory", metavar="FILE", help="an inventory.json (cues scan, or hand-written)")
    src.add_argument("--add", metavar="A,B", help="cue names to add as undecided cues")
    src.add_argument("--from-session", metavar="SESSION", help="a recording's session.json")
    p_cinit.add_argument("--loop", metavar="C", help="with --add: loop cue names (loop: true)")
    dst = p_cinit.add_mutually_exclusive_group(required=True)
    dst.add_argument("--out", metavar="TABLE", help="a new cue table (must not exist)")
    dst.add_argument("--into", metavar="TABLE", help="an existing cue table to add to")
    p_cinit.add_argument("--project-root", metavar="DIR", help="game project root (default: the current directory)")
    p_cinit.add_argument("--config", metavar="PATH", help="hapbeat-scene.json (default: <project-root>/hapbeat-scene.json)")
    p_cinit.add_argument(
        "--source-root", metavar="DIR",
        help="the folder the inventory's wav paths are relative to (default: the project root)",
    )
    p_cinit.add_argument("--project", metavar="NAME", help="project name when the input has none")
    p_cinit.add_argument("--title", metavar="T", help="title of a new hapbeat-scene.json (default: the project name)")
    p_cinit.add_argument(
        "--clips", metavar="DIR",
        help="clip folder under the project root when there is no hapbeat-scene.json (default: <table folder>/Clips)",
    )
    p_cinit.add_argument(
        "--sounds", metavar="DIR",
        help="sound folder under the project root when there is no hapbeat-scene.json (default: <table folder>/Sounds)",
    )
    p_cinit.add_argument("--kit", metavar="NAME", help="kit of a new table (default: <project>-kit)")
    p_cinit.add_argument("--dry-run", action="store_true", help="write nothing (no table, WAV or config)")
    p_cinit.add_argument("--json", action="store_true", help="print one JSON object instead of text")
    p_cinit.set_defaults(func=_cmd_cues_init)

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
