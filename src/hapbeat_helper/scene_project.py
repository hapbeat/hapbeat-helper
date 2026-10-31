"""A game project's ``hapbeat-scene.json`` (``hapbeat-contracts`` ``specs/scene-recording.md`` section 6).

The file sits at the project root and is kept in version control. It holds what does not change
between recordings: fps, where the screen and the log are, the viewer-lib (``lib``) and the
``bindings`` from an SDK entry (``source``) to a cue. ``hapbeat-helper scene session`` turns the
recording's log (section 5) into ``session.json`` with it; ``hapbeat-helper cues init`` writes its
``bindings``.

Checks use the standard library only (no jsonschema dependency).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from hapbeat_helper.cue_table import CUE_REF, in_range
from hapbeat_helper.scene_build import HANDS, LEVEL_COLUMN, PROJECT_NAME, validate_viewer_lib, write_json

CONFIG_NAME = "hapbeat-scene.json"
DEFAULT_LOG = "Saved/HapticRecord/events.jsonl"
DEFAULT_FRAMES = "Saved/HapticRecord/frames"
CAPTURE_MODES = ("game", "sdk", "both")
# <engine>:<id> (section 5.2), e.g. unity:ce65e997e9d54d2eabde0d646dea67c7
SOURCE = re.compile(r"^[a-z][a-z0-9_-]*:\S+$")


class SceneProjectError(Exception):
    """A hapbeat-scene.json that cannot be read."""


def validate_project(cfg: Any) -> list[str]:
    """Problems of a hapbeat-scene.json, one line each."""
    if not isinstance(cfg, dict):
        return [f"{CONFIG_NAME} must be a JSON object"]
    err: list[str] = []
    if cfg.get("schema_version") != 1 or isinstance(cfg.get("schema_version"), bool):
        err.append("schema_version must be 1")
    project = cfg.get("project")
    if not isinstance(project, str) or not PROJECT_NAME.match(project):
        err.append(f"project must match {PROJECT_NAME.pattern}")
    if not in_range(cfg.get("fps"), 0, 240) or cfg["fps"] <= 0:
        err.append("fps must be a number > 0 and <= 240")
    if ("frames" in cfg) == ("video" in cfg):
        err.append("exactly one of frames / video is needed")
    for key in ("frames", "video", "log"):
        if key in cfg and not (isinstance(cfg[key], str) and cfg[key]):
            err.append(f"{key} must be a non-empty path")
    if "capture" in cfg and cfg["capture"] not in CAPTURE_MODES:
        err.append(f"capture must be one of {', '.join(CAPTURE_MODES)}")
    if "columns" in cfg:
        columns = cfg["columns"]
        if (
            not isinstance(columns, list)
            or not all(isinstance(c, str) and LEVEL_COLUMN.match(c) for c in columns)
            or len(set(columns)) != len(columns)
        ):
            err.append("columns must be a list of distinct names <cue>:level|rate[:left|right]")
    if "lib" not in cfg:
        err.append("lib is required")
    else:
        err += validate_viewer_lib(cfg["lib"])
        if isinstance(cfg["lib"], dict) and isinstance(project, str) and cfg["lib"].get("project_name") != project:
            err.append("lib.project_name must be the same as project")
    bindings = cfg.get("bindings", [])
    if not isinstance(bindings, list):
        err.append("bindings must be a list")
    else:
        seen: set[str] = set()
        for i, b in enumerate(bindings):
            label = f"bindings[{i}]"
            if not isinstance(b, dict):
                err.append(f"{label} must be an object")
                continue
            source = b.get("source")
            if not isinstance(source, str) or not SOURCE.match(source):
                err.append(f"{label}.source must be <engine>:<id>")
            elif source in seen:
                err.append(f"{label}.source {source} is bound twice")
            else:
                seen.add(source)
            if not isinstance(b.get("cue"), str) or not CUE_REF.match(b["cue"]):
                err.append(f"{label}.cue must be <cue> or <cue>:<variant>")
            if "hand" in b and b["hand"] not in HANDS:
                err.append(f"{label}.hand must be one of {', '.join(HANDS)}")
    return err


def load_project(path: Path) -> dict:
    """Read and check a hapbeat-scene.json; raises SceneProjectError with every problem."""
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SceneProjectError(f"cannot read {path}: {exc}") from exc
    problems = validate_project(cfg)
    if problems:
        raise SceneProjectError(f"invalid {path}:\n" + "\n".join(f"  {p}" for p in problems))
    return cfg


def save_project(path: Path, cfg: dict) -> None:
    """Write a hapbeat-scene.json (UTF-8, 2-space indent, trailing newline). Checks it first."""
    problems = validate_project(cfg)
    if problems:
        raise SceneProjectError(f"refusing to write an invalid {path}:\n" + "\n".join(f"  {p}" for p in problems))
    write_json(Path(path), cfg)


def new_project(project: str, title: str, cues: str, clips: str, sounds: str, fps: float = 30) -> dict:
    """A minimal hapbeat-scene.json: frames and log at their defaults, no bindings yet."""
    return {
        "schema_version": 1,
        "project": project,
        "fps": fps,
        "frames": DEFAULT_FRAMES,
        "log": DEFAULT_LOG,
        "lib": {
            "schema_version": 1,
            "title": title,
            "project_name": project,
            "paths": {"cues": cues, "clips": clips, "sounds": sounds},
        },
        "bindings": [],
    }


def set_bindings(cfg: dict, bindings: list[dict]) -> None:
    """Add bindings; one already there for the same source is replaced in place (section 10.5 of cue-table.md)."""
    current = cfg.setdefault("bindings", [])
    index = {b.get("source"): i for i, b in enumerate(current) if isinstance(b, dict)}
    for b in bindings:
        if b["source"] in index:
            current[index[b["source"]]] = b
        else:
            index[b["source"]] = len(current)
            current.append(b)


def binding_map(cfg: dict) -> dict[str, dict]:
    """``{source: {cue, hand?}}``"""
    return {b["source"]: b for b in cfg.get("bindings", [])}
