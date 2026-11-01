"""A first cue table from an event inventory (``hapbeat-helper cues init``).

``hapbeat-contracts`` ``specs/cue-table.md`` sections 10.2-10.7: inventory items -> cues, haptic routes
and clips (the WAVs copied into the clip folder), plus the recording ``bindings`` (source -> cue) of
``hapbeat-scene.json`` (``specs/scene-recording.md`` section 6). Sound effects are left undecided.
Two more entry points: ``--add`` / ``--loop`` (empty cues for games with their own cue layer) and
``--from-session`` (the cues a recording fires, as Studio's "Create the cue table" makes them).

Planning (``plan_*``) writes nothing; ``write_plan`` copies the WAVs and writes the table. Existing
WAVs are never overwritten, and a value an existing cue / variant / clip / sound has is never changed
(section 10.6).
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hapbeat_helper.cue_inventory import cue_of
from hapbeat_helper.cue_table import AT_VALUES, CUE_NAME, validate_cue_table
from hapbeat_helper.scene_build import write_json

POSITIONS = tuple(a for a in AT_VALUES if a.startswith("pos_"))
HAND_PAIR = frozenset(("pos_l_wrist", "pos_r_wrist"))
HAND_OF = {"pos_l_wrist": "left", "pos_r_wrist": "right"}
MAX_GAIN = 2


class CueInitError(Exception):
    """Unreadable input (a WAV the inventory names is missing)."""


@dataclass
class Plan:
    """What ``cues init`` would write. ``errors`` = rule problems (exit 1, nothing is written)."""

    table: dict
    table_changed: bool = False
    copies: list[tuple[Path, Path]] = field(default_factory=list)  # (wav in the project, new clip file)
    bindings: list[dict] = field(default_factory=list)  # the bindings to write (set_bindings)
    added: dict = field(default_factory=lambda: {"cues": [], "variants": [], "clips": []})
    undecided: list[dict] = field(default_factory=list)
    renamed: list[dict] = field(default_factory=list)
    target_rest: list[dict] = field(default_factory=list)  # {cue, rest}: the target outside the table
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def new_table(kit: str) -> dict:
    return {"schema_version": 1, "kit": kit, "clips": {}, "sounds": {}, "cues": {}}


def wav_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise CueInitError(f"cannot read {path}: {exc}") from exc


def split_target(target: str) -> tuple[str | None, str]:
    """(position, the rest of the target) by section 10.3.

    The position is the one ``/`` segment that is a device-addressing section 3 position name
    (``*`` and ``pos_*`` are not); the rest keeps the other segments with ``*`` where the position was.
    No position (none, or more than one) -> (None, target).
    """
    segments = target.split("/") if target else []
    hits = [i for i, s in enumerate(segments) if s in POSITIONS]
    if len(hits) != 1:
        return None, target
    rest = segments[:hits[0]] + ["*"] + segments[hits[0] + 1:]
    return segments[hits[0]], "/".join(rest) if len(rest) > 1 else ""


@dataclass
class _Item:
    event: dict
    cue: str  # <cue> or <cue>:<variant> after section 10.2
    named: bool  # by the inventory's cue or an existing binding (rules 1-2)
    position: str | None
    rest: str
    digest: str | None
    signature: str  # everything of section 10.3 rule 1 but the position


@dataclass
class _Group:
    """Items that become one cue (or variant): one route, or none when the haptics stay undecided."""

    cue: str
    items: list[_Item]

    @property
    def positions(self) -> set:
        return {i.position for i in self.items}

    def accepts(self, item: _Item) -> bool:
        if item.signature != self.items[0].signature:
            return False
        positions = self.positions | {item.position}
        return len(positions) == 1 or positions == HAND_PAIR


def _signature(ev: dict, rest: str, digest: str | None) -> str:
    return json.dumps(
        [rest, ev["loop"], ev["gain"], digest, ev.get("clip_gain", 1), ev.get("levelMap") if ev["loop"] else None],
        sort_keys=True,
    )


def _items(inventory: dict, source_root: Path, bound: dict[str, str]) -> list[_Item]:
    out, digests = [], {}
    for ev in inventory["events"]:
        digest = None
        if ev.get("wav"):
            wav = source_root / ev["wav"]
            if wav not in digests:
                digests[wav] = wav_hash(wav)
            digest = digests[wav]
        cue, named = cue_of(ev, bound)
        position, rest = split_target(ev["target"])
        out.append(_Item(ev, cue, named, position, rest, digest, _signature(ev, rest, digest)))
    return out


def _groups(items: list[_Item], table_cues: set[str], plan: Plan) -> list[_Group]:
    """Section 10.3: merge the items that play the same; rename (derived names) or fail."""
    loops: dict[str, set] = {}
    for item in items:
        loops.setdefault(item.cue.split(":")[0], set()).add(item.event["loop"])
    for cue, kinds in loops.items():
        if len(kinds) > 1:
            plan.errors.append(f"{cue}: its items (variants included) mix loop and one-shot")
    by_cue: dict[str, list[_Item]] = {}
    for item in items:
        by_cue.setdefault(item.cue, []).append(item)
    taken = {c.split(":")[0] for c in by_cue} | table_cues
    out: list[_Group] = []
    for cue, members in by_cue.items():
        sets: list[_Group] = []
        for item in members:
            home = next((g for g in sets if g.accepts(item)), None)
            if home is None:
                sets.append(_Group(cue, [item]))
            else:
                home.items.append(item)
        if len(sets) > 1 and any(i.named for i in members):
            sources = ", ".join(i.event["source"] for i in members)
            plan.errors.append(
                f"{cue}: the items named {cue} cannot be merged (target, gain, WAV, clip_gain or levelMap "
                f"differ): {sources}"
            )
        elif len(sets) > 1:
            n = 2
            for g in sets[1:]:
                while f"{cue}_{n}" in taken:
                    n += 1
                g.cue = f"{cue}_{n}"
                taken.add(g.cue)
                sources = [i.event["source"] for i in g.items]
                plan.renamed.append({"from": cue, "to": g.cue, "sources": sources})
                plan.warnings.append(f"{cue}: items that play differently became {g.cue} ({', '.join(sources)})")
        out += sets
    return out


class _Clips:
    """Clip names (section 10.4 rule 3): the cue's name, ``_2`` ... when the folder or the table has it."""

    def __init__(self, table: dict, clips_dir: Path) -> None:
        self.table = table
        self.clips_dir = clips_dir
        self.planned: set[str] = set()

    def name_for(self, base: str, src: Path, digest: str) -> tuple[str, bool]:
        """(clip name, whether the WAV must be copied)."""
        n, name = 1, base
        while True:
            if name not in self.table["clips"] and name not in self.planned:
                wav = self.clips_dir / f"{name}.wav"
                if not wav.exists():
                    return name, True
                if wav.is_file() and (wav.resolve() == src.resolve() or wav_hash(wav) == digest):
                    return name, False
            n += 1
            name = f"{base}_{n}"


def _route(group: _Group, clip: str) -> dict:
    ev = group.items[0].event
    at = "hand" if group.positions == HAND_PAIR else group.items[0].position
    route: dict[str, Any] = {"clip": clip, "at": at, "gain": min(ev["gain"], MAX_GAIN)}
    if ev["loop"] and ev.get("levelMap"):
        route["levelMap"] = copy.deepcopy(ev["levelMap"])
    return route


def _undecided_reason(group: _Group) -> str | None:
    reasons = []
    targets = sorted({i.event["target"] for i in group.items})
    if any(not t for t in targets):
        reasons.append("target is empty (everyone)")
    elif any(i.position is None for i in group.items):
        reasons.append(f"target {', '.join(targets)} has no single body position")
    if any(not i.event.get("wav") for i in group.items):
        reasons.append("no WAV")
    return "; ".join(reasons) or None


def plan_inventory(
    inventory: dict, base: dict | None, *, kit: str, source_root: Path, clips_dir: Path,
    bindings: list[dict] | None = None,
) -> Plan:
    """Sections 10.2-10.6 for an inventory. ``base`` = the table to add to (None = a new one);
    ``bindings`` = the bindings already in hapbeat-scene.json."""
    table = copy.deepcopy(base) if base is not None else new_table(kit)
    table.setdefault("clips", {})
    table.setdefault("cues", {})
    plan = Plan(table=table, table_changed=base is None)
    bound = {b["source"]: b["cue"] for b in bindings or [] if isinstance(b, dict) and "source" in b and "cue" in b}
    groups = _groups(_items(inventory, source_root, bound), set(table["cues"]), plan)
    if plan.errors:
        return plan
    clips = _Clips(table, clips_dir)
    original = set(table["cues"])
    skipped: set[str] = set()
    for g in groups:
        cue, _, variant = g.cue.partition(":")
        ev = g.items[0].event
        loop = ev["loop"]
        for item in g.items:
            if item.event["gain"] > MAX_GAIN:
                plan.warnings.append(f"{g.cue}: gain {item.event['gain']} of {item.event['source']} limited to {MAX_GAIN}")
        rests = sorted({i.rest for i in g.items if i.rest})
        if rests:
            plan.target_rest.append({"cue": g.cue, "rest": rests})
        existing = table["cues"].get(cue)
        if existing is None:
            # A cue made only for its variants keeps its own haptics undecided (section 10.4 rule 4).
            table["cues"][cue] = {"loop": True} if loop else {}
            plan.added["cues"].append(cue)
            plan.table_changed = True
        elif not isinstance(existing, dict):
            plan.errors.append(f"{cue}: the table's cue is not an object")
            continue
        elif cue in original and (existing.get("loop") is True) != loop:
            if cue not in skipped:
                skipped.add(cue)
                plan.warnings.append(
                    f"{cue}: the table has loop={str(existing.get('loop') is True).lower()}, the inventory "
                    f"loop={str(loop).lower()}; its items are not added (the table is kept)"
                )
            continue
        entry = table["cues"][cue]
        if variant:
            variants = entry.setdefault("variants", {})
            if variant not in variants:
                variants[variant] = {"description": ev["name"]}
                plan.added["variants"].append(g.cue)
                plan.table_changed = True
            target = variants[variant]
            if not isinstance(target, dict):
                continue
        else:
            target = entry
            if cue not in original and "description" not in target:
                table["cues"][cue] = target = {"description": ev["name"], **target}
        if "haptics" in target:
            continue  # decided already (in the table, or by an earlier item of this run)
        reason = _undecided_reason(g)
        if reason:
            plan.undecided.append({"cue": g.cue, "reason": reason})
            if ev.get("levelMap"):
                plan.warnings.append(f"{g.cue}: levelMap not written (no route)")
            if variant:
                # A variant with no haptics key would inherit the cue's route and play elsewhere.
                target["haptics"] = []
                target.setdefault("review", {}).setdefault("haptics", "tentative")
                plan.table_changed = True
            continue
        src = source_root / ev["wav"]
        clip, copy_needed = clips.name_for(g.cue.replace(":", "_"), src, g.items[0].digest)
        clips.planned.add(clip)
        table["clips"][clip] = {"clip_gain": ev.get("clip_gain", 1), "loop": loop, "description": f"from {ev['wav']}"}
        plan.added["clips"].append(clip)
        if copy_needed:
            plan.copies.append((src, clips_dir / f"{clip}.wav"))
        target["haptics"] = [_route(g, clip)]
        target.setdefault("review", {}).setdefault("haptics", "tentative")
        plan.table_changed = True
    for g in groups:
        pair = g.positions == HAND_PAIR
        for item in g.items:
            if item.event["source"] in bound and not item.event.get("cue"):
                continue  # named by its binding: the binding stays as it is (section 10.5)
            b: dict[str, Any] = {"source": item.event["source"], "cue": g.cue}
            if pair:
                b["hand"] = HAND_OF[item.position]
            plan.bindings.append(b)
    for problem in validate_cue_table(table):
        plan.errors.append(f"result: {problem}")
    return plan


def _plan_names(base: dict | None, kit: str, names: list[tuple[str, bool]]) -> Plan:
    table = copy.deepcopy(base) if base is not None else new_table(kit)
    table.setdefault("cues", {})
    plan = Plan(table=table, table_changed=base is None)
    for name, loop in names:
        existing = table["cues"].get(name)
        if existing is None:
            table["cues"][name] = {"loop": True} if loop else {}
            plan.added["cues"].append(name)
            plan.undecided.append({"cue": name, "reason": "new cue: sfx and haptics not decided"})
            plan.table_changed = True
        elif isinstance(existing, dict) and (existing.get("loop") is True) != loop:
            plan.warnings.append(
                f"{name}: the table has loop={str(existing.get('loop') is True).lower()}, asked "
                f"loop={str(loop).lower()} (the table is kept)"
            )
    for problem in validate_cue_table(table):
        plan.errors.append(f"result: {problem}")
    return plan


def plan_add(base: dict | None, kit: str, add: list[str], loop: list[str]) -> Plan:
    """``--add A,B --loop C``: empty (undecided) cues; the ``--loop`` ones get ``loop: true``."""
    names = [(n, n in loop) for n in add] + [(n, True) for n in loop if n not in add]
    return _plan_names(base, kit, names)


def plan_session(base: dict | None, session: dict, kit: str) -> Plan:
    """``--from-session``: Studio's ``newCueTable`` (every cue the recording fires, ``:variant`` left out,
    and the lib's layer cues as loop cues), sorted."""
    lib = session.get("lib", {})
    loops = {layer["cue"] for layer in lib.get("layers", []) if isinstance(layer, dict) and "cue" in layer}
    fired = {e["cue"].split(":")[0] for e in session.get("events", []) if isinstance(e, dict) and isinstance(e.get("cue"), str)}
    names = sorted(n for n in fired | loops if CUE_NAME.match(n))
    return _plan_names(base, kit, [(n, n in loops) for n in names])


def write_plan(plan: Plan, table_path: Path) -> None:
    """Copy the new clips' WAVs (never over a file) and write the table when it changed."""
    for src, dst in plan.copies:
        if dst.exists():
            raise CueInitError(f"refusing to overwrite {dst}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    if plan.table_changed:
        table_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(table_path, plan.table)
