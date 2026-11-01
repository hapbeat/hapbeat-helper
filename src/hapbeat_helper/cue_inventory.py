"""The event inventory (``hapbeat-contracts`` ``specs/cue-table.md`` section 10).

One item per SDK event (an Event Map entry in Unity / Unreal), with what a first cue table needs:
its id (``source``), name, loop, target, gain, haptic WAV, clip_gain and where it is fired.
``hapbeat-helper cues scan --unity`` writes it, ``hapbeat-helper cues init`` reads it; agents and
people may also write it by hand (Unreal, games with their own cue layer).

Checks use the standard library only (no jsonschema dependency).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from hapbeat_helper.cue_table import CUE_REF, in_range, is_level_map, is_number
from hapbeat_helper.scene_build import PROJECT_NAME, write_json
from hapbeat_helper.scene_project import SOURCE

INVENTORY_NAME = "inventory.json"
ENGINES = ("unity", "unreal", "godot", "js", "python", "other")

_CAMEL_LOWER_UPPER = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_UPPER_RUN = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_NOT_NAME = re.compile(r"[^a-z0-9]+")


class InventoryError(Exception):
    """An inventory that cannot be read."""


def normalize_name(name: str, source: str) -> str:
    """A cue name from an SDK entry's name (section 10.2).

    ``GrabLight`` -> ``grab_light``, ``UI-Click-Light`` -> ``ui_click_light``,
    ``HTTPRequest`` -> ``http_request``, ``2ndHit`` -> ``c_2nd_hit``. An empty result becomes
    ``cue_`` + the first 8 letters / digits of the entry's identifier in ``source``
    (``<engine>:<map id>/<entry id>``: the part after the last ``/``).
    """
    s = _CAMEL_UPPER_RUN.sub("_", _CAMEL_LOWER_UPPER.sub("_", name))
    s = _NOT_NAME.sub("_", s.lower()).strip("_")
    if not s:
        ident = source.split(":", 1)[-1].rsplit("/", 1)[-1]
        return "cue_" + "".join(c for c in ident if c.isascii() and c.isalnum())[:8].lower()
    return "c_" + s if s[0].isdigit() else s


def validate_inventory(inv: Any) -> list[str]:
    """Problems of an inventory (section 10.1), one line each."""
    if not isinstance(inv, dict):
        return ["inventory must be a JSON object"]
    err: list[str] = []
    if inv.get("schema_version") != 1 or isinstance(inv.get("schema_version"), bool):
        err.append("schema_version must be 1")
    if inv.get("engine") not in ENGINES:
        err.append(f"engine must be one of {', '.join(ENGINES)}")
    if "project" in inv and not (isinstance(inv["project"], str) and PROJECT_NAME.match(inv["project"])):
        err.append(f"project must match {PROJECT_NAME.pattern}")
    maps = inv.get("maps", [])
    if not isinstance(maps, list):
        err.append("maps must be a list")
    else:
        for i, m in enumerate(maps):
            if (
                not isinstance(m, dict) or not (isinstance(m.get("path"), str) and m["path"])
                or ("id" in m and not isinstance(m["id"], str)) or not isinstance(m.get("referenced"), bool)
            ):
                err.append(f"maps[{i}] must be {{path, id?, referenced: true/false}}")
    if "warnings" in inv and not (
        isinstance(inv["warnings"], list) and all(isinstance(w, str) for w in inv["warnings"])
    ):
        err.append("warnings must be a list of strings")
    events = inv.get("events")
    if not isinstance(events, list):
        err.append("events must be a list")
        return err
    seen: set[str] = set()
    for i, e in enumerate(events):
        label = f"events[{i}]"
        if not isinstance(e, dict):
            err.append(f"{label} must be an object")
            continue
        source = e.get("source")
        if not isinstance(source, str) or not SOURCE.match(source):
            err.append(f"{label}.source must be <engine>:<id>")
        elif source in seen:
            err.append(f"{label}.source {source} is listed twice")
        else:
            seen.add(source)
        if "map" in e and not (isinstance(e["map"], str) and e["map"]):
            err.append(f"{label}.map must be a non-empty string (maps[].id)")
        if not (isinstance(e.get("name"), str) and e["name"]):
            err.append(f"{label}.name must be a non-empty string")
        if "cue" in e and not (isinstance(e["cue"], str) and CUE_REF.match(e["cue"])):
            err.append(f"{label}.cue must be <cue> or <cue>:<variant>")
        if not isinstance(e.get("loop"), bool):
            err.append(f"{label}.loop must be true/false")
        if not isinstance(e.get("target"), str):
            err.append(f"{label}.target must be a string (empty = everyone)")
        if not (is_number(e.get("gain")) and e["gain"] >= 0):
            err.append(f"{label}.gain must be a number >= 0")
        if "wav" in e and not (isinstance(e["wav"], str) and e["wav"]):
            err.append(f"{label}.wav must be a non-empty path")
        if "clip_gain" in e and not in_range(e["clip_gain"], 0, 1):
            err.append(f"{label}.clip_gain must be 0..1")
        if "levelMap" in e:
            if e.get("loop") is not True:
                err.append(f"{label}.levelMap is for loop items")
            elif not is_level_map(e["levelMap"]):
                err.append(f"{label}.levelMap must have the cue-table section 5.6 shape")
        if "where" in e and not (isinstance(e["where"], list) and all(isinstance(w, str) for w in e["where"])):
            err.append(f"{label}.where must be a list of strings")
        if "note" in e and not isinstance(e["note"], str):
            err.append(f"{label}.note must be a string")
    return err


def load_inventory(path: Path) -> dict:
    """Read and check an inventory; raises InventoryError with every problem."""
    try:
        inv = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise InventoryError(f"cannot read {path}: {exc}") from exc
    problems = validate_inventory(inv)
    if problems:
        raise InventoryError(f"invalid {path}:\n" + "\n".join(f"  {p}" for p in problems))
    return inv


def save_inventory(path: Path, inv: dict) -> None:
    """Write an inventory (UTF-8, 2-space indent, trailing newline). Checks it first."""
    problems = validate_inventory(inv)
    if problems:
        raise InventoryError(f"refusing to write an invalid {path}:\n" + "\n".join(f"  {p}" for p in problems))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    write_json(Path(path), inv)


def cue_of(event: dict, bound: dict[str, str] | None = None) -> tuple[str, bool]:
    """The cue (``<cue>`` or ``<cue>:<variant>``) of an item by section 10.2, and whether it was named
    (rules 1-2: the item's ``cue``, or ``bound`` = an existing ``{source: cue}`` binding) rather than derived."""
    if event.get("cue"):
        return event["cue"], True
    if bound and event["source"] in bound:
        return bound[event["source"]], True
    return normalize_name(event["name"], event["source"]), False
