"""A recording's log (``hapbeat-contracts`` ``specs/scene-recording.md`` section 5).

The game (or the SDK's fire notification) writes one JSON record per line while it records, either
to its own file (default ``Saved/HapticRecord/events.jsonl``) or into the engine's log with the
marker ``HAPBEAT_SCENE `` in front of the JSON. ``read_log`` reads either kind, keeps the records
from the last ``start`` up to the first ``end`` after it, and checks each record on its own (section 5.2).
``hapbeat-helper scene session`` (``scene_session.py``) turns the records into ``session.json``.

Checks use the standard library only (no jsonschema dependency).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hapbeat_helper.cue_table import CUE_REF, is_number
from hapbeat_helper.scene_build import HANDS, LEVEL_COLUMN
from hapbeat_helper.scene_project import SOURCE

MARKER = "HAPBEAT_SCENE "

# Record types (the ``t`` of a record, section 5.2).
T_START = "start"
T_TAKE = "take"
T_TAKE_END = "take_end"
T_CUE = "cue"
T_LEVEL = "level"
T_FAIL = "fail"
T_END = "end"
RECORD_TYPES = (T_START, T_TAKE, T_TAKE_END, T_CUE, T_LEVEL, T_FAIL, T_END)
# Records that need a frame (``fail`` may have one).
FRAMED = (T_TAKE, T_TAKE_END, T_CUE, T_LEVEL)


@dataclass
class LogResult:
    """``records[i]`` was read from line ``lines[i]`` (1-based). Problems carry their line number."""

    records: list[dict] = field(default_factory=list)
    lines: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    is_log: bool = True  # False: no marker and the first non-empty line is not JSON (section 5.1)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _cue_ref_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(x, str) and CUE_REF.match(x) for x in value)


def check_record(rec: dict) -> list[str]:
    """Problems of one known record (section 5.2), without the line number."""
    t = rec["t"]
    err: list[str] = []
    if t in FRAMED or (t == T_FAIL and "frame" in rec):
        if not _is_int(rec.get("frame")) or rec["frame"] < 0:
            err.append(f"{t}: frame must be an integer >= 0")
    if t == T_START:
        if rec.get("schema_version") != 1 or isinstance(rec.get("schema_version"), bool):
            err.append("start: schema_version must be 1")
        if not is_number(rec.get("fps")) or rec["fps"] <= 0:
            err.append("start: fps must be a number > 0")
        if "project" in rec and not isinstance(rec["project"], str):
            err.append("start: project must be a string")
    elif t == T_TAKE:
        if not isinstance(rec.get("label"), str):
            err.append("take: label must be a string")
        for key in ("expect", "allow"):
            if key in rec and not _cue_ref_list(rec[key]):
                err.append(f"take: {key} must be a list of <cue> or <cue>:<variant>")
    elif t == T_CUE:
        if ("cue" in rec) == ("source" in rec):
            err.append("cue: exactly one of cue / source is needed")
        elif "cue" in rec and not (isinstance(rec["cue"], str) and CUE_REF.match(rec["cue"])):
            err.append("cue: cue must be <cue> or <cue>:<variant>")
        elif "source" in rec and not (isinstance(rec["source"], str) and SOURCE.match(rec["source"])):
            err.append("cue: source must be <engine>:<map id>/<entry id>")
    elif t == T_LEVEL:
        values = rec.get("values")
        if ("values" in rec) == ("source" in rec):
            err.append("level: exactly one of values / source (with value) is needed")
        elif "source" in rec:
            if not (isinstance(rec["source"], str) and SOURCE.match(rec["source"])):
                err.append("level: source must be <engine>:<map id>/<entry id>")
            if not is_number(rec.get("value")) or rec["value"] < 0:
                err.append("level: value must be a number >= 0")
            if "hand" in rec and rec["hand"] not in HANDS:
                err.append(f"level: hand must be one of {', '.join(HANDS)}")
        elif not isinstance(values, dict) or not values:
            err.append("level: values must be a non-empty object")
        else:
            for name, value in values.items():
                if not LEVEL_COLUMN.match(name):
                    err.append(f"level: {name} is not a column name <cue>:level|rate[:left|right]")
                elif not is_number(value) or value < 0:
                    err.append(f"level: {name} must be a number >= 0")
    elif t == T_END:
        if not _is_int(rec.get("frames")) or rec["frames"] < 1:
            err.append("end: frames must be an integer >= 1")
    return err


def _decode(text: str) -> list[tuple[int, Any | None, str]] | None:
    """``(line, value, problem)`` per record line; value None when the line could not be decoded.

    None when the text is not a log: no marker anywhere and the first non-empty line is not JSON.
    """
    lines = text.splitlines()
    marked = any(MARKER in line for line in lines)
    decoder = json.JSONDecoder()
    out: list[tuple[int, Any | None, str]] = []
    for n, line in enumerate(lines, start=1):
        if marked:
            at = line.find(MARKER)
            if at < 0:
                continue
            try:
                value, _end = decoder.raw_decode(line, at + len(MARKER))
            except ValueError as exc:
                out.append((n, None, f"no JSON after the marker ({exc})"))
                continue
        else:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except ValueError as exc:
                if not out:
                    return None
                out.append((n, None, f"not JSON ({exc})"))
                continue
        out.append((n, value, ""))
    return out


def parse_log_text(text: str) -> LogResult:
    """Read a log (section 5.1): from the last ``start`` (included) up to the first ``end`` after it
    (included). Lines after that ``end`` are left out with a warning; lines that cannot be read are errors.
    """
    result = LogResult()
    decoded = _decode(text)
    if decoded is None:
        result.is_log = False
        return result
    first = 0
    for i, (_n, value, _p) in enumerate(decoded):
        if isinstance(value, dict) and value.get("t") == T_START:
            first = i
    decoded = decoded[first:]
    for i, (_n, value, _p) in enumerate(decoded):
        if isinstance(value, dict) and value.get("t") == T_END:
            rest = decoded[i + 1:]
            if rest:
                result.warnings.append(
                    f"{len(rest)} line(s) after the end record (line {decoded[i][0]}) left out "
                    f"(lines {rest[0][0]}-{rest[-1][0]})"
                )
            decoded = decoded[:i + 1]
            break
    for n, value, problem in decoded:
        if value is None:
            result.errors.append(f"line {n}: cannot be read, {problem}")
            continue
        if not isinstance(value, dict):
            result.errors.append(f"line {n}: cannot be read, a record must be a JSON object")
            continue
        t = value.get("t")
        if t not in RECORD_TYPES:
            result.warnings.append(f"line {n}: unknown record type t={t!r} (ignored)")
            continue
        result.errors += [f"line {n}: {p}" for p in check_record(value)]
        result.records.append(value)
        result.lines.append(n)
    return result


def read_log(path: Path) -> LogResult:
    """``parse_log_text`` of a file (UTF-8, BOM tolerated, undecodable bytes replaced). Raises OSError."""
    return parse_log_text(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
