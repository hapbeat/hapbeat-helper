"""A recording's log + ``hapbeat-scene.json`` -> ``session.json`` (``hapbeat-helper scene session``).

``hapbeat-contracts`` ``specs/scene-recording.md``: the log is section 5 (``scene_log.py``), the
project config section 6 (``scene_project.py``), the result section 2. The recorder writes only
what happened; this module builds ``events``, ``levels.rows`` (one row per frame) and ``takes``
(section 7.1), and refuses a recording that did not finish or failed (section 7.2 rule 4).
The checks against the cue table are ``scene_check.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hapbeat_helper import scene_log
from hapbeat_helper.scene_build import EVENT_EXTRAS, FRAME_FILE, validate_session
from hapbeat_helper.scene_log import LogResult
from hapbeat_helper.scene_project import binding_map


@dataclass
class SessionResult:
    """``session`` is None when there are errors."""

    session: dict | None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _initial(column: str) -> float:
    """The value of a column before its first write (section 5.2): rate 1, level 0."""
    return 1 if column.split(":")[1] == "rate" else 0


def _levels(
    writes: list[tuple[int, int, dict]], frames: int, order: list[str], warnings: list[str],
) -> dict:
    """``{columns, rows}``: each column keeps its last written value until the next write.

    ``writes`` = ``(line, frame, {column: value})`` in log order; ``order`` = the config's ``columns``.
    """
    columns = list(order)
    for _line, _frame, values in writes:
        for name in values:
            if name not in columns:
                columns.append(name)
    index = {name: i for i, name in enumerate(columns)}
    by_frame = sorted(writes, key=lambda w: w[1])  # stable: same frame -> the later write wins
    current = [_initial(c) for c in columns]
    rows: list[list] = []
    k = 0
    for f in range(frames):
        while k < len(by_frame) and by_frame[k][1] <= f:
            for name, value in by_frame[k][2].items():
                current[index[name]] = value
            k += 1
        rows.append(list(current))
    for line, frame, _values in by_frame[k:]:
        warnings.append(f"line {line}: level at frame {frame} is past the last frame ({frames - 1}); left out")
    return {"columns": columns, "rows": rows}


def _leftover_frames(cfg: dict, root: Path | None, frames: int) -> list[str]:
    """Section 7.2 rule 4: ``f<frame>.png`` numbered ``frames`` or more are from an earlier recording."""
    if root is None or "frames" not in cfg:
        return []
    folder = root / cfg["frames"]
    if not folder.is_dir():
        return []
    late = sorted(
        int(m.group(1)) for p in folder.iterdir() if (m := FRAME_FILE.match(p.name)) and int(m.group(1)) >= frames
    )
    if not late:
        return []
    return [
        f"{folder} has {len(late)} f<frame>.png at or past frame {frames} (f{late[0]}.png ...), "
        "left from an earlier recording: delete the folder's f<frame>.png before start"
    ]


def make_session(cfg: dict, log: LogResult, root: Path | None = None) -> SessionResult:
    """Build ``session.json`` from a checked ``hapbeat-scene.json`` and a read log.

    ``root``: the project root; when given, the config's ``frames`` folder is checked for leftovers.
    """
    errors = list(log.errors)
    warnings = list(log.warnings)
    if not log.is_log:
        return SessionResult(None, ["no start record: the file is not a scene log"], warnings)
    if errors:
        return SessionResult(None, errors, warnings)
    pairs = list(zip(log.lines, log.records))

    starts = [(n, r) for n, r in pairs if r["t"] == scene_log.T_START]
    ends = [(n, r) for n, r in pairs if r["t"] == scene_log.T_END]
    if not starts:
        errors.append("no start record in the log")
    else:
        n, start = starts[0]
        if start["fps"] != cfg["fps"]:
            errors.append(f"line {n}: start fps {start['fps']} is not the fps of the config ({cfg['fps']})")
        if "project" in start and start["project"] != cfg["project"]:
            errors.append(
                f"line {n}: start project {start['project']!r} is not the project of the config ({cfg['project']!r})"
            )
    if not ends:
        errors.append("no end record in the log (the recording did not finish)")
    fails = [(n, r) for n, r in pairs if r["t"] == scene_log.T_FAIL]
    if fails:
        reasons = []
        for n, r in fails:
            where = f"line {n}" + (f", frame {r['frame']}" if "frame" in r else "")
            reasons.append(f"{r.get('reason', '')} ({where})")
        errors.append("the recording failed: " + "; ".join(reasons))

    bindings = binding_map(cfg)

    def bound(n: int, source: str) -> dict:
        binding = bindings.get(source, {})
        if not binding:
            errors.append(f"line {n}: source {source} is not in the bindings of the config (unmapped)")
        return binding

    events: list[tuple[int, dict]] = []
    level_writes: list[tuple[int, int, dict]] = []
    for n, r in pairs:
        if r["t"] == scene_log.T_CUE:
            event: dict = {"frame": r["frame"]}
            binding: dict = {}
            if "source" in r:
                binding = bound(n, r["source"])
                if not binding:
                    continue
                event["cue"] = binding["cue"]
            else:
                event["cue"] = r["cue"]
            if "hand" in r:
                event["hand"] = r["hand"]
            elif "hand" in binding:
                event["hand"] = binding["hand"]
            event.update({k: r[k] for k in EVENT_EXTRAS if k != "hand" and k in r})
            events.append((n, event))
        elif r["t"] == scene_log.T_LEVEL:
            if "values" in r:
                level_writes.append((n, r["frame"], r["values"]))
                continue
            binding = bound(n, r["source"])
            if not binding:
                continue
            cue = binding["cue"].split(":", 1)[0]  # level columns name the cue, not a variant
            hand = r.get("hand", binding.get("hand"))
            column = f"{cue}:level:{hand}" if hand in ("left", "right") else f"{cue}:level"
            level_writes.append((n, r["frame"], {column: r["value"]}))

    takes: list[dict] = []
    open_take: dict | None = None
    for n, r in pairs:
        if r["t"] == scene_log.T_TAKE:
            if open_take is not None:
                open_take["end"] = r["frame"]
                takes.append(open_take)
            open_take = {
                "start": r["frame"], "end": None, "label": r["label"],
                "expect": list(r.get("expect", [])), "allow": list(r.get("allow", [])),
            }
        elif r["t"] == scene_log.T_TAKE_END:
            if open_take is None:
                errors.append(f"line {n}: take_end with no take open")
                continue
            if r["frame"] <= open_take["start"]:
                errors.append(
                    f"line {n}: take_end at frame {r['frame']} is not after its take's frame ({open_take['start']})"
                )
            open_take["end"] = r["frame"]
            takes.append(open_take)
            open_take = None
    if errors:
        return SessionResult(None, errors, warnings)

    frames = ends[0][1]["frames"]
    if open_take is not None:
        open_take["end"] = frames
        takes.append(open_take)
    errors += _leftover_frames(cfg, root, frames)
    if errors:
        return SessionResult(None, errors, warnings)

    late = [(n, e) for n, e in events if e["frame"] >= frames]
    for n, e in late:
        warnings.append(f"line {n}: {e['cue']} at frame {e['frame']} is past the last frame ({frames - 1}); left out")
    kept = [(n, e) for n, e in events if e["frame"] < frames]
    out_of_order = sum(1 for (_a, x), (_b, y) in zip(kept, kept[1:]) if y["frame"] < x["frame"])
    if out_of_order:
        warnings.append(f"{out_of_order} event(s) out of frame order in the log; sorted by frame")
    kept.sort(key=lambda p: p[1]["frame"])  # stable

    session: dict = {
        "schema_version": cfg["schema_version"],
        "project": cfg["project"],
        "fps": cfg["fps"],
    }
    for key in ("frames", "video"):
        if key in cfg:
            session[key] = cfg[key]
    session["events"] = [e for _n, e in kept]
    if level_writes:
        session["levels"] = _levels(level_writes, frames, cfg.get("columns", []), warnings)
    if takes:
        session["takes"] = takes
    session["lib"] = cfg["lib"]
    problems = validate_session(session)
    if problems:
        return SessionResult(None, problems, warnings)
    return SessionResult(session, [], warnings)
