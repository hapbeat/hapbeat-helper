"""Cue table checks (``hapbeat-helper cues validate``).

The cue table maps game events (cues) to a sound effect and haptic routes. Its
structure is ``hapbeat-contracts`` ``schemas/cue-table.schema.json`` and the
cross-field rules are ``specs/cue-table.md`` section 6; this module checks
both with the standard library only (no jsonschema dependency), so Studio, the
game's import step and this CLI agree on what a valid table is.

``validate_cue_table`` returns every problem as one line (empty = valid). The
WAV checks (section 6 rules 2, 3, 5) run only when the clip / sound folders are
given.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

CUE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
VARIANT_NAME = CUE_NAME
CLIP_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
SOUND_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
KIT_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
CUE_REF = re.compile(r"^[a-z][a-z0-9_]*(:[a-z][a-z0-9_]*)?$")

# cue-table section 4.5 (device-addressing section 3 positions).
AT_VALUES = (
    "hand", "both", "pos_neck", "pos_chest", "pos_abd", "pos_l_arm", "pos_r_arm",
    "pos_l_wrist", "pos_r_wrist", "pos_hip", "pos_l_thigh", "pos_r_thigh",
    "pos_l_ankle", "pos_r_ankle",
)
CURVES = ("linear", "easeIn", "easeOut", "easeInOut", "sigmoid")
PICK_MODES = ("random", "roundRobin")
REVIEW_STATES = ("tentative", "approved")
SYNC_MAX_MS = 600000
VARIATION_RANGES = {"gainJitterDb": (0, 12), "pitchJitterSt": (0, 12), "rateJitterPct": (0, 50)}
LEVEL_MAP_KEYS = ("points", "intercept", "curve")
LEVEL_MAP_MAX_POINTS = 16
EMIT_KEYS = ("during", "intervalSec", "jitterPct")
LEVEL_MAP_RULE = (
    "{points: [[input > 0, output 0..4], ...] (1-16, inputs increasing), "
    "intercept?: 0..4, curve?: " + " | ".join(CURVES) + "}"
)


def is_number(value: Any) -> bool:
    """A JSON number (bool is not one, nor NaN / infinity)."""
    return (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(value)
    )


def in_range(value: Any, lo: float, hi: float) -> bool:
    return is_number(value) and lo <= value <= hi


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _non_empty_list(value: Any) -> bool:
    return isinstance(value, list) and len(value) > 0


def is_level_map(value: Any) -> bool:
    """cue-table section 5.6: points 1-16, inputs > 0 increasing, outputs 0..4."""
    if not _is_record(value) or any(k not in LEVEL_MAP_KEYS for k in value):
        return False
    points = value.get("points")
    if not isinstance(points, list) or not 1 <= len(points) <= LEVEL_MAP_MAX_POINTS:
        return False
    last = 0.0
    for p in points:
        if not isinstance(p, list) or len(p) != 2 or not is_number(p[0]) or not is_number(p[1]):
            return False
        if p[0] <= last or not in_range(p[1], 0, 4):
            return False
        last = p[0]
    if "intercept" in value and not in_range(value["intercept"], 0, 4):
        return False
    return "curve" not in value or value["curve"] in CURVES


def _sfx_sounds(sfx: Any) -> list:
    """The starred sounds of a sfx (one for ``sound``)."""
    if not _is_record(sfx):
        return []
    if isinstance(sfx.get("sounds"), list):
        return sfx["sounds"]
    return [sfx["sound"]] if "sound" in sfx else []


def _route_clips(route: Any) -> list:
    """The starred clips of a route (one for ``clip``)."""
    if not _is_record(route):
        return []
    if isinstance(route.get("clips"), list):
        return route["clips"]
    return [route["clip"]] if "clip" in route else []


def _alternates(entry: Any) -> list:
    alts = entry.get("alternates") if _is_record(entry) else None
    return alts if isinstance(alts, list) else []


def paired_problem(sfx: Any, routes: list) -> str | None:
    """Why the sounds and clips cannot be paired (None when they line up).

    Every route needs as many starred clips as there are starred sounds. A sfx
    or route with nothing starred (only alternates) plays nothing and is left
    out; with no starred sound each route picks on its own.
    """
    n = len(_sfx_sounds(sfx))
    if not n:
        return None if _alternates(sfx) else "there is no sound to pair"
    bad = [
        (i, len(_route_clips(r))) for i, r in enumerate(routes)
        if len(_route_clips(r)) not in (0, n)
    ]
    if not bad:
        return None
    return "; ".join(f"route {i + 1} has {count} clip(s) for {n} sound(s)" for i, count in bad)


class _Checker:
    """Collects the problems of one table (``validate_cue_table``)."""

    def __init__(self, table: dict, clips_dir: Path | None, sounds_dir: Path | None) -> None:
        self.table = table
        self.clips_dir = clips_dir
        self.sounds_dir = sounds_dir
        self.err: list[str] = []
        clips = table.get("clips")
        self.clips: dict = clips if _is_record(clips) else {}
        cues = table.get("cues")
        self.cues: dict = cues if _is_record(cues) else {}

    def add(self, message: str) -> None:
        self.err.append(message)

    # ── helpers ──

    def is_loop_cue(self, name: str) -> bool:
        cue = self.cues.get(name)
        return _is_record(cue) and cue.get("loop") is True

    def sound_wav_missing(self, sound: str) -> bool:
        return self.sounds_dir is not None and not (self.sounds_dir / f"{sound}.wav").is_file()

    def sound_wav(self) -> str:
        return self.sounds_dir.as_posix() if self.sounds_dir is not None else ""

    # ── top level ──

    def run(self) -> list[str]:
        t = self.table
        if not _is_record(t):
            return ["cue table must be a JSON object"]
        if t.get("schema_version") != 1 or isinstance(t.get("schema_version"), bool):
            self.add("schema_version must be 1")
        # Rule 1.
        kit = t.get("kit")
        if not isinstance(kit, str) or not KIT_NAME.match(kit):
            self.add(f"kit must be a Kit name matching {KIT_NAME.pattern}")
        if not _is_record(t.get("clips")):
            self.add("clips must be an object")
        else:
            for name, clip in self.clips.items():
                self.check_clip(name, clip)
        if "sounds" in t:
            if not _is_record(t["sounds"]):
                self.add("sounds must be an object")
            else:
                for name, entry in t["sounds"].items():
                    self.check_sound(name, entry)
        if not _is_record(t.get("cues")):
            self.add("cues must be an object")
        else:
            for name, cue in self.cues.items():
                self.check_cue(name, cue)
        return self.err

    def check_clip(self, name: str, clip: Any) -> None:
        # Rule 2.
        if not CLIP_NAME.match(name):
            self.add(f"clip name {name} must match {CLIP_NAME.pattern}")
        if not _is_record(clip):
            self.add(f"clip {name} must be an object")
            return
        if not in_range(clip.get("intensity"), 0, 1):
            self.add(f"clip {name}: intensity must be 0..1")
        if not isinstance(clip.get("loop"), bool):
            self.add(f"clip {name}: loop must be true/false")
        if "description" in clip and not isinstance(clip["description"], str):
            self.add(f"clip {name}: description must be a string")
        if "syncMs" in clip and not in_range(clip["syncMs"], 0, SYNC_MAX_MS):
            self.add(f"clip {name}: syncMs must be 0..{SYNC_MAX_MS}")
        if self.clips_dir is not None and not (self.clips_dir / f"{name}.wav").is_file():
            self.add(f"clip {name}: {self.clips_dir.as_posix()}/{name}.wav missing")

    def check_sound(self, name: str, entry: Any) -> None:
        # Rule 3.
        if not SOUND_NAME.match(name):
            self.add(f"sound name {name} must match {SOUND_NAME.pattern}")
        if not _is_record(entry):
            self.add(f"sound {name} must be an object")
        else:
            if not in_range(entry.get("intensity"), 0, 1):
                self.add(f"sound {name}: intensity must be 0..1")
            if "syncMs" in entry and not in_range(entry["syncMs"], 0, SYNC_MAX_MS):
                self.add(f"sound {name}: syncMs must be 0..{SYNC_MAX_MS}")
        if self.sound_wav_missing(name):
            self.add(f"sound {name}: {self.sound_wav()}/{name}.wav missing")

    # ── cues ──

    def check_cue(self, name: str, cue: Any) -> None:
        # Rule 4.
        if not CUE_NAME.match(name):
            self.add(f"cue name {name} must match {CUE_NAME.pattern}")
        if not _is_record(cue):
            self.add(f"cue {name} must be an object")
            return
        if "loop" in cue and not isinstance(cue["loop"], bool):
            self.add(f"{name}: loop must be true/false")
        self.check_fields(name, name, cue)
        if "emit" in cue:
            self.check_emit(name, cue["emit"])
        if "variants" in cue:
            variants = cue["variants"]
            if not _is_record(variants):
                self.add(f"{name}: variants must be an object")
                return
            for vn, variant in variants.items():
                label = f"{name}:{vn}"
                if not VARIANT_NAME.match(vn):
                    self.add(f"{name}: variant name {vn} must match {VARIANT_NAME.pattern}")
                if not _is_record(variant):
                    self.add(f"{label} must be an object")
                    continue
                for key in ("loop", "emit", "variants"):
                    if key in variant:
                        self.add(f"{label}: a variant cannot have {key} (it belongs to the cue)")
                self.check_fields(name, label, variant)
                self.check_variant_scale(label, variant)

    def check_fields(self, cue: str, label: str, entry: dict) -> None:
        """The checks of one cue or variant. ``cue`` is the cue whose loop kind applies."""
        if "description" in entry and not isinstance(entry["description"], str):
            self.add(f"{label}: description must be a string")
        if "sfx" in entry:
            sfx = entry["sfx"]
            if sfx is not None:
                if _is_record(sfx):
                    self.check_sfx(cue, label, sfx)
                else:
                    self.add(f"{label}: sfx must be an object or null")
        if "haptics" in entry:
            routes = entry["haptics"]
            if not isinstance(routes, list):
                self.add(f"{label}: haptics must be a list")
            else:
                for i, route in enumerate(routes):
                    if _is_record(route):
                        self.check_route(cue, f"{label} route {i + 1}", route)
                    else:
                        self.add(f"{label}: route {i + 1} must be an object")
        if "distanceFalloff" in entry:
            self.check_distance_falloff(label, entry["distanceFalloff"])
        if "review" in entry:
            self.check_review(label, entry["review"])
        if "preview" in entry:
            self.add(f"{label}: preview is not a field")
        if "variation" in entry:
            self.check_variation(label, entry["variation"])
        # Rule 8 (paired): on what this entry plays, inherited fields included.
        base = self.cues.get(cue) if _is_record(self.cues.get(cue)) else {}
        effective = {**base, **entry}
        variation = effective.get("variation")
        if _is_record(variation) and variation.get("paired") is True:
            routes = effective.get("haptics")
            problem = paired_problem(
                effective.get("sfx"), routes if isinstance(routes, list) else [],
            )
            if problem:
                self.add(f"{label}: variation.paired: {problem}")

    def check_sfx(self, cue: str, label: str, sfx: dict) -> None:
        # Rule 5.
        has_one, has_many = "sound" in sfx, "sounds" in sfx
        if has_one and has_many:
            self.add(f"{label}: sfx needs one of sound / sounds, not both")
        elif not has_one and not has_many and not _non_empty_list(sfx.get("alternates")):
            self.add(f"{label}: sfx needs sound / sounds, or alternates when nothing is starred")
        elif has_many and not _non_empty_list(sfx["sounds"]):
            self.add(f"{label}: sfx.sounds must be a non-empty list")
        else:
            for sound in _sfx_sounds(sfx):
                if not isinstance(sound, str) or not SOUND_NAME.match(sound):
                    self.add(f"{label}: bad sound {sound!r} (must match {SOUND_NAME.pattern})")
                elif self.sound_wav_missing(sound):
                    self.add(f"{label}: {self.sound_wav()}/{sound}.wav missing")
        if "alternates" in sfx:
            alts = sfx["alternates"]
            if not isinstance(alts, list) or any(
                not isinstance(x, str) or not SOUND_NAME.match(x) for x in alts
            ):
                self.add(f"{label}: sfx alternates must be a list of sound names")
            else:
                for x in alts:
                    if self.sound_wav_missing(x):
                        self.add(f"{label}: {self.sound_wav()}/{x}.wav missing")
                self.check_alternates(label, "sfx", alts, _sfx_sounds(sfx))
        if not in_range(sfx.get("volume"), 0, 2):
            self.add(f"{label}: sfx volume must be 0..2")
        if "levelMap" in sfx:
            self.check_level_map(cue, label, "sfx", sfx["levelMap"])

    def check_route(self, cue: str, label: str, route: dict) -> None:
        # Rule 6.
        has_one, has_many = "clip" in route, "clips" in route
        loop = self.is_loop_cue(cue)
        if has_one and has_many:
            self.add(f"{label}: needs one of clip / clips, not both")
        elif not has_one and not has_many and not _non_empty_list(route.get("alternates")):
            self.add(f"{label}: needs clip / clips, or alternates when nothing is starred")
        elif has_many and not _non_empty_list(route["clips"]):
            self.add(f"{label}: clips must be a non-empty list")
        else:
            for clip in _route_clips(route):
                if not isinstance(clip, str) or clip not in self.clips:
                    self.add(f"{label}: unknown clip {clip!r}")
                elif _is_record(self.clips[clip]) and self.clips[clip].get("loop") is not loop:
                    self.add(
                        f"{label}: clip {clip} loop={str(self.clips[clip].get('loop')).lower()} "
                        f"does not fit this {'loop' if loop else 'one-shot'} cue"
                    )
        if "alternates" in route:
            alts = route["alternates"]
            if not isinstance(alts, list) or any(
                not isinstance(x, str) or x not in self.clips for x in alts
            ):
                self.add(f"{label}: alternates must be clip names of this table")
            else:
                self.check_alternates(label, "route", alts, _route_clips(route))
        if route.get("at") not in AT_VALUES:
            self.add(f"{label}: at must be one of {', '.join(AT_VALUES)}")
        if not in_range(route.get("gain"), 0, 2):
            self.add(f"{label}: gain must be 0..2")
        if "levelMap" in route:
            self.check_level_map(cue, label, "route", route["levelMap"])

    def check_alternates(self, label: str, kind: str, alts: list, starred: list) -> None:
        twice = sorted({x for i, x in enumerate(alts) if alts.index(x) != i})
        both = [x for x in alts if x in starred]
        if twice:
            self.add(f"{label}: {kind} alternates list {', '.join(twice)} twice")
        if both:
            self.add(f"{label}: {kind} alternates {', '.join(both)} are also starred")

    def check_level_map(self, cue: str, label: str, kind: str, value: Any) -> None:
        # Rule 7.
        if not self.is_loop_cue(cue):
            self.add(f"{label}: {kind} levelMap is for loop cues")
        elif not is_level_map(value):
            self.add(f"{label}: {kind} levelMap must be {LEVEL_MAP_RULE}")

    def check_variation(self, label: str, v: Any) -> None:
        # Rule 8 (ranges).
        if not _is_record(v):
            self.add(f"{label}: variation must be an object")
            return
        for key, (lo, hi) in VARIATION_RANGES.items():
            if key in v and not in_range(v[key], lo, hi):
                self.add(f"{label}: variation.{key} must be {lo}..{hi}")
        if "pick" in v and v["pick"] not in PICK_MODES:
            self.add(f"{label}: variation.pick must be {' or '.join(PICK_MODES)}")
        if "paired" in v and not isinstance(v["paired"], bool):
            self.add(f"{label}: variation.paired must be true/false")

    def check_variant_scale(self, label: str, v: dict) -> None:
        # Rule 9.
        for key in ("sfxVolume", "hapticsGain", "rampTo"):
            if key in v and not in_range(v[key], 0, 2):
                self.add(f"{label}: {key} must be 0..2")
        if "sfxVolume" in v and "sfx" in v:
            self.add(f"{label}: sfxVolume is for an inherited sfx (this variant has its own sfx)")
        if "hapticsGain" in v and "haptics" in v:
            self.add(f"{label}: hapticsGain is for inherited haptics (this variant has its own haptics)")
        if "rampTo" in v and "sfx" in v and "haptics" in v:
            self.add(f"{label}: rampTo needs an inherited sfx or haptics")
        if "rampCurve" in v:
            if v["rampCurve"] not in CURVES:
                self.add(f"{label}: rampCurve must be one of {', '.join(CURVES)}")
            if "rampTo" not in v:
                self.add(f"{label}: rampCurve needs rampTo")
        if "rampSteps" in v:
            steps = v["rampSteps"]
            if (
                not isinstance(steps, list) or not 1 <= len(steps) <= 64
                or any(not in_range(x, 0, 2) for x in steps)
            ):
                self.add(f"{label}: rampSteps must be 1-64 numbers 0..2")
            if "rampTo" in v or "rampCurve" in v:
                self.add(f"{label}: rampSteps replaces rampTo / rampCurve (not both)")
            if "sfx" in v and "haptics" in v:
                self.add(f"{label}: rampSteps needs an inherited sfx or haptics")

    def check_distance_falloff(self, label: str, df: Any) -> None:
        # Rule 10.
        if df is None:
            return
        if not _is_record(df):
            self.add(f"{label}: distanceFalloff must be an object or null")
            return
        near, far = df.get("nearCm"), df.get("farCm")
        if not (is_number(near) and is_number(far) and 0 <= near < far):
            self.add(f"{label}: distanceFalloff needs 0 <= nearCm < farCm")
        if not in_range(df.get("farGain"), 0, 2):
            self.add(f"{label}: distanceFalloff.farGain must be 0..2")
        if "curve" in df and df["curve"] not in CURVES:
            self.add(f"{label}: distanceFalloff.curve must be one of {', '.join(CURVES)}")

    def check_emit(self, name: str, emit: Any) -> None:
        # Rule 11.
        rule = "emit must be {during: <loop cue>, intervalSec: 0.05..30, jitterPct?: 0..100}"
        if self.is_loop_cue(name):
            self.add(f"{name}: emit is for one-shot cues (this cue has loop: true)")
            return
        if not _is_record(emit) or any(k not in EMIT_KEYS for k in emit):
            self.add(f"{name}: {rule}")
            return
        during = emit.get("during")
        if not isinstance(during, str) or not self.is_loop_cue(during):
            self.add(f"{name}: emit.during must be a loop cue of this table")
        if not in_range(emit.get("intervalSec"), 0.05, 30):
            self.add(f"{name}: emit.intervalSec must be 0.05..30")
        if "jitterPct" in emit and not in_range(emit["jitterPct"], 0, 100):
            self.add(f"{name}: emit.jitterPct must be 0..100")

    def check_review(self, label: str, review: Any) -> None:
        # Rule 12.
        if not _is_record(review):
            self.add(f"{label}: review must be an object")
            return
        for key, state in review.items():
            if key not in ("sfx", "haptics"):
                self.add(f"{label}: review.{key} is not a field (sfx / haptics)")
            elif state not in REVIEW_STATES:
                self.add(f"{label}: review.{key} must be {' or '.join(REVIEW_STATES)}")


def validate_cue_table(
    table: Any, clips_dir: Path | None = None, sounds_dir: Path | None = None,
) -> list[str]:
    """Every problem of ``table`` (cue-table schema + section 6), one line each.

    ``clips_dir`` / ``sounds_dir``: the folders of ``<clip>.wav`` / ``<Sound>.wav``;
    the WAV checks are skipped for a folder that is None.
    """
    return _Checker(table, clips_dir, sounds_dir).run()


def load_json(path: Path) -> Any:
    """Read a UTF-8 JSON file (a BOM is tolerated)."""
    return json.loads(path.read_text(encoding="utf-8-sig"))
