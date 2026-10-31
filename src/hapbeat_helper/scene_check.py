"""A recording (``session.json``) against the cue table (``hapbeat-helper scene check``).

``hapbeat-contracts`` ``specs/scene-recording.md`` section 7: errors (7.2 rules 1-3, 5, 6; rule 4
belongs to building the session from a log, ``scene_session.py``) and warnings (7.3 1, 2, 5, 6;
3 and 4 come from the log as well). The cue table is ``specs/cue-table.md``; it is expected to have
passed ``cue_table.validate_cue_table`` already. Without a cue table yet (None), only the session's
shape and the takes are checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hapbeat_helper.scene_build import validate_session


@dataclass
class CheckResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    coverage: dict[str, int] = field(default_factory=dict)  # {fired cue ref: count}
    takes: list[dict] = field(default_factory=list)  # [{label, start, end, ok, fired, missing, unexpected}]


def matches(pattern: str, ref: str) -> bool:
    """``<cue>`` matches the cue and all its variants; ``<cue>:<variant>`` only itself (section 7.1)."""
    return ref == pattern or (":" not in pattern and ref.split(":", 1)[0] == pattern)


def _split(ref: str) -> tuple[str, str | None]:
    name, _sep, variant = ref.partition(":")
    return name, variant or None


def _cues(table: Any) -> dict:
    cues = table.get("cues") if isinstance(table, dict) else None
    return cues if isinstance(cues, dict) else {}


def is_loop(table: Any, name: str) -> bool:
    cue = _cues(table).get(name)
    return isinstance(cue, dict) and cue.get("loop") is True


def has_hand_route(table: Any, ref: str) -> bool | None:
    """Whether the routes played for ``ref`` include ``at: hand``; None when its haptics are undecided.

    A variant plays its own ``haptics`` when it has the key, else the parent's (cue-table 5.3).
    """
    name, variant = _split(ref)
    cue = _cues(table).get(name, {})
    entry = cue
    if variant is not None:
        own = (cue.get("variants") or {}).get(variant, {})
        if isinstance(own, dict) and "haptics" in own:
            entry = own
    if "haptics" not in entry:
        return None
    haptics = entry["haptics"]
    return isinstance(haptics, list) and any(isinstance(r, dict) and r.get("at") == "hand" for r in haptics)


def counted_cues(table: Any) -> list[str]:
    """Cues that should fire in a recording: all but the ``emit`` ones (Studio makes those, cue-table 5.7)."""
    return [name for name, cue in _cues(table).items() if not (isinstance(cue, dict) and "emit" in cue)]


def cue_counts(coverage: dict[str, int], table: Any) -> tuple[int, int]:
    """``(fired, total)`` over ``counted_cues``; a variant's event counts for its cue."""
    names = counted_cues(table)
    fired = {_split(ref)[0] for ref in coverage}
    return sum(1 for n in names if n in fired), len(names)


def check_session(session: Any, table: Any, require_all: bool = False) -> CheckResult:
    """``table`` None = no cue table yet (section 7): rules 2, 3, 6 and warnings 1, 2, 5 are skipped."""
    result = CheckResult()
    # Rule 1: the session itself.
    problems = validate_session(session)
    if problems:
        result.errors = problems
        return result
    cues = _cues(table)
    events = session["events"]
    err, warn = result.errors, result.warnings

    for e in events:
        result.coverage[e["cue"]] = result.coverage.get(e["cue"], 0) + 1
    if table is None:
        warn.append("no cue table yet: the cues, variants, level columns and hands were not checked")

    # Rule 2: every fired cue and variant is in the table.
    known: list[bool] = []
    for i, e in enumerate(events if table is not None else []):
        name, variant = _split(e["cue"])
        label = f"events[{i}] (frame {e['frame']})"
        if name not in cues:
            err.append(f"{label}: cue {name} is not in the cue table")
            known.append(False)
        elif variant is not None and variant not in (cues[name].get("variants") or {}):
            err.append(f"{label}: {name} has no variant {variant}")
            known.append(False)
        else:
            known.append(True)

    # Rule 3: level columns only for loop cues; a fired loop cue has a level column.
    columns = session["levels"]["columns"] if "levels" in session and table is not None else []
    for col in columns:
        name = col.split(":", 1)[0]
        if not is_loop(table, name):
            what = "not a loop cue" if name in cues else "not in the cue table"
            err.append(f"levels column {col}: {name} is {what}")
    fired_loops = []
    for e in events if table is not None else []:
        name = _split(e["cue"])[0]
        if is_loop(table, name) and name not in fired_loops:
            fired_loops.append(name)
    for name in fired_loops:
        if not any(c in columns for c in (f"{name}:level", f"{name}:level:left", f"{name}:level:right")):
            err.append(f"loop cue {name} fired but levels has no {name}:level, {name}:level:left or {name}:level:right")

    # Rule 5: the takes (section 7.1).
    takes = session.get("takes", [])
    for take in takes:
        start, end = take["start"], take["end"]
        expect, allow = take.get("expect", []), take.get("allow", [])
        fired: list[str] = []
        unexpected: list[str] = []
        for e in events:
            if not start <= e["frame"] < end:
                continue
            ref = e["cue"]
            if ref not in fired:
                fired.append(ref)
            if not any(matches(p, ref) for p in expect + allow):
                err.append(
                    f"take {take['label']!r} (frames {start}-{end - 1}): {ref} fired at frame {e['frame']}, "
                    "not in expect / allow"
                )
                if ref not in unexpected:
                    unexpected.append(ref)
        missing = [p for p in expect if not any(matches(p, ref) for ref in fired)]
        for p in missing:
            err.append(f"take {take['label']!r} (frames {start}-{end - 1}): expected {p} did not fire")
        result.takes.append({
            "label": take["label"], "start": start, "end": end,
            "ok": not missing and not unexpected, "fired": fired, "missing": missing, "unexpected": unexpected,
        })

    # Rule 6 (with --require-all) / warning 5: cues and variants that never fired.
    # Cues with emit and their variants are not counted.
    unfired: list[str] = []
    for name in counted_cues(table):
        if not any(_split(ref)[0] == name for ref in result.coverage):
            unfired.append(f"cue {name}")
        variants = cues[name].get("variants") if isinstance(cues[name], dict) else None
        for variant in variants or {}:
            if f"{name}:{variant}" not in result.coverage:
                unfired.append(f"variant {name}:{variant}")
    for what in unfired:
        (err if require_all else warn).append(f"{what} never fired")

    # Warnings 1, 2: hand against the at: hand routes (per cue ref, known ones only).
    no_hand: dict[str, list[int]] = {}
    unused_hand: dict[str, list[int]] = {}
    for e, ok in zip(events, known):
        if not ok:
            continue
        hand_route = has_hand_route(table, e["cue"])
        if hand_route is None:
            continue  # undecided haptics
        if hand_route and "hand" not in e:
            no_hand.setdefault(e["cue"], []).append(e["frame"])
        elif not hand_route and e.get("hand") in ("left", "right"):
            unused_hand.setdefault(e["cue"], []).append(e["frame"])
    for ref, frames in no_hand.items():
        warn.append(
            f"{ref}: {len(frames)} event(s) without hand (first at frame {frames[0]}); "
            "its at: hand route plays on both wrists"
        )
    for ref, frames in unused_hand.items():
        warn.append(
            f"{ref}: {len(frames)} event(s) with hand (first at frame {frames[0]}); "
            "it plays no at: hand route, so hand is not used"
        )

    # Warning 6: events outside every take.
    if takes:
        for i, e in enumerate(events):
            if not any(t["start"] <= e["frame"] < t["end"] for t in takes):
                warn.append(f"events[{i}] (frame {e['frame']}): {e['cue']} is outside every take")
    return result
