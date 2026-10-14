"""Scene recording -> what Hapbeat Studio's Scene tab reads (``hapbeat-helper scene build``).

Input is the recording's ``session.json`` (``hapbeat-contracts``
``specs/scene-recording.md`` section 2) plus its screen (``f<frame>.png`` frames
or a constant-fps video). Output goes to ``<project>/Saved/HapticViewer/``
(section 3): ``full_replay.mp4`` (H.264 yuv420p, constant ``fps``, no audio),
``viewer-data.json`` and ``viewer-lib.json``. Other files there
(``studio-scene-ui.json`` ...) are left alone.

Checks use the standard library only (no jsonschema dependency).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hapbeat_helper.cue_table import AT_VALUES, CUE_NAME, CUE_REF, in_range, is_number

VIEWER_DIR = Path("Saved") / "HapticViewer"
VIDEO_NAME = "full_replay.mp4"
PROJECT_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
LEVEL_COLUMN = re.compile(r"^[a-z][a-z0-9_]*:(level|rate)(:(left|right))?$")
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
HANDS = ("left", "right", "both")
FRAME_FILE = re.compile(r"^f(\d+)\.png$")
# Event fields copied as they are into viewer-data (section 3.2).
EVENT_EXTRAS = ("hand", "gain", "dist", "note")


class SceneBuildError(Exception):
    """A build that cannot go on (bad session, missing frames, ffmpeg failure)."""


def _is_record(value: Any) -> bool:
    return isinstance(value, dict)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 0


def _string_list(value: Any, pattern: re.Pattern | None = None) -> bool:
    return isinstance(value, list) and all(
        isinstance(x, str) and (pattern is None or pattern.match(x)) for x in value
    )


def _pair(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and all(_is_int(x) and x >= 0 for x in value)


# ── checks ───────────────────────────────────────────────────


def validate_viewer_lib(lib: Any, label: str = "lib") -> list[str]:
    """Problems of a viewer-lib.json (scene-viewer-lib schema), one line each."""
    if not _is_record(lib):
        return [f"{label} must be an object"]
    err: list[str] = []
    if lib.get("schema_version") != 1 or isinstance(lib.get("schema_version"), bool):
        err.append(f"{label}.schema_version must be 1")
    if not _non_empty_str(lib.get("title")):
        err.append(f"{label}.title must be a non-empty string")
    name = lib.get("project_name")
    if not isinstance(name, str) or not PROJECT_NAME.match(name):
        err.append(f"{label}.project_name must match {PROJECT_NAME.pattern}")
    paths = lib.get("paths")
    if not _is_record(paths):
        err.append(f"{label}.paths must be an object")
    else:
        for key in ("cues", "clips", "sounds"):
            if not _non_empty_str(paths.get(key)):
                err.append(f"{label}.paths.{key} must be a non-empty string")
    for key in ("at", "loop_at"):
        if key in lib:
            value = lib[key]
            if (
                not isinstance(value, list) or not value
                or any(x not in AT_VALUES for x in value) or len(set(map(str, value))) != len(value)
            ):
                err.append(f"{label}.{key} must be a non-empty list of distinct {', '.join(AT_VALUES)}")
    if "families" in lib:
        families = lib["families"]
        if not isinstance(families, list):
            err.append(f"{label}.families must be a list")
        else:
            for i, fam in enumerate(families):
                if (
                    not _is_record(fam) or not isinstance(fam.get("label"), str)
                    or not isinstance(fam.get("color"), str) or not COLOR.match(fam["color"])
                    or not _string_list(fam.get("cues"), CUE_REF)
                ):
                    err.append(
                        f"{label}.families[{i}] must be {{label, color: #rrggbb, cues: [<cue>[:<variant>]...]}}"
                    )
    if "ticks" in lib and not _string_list(lib["ticks"], CUE_NAME):
        err.append(f"{label}.ticks must be a list of cue names")
    if "layers" in lib:
        layers = lib["layers"]
        if not isinstance(layers, list):
            err.append(f"{label}.layers must be a list")
        else:
            for i, layer in enumerate(layers):
                if (
                    not _is_record(layer) or not isinstance(layer.get("cue"), str)
                    or not CUE_NAME.match(layer["cue"]) or not _pair(layer.get("level"))
                    or not (layer.get("rate") is None or _pair(layer["rate"]))
                    or not _string_list(layer.get("colors"))
                ):
                    err.append(
                        f"{label}.layers[{i}] must be {{cue, level: [l, r], rate?: [l, r] | null, colors: [...]}}"
                    )
    if "moment_rules" in lib:
        rules = lib["moment_rules"]
        ok = _is_record(rules)
        if ok and "max" in rules:
            ok = _is_record(rules["max"]) and all(_is_int(v) and v >= 1 for v in rules["max"].values())
        if ok and "min_gap_s" in rules:
            ok = _is_record(rules["min_gap_s"]) and all(
                is_number(v) and v >= 0 for v in rules["min_gap_s"].values()
            )
        if ok and "default_max" in rules:
            ok = rules["default_max"] is None or (_is_int(rules["default_max"]) and rules["default_max"] >= 1)
        if not ok:
            err.append(
                f"{label}.moment_rules must be {{max?: {{cue: n >= 1}}, min_gap_s?: {{cue: s >= 0}}, "
                "default_max?: n >= 1 | null}"
            )
    for key in ("import_command", "record_command"):
        if key in lib and not isinstance(lib[key], str):
            err.append(f"{label}.{key} must be a string")
    return err


def validate_levels(levels: Any, label: str = "levels") -> list[str]:
    """``{columns, rows}`` (section 2.1): column names unique, every row as wide as the columns, values >= 0."""
    if not _is_record(levels):
        return [f"{label} must be an object"]
    err: list[str] = []
    columns = levels.get("columns")
    if (
        not isinstance(columns, list) or not columns
        or not _string_list(columns, LEVEL_COLUMN) or len(set(columns)) != len(columns)
    ):
        err.append(f"{label}.columns must be distinct names <cue>:level|rate[:left|right]")
        width = None
    else:
        width = len(columns)
    rows = levels.get("rows")
    if not isinstance(rows, list):
        err.append(f"{label}.rows must be a list")
        return err
    for i, row in enumerate(rows):
        if not isinstance(row, list) or any(not is_number(x) or x < 0 for x in row):
            err.append(f"{label}.rows[{i}] must be a list of numbers >= 0")
        elif width is not None and len(row) != width:
            err.append(f"{label}.rows[{i}] has {len(row)} values for {width} columns")
    return err


def validate_session(session: Any) -> list[str]:
    """Problems of a session.json (scene-session schema), one line each."""
    if not _is_record(session):
        return ["session must be a JSON object"]
    err: list[str] = []
    if session.get("schema_version") != 1 or isinstance(session.get("schema_version"), bool):
        err.append("schema_version must be 1")
    project = session.get("project")
    if not isinstance(project, str) or not PROJECT_NAME.match(project):
        err.append(f"project must match {PROJECT_NAME.pattern}")
    if not in_range(session.get("fps"), 0, 240) or session["fps"] <= 0:
        err.append("fps must be a number > 0 and <= 240")
    has_frames, has_video = "frames" in session, "video" in session
    if has_frames == has_video:
        err.append("exactly one of frames / video is needed")
    for key in ("frames", "video"):
        if key in session and not _non_empty_str(session[key]):
            err.append(f"{key} must be a non-empty path")
    events = session.get("events")
    if not isinstance(events, list):
        err.append("events must be a list")
    else:
        for i, e in enumerate(events):
            label = f"events[{i}]"
            if not _is_record(e):
                err.append(f"{label} must be an object")
                continue
            if not _is_int(e.get("frame")) or e["frame"] < 0:
                err.append(f"{label}.frame must be an integer >= 0")
            if not isinstance(e.get("cue"), str) or not CUE_REF.match(e["cue"]):
                err.append(f"{label}.cue must be <cue> or <cue>:<variant>")
            if "hand" in e and e["hand"] not in HANDS:
                err.append(f"{label}.hand must be one of {', '.join(HANDS)}")
            for key in ("gain", "dist"):
                if key in e and not (is_number(e[key]) and e[key] >= 0):
                    err.append(f"{label}.{key} must be a number >= 0")
            if "note" in e and not isinstance(e["note"], str):
                err.append(f"{label}.note must be a string")
    if "levels" in session:
        err += validate_levels(session["levels"])
    if "lib" not in session:
        err.append("lib is required")
    else:
        err += validate_viewer_lib(session["lib"])
    return err


# ── viewer-data ──────────────────────────────────────────────


def pad_levels(levels: dict, frame_count: int) -> dict:
    """``levels`` with exactly ``frame_count`` rows: missing rows are 0, extra rows dropped."""
    width = len(levels["columns"])
    rows = [list(r) for r in levels["rows"][:frame_count]]
    rows += [[0] * width for _ in range(frame_count - len(rows))]
    return {"columns": list(levels["columns"]), "rows": rows}


def build_viewer_data(session: dict, frame_count: int) -> dict:
    """viewer-data.json (section 3.2) for a video of ``frame_count`` frames."""
    fps = session["fps"]
    events = []
    for e in session["events"]:
        out = {"t": round(e["frame"] / fps, 4), "name": e["cue"]}
        out.update({k: e[k] for k in EVENT_EXTRAS if k in e})
        events.append(out)
    data: dict = {
        "schema_version": 1,
        "fps": fps,
        "frames": frame_count,
        "video": VIDEO_NAME,
        "events": events,
    }
    if "levels" in session:
        data["levels"] = pad_levels(session["levels"], frame_count)
    return data


def write_json(path: Path, obj: Any) -> None:
    """UTF-8, 2-space indent, trailing newline (as Studio writes)."""
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


# ── video ────────────────────────────────────────────────────


def find_frames(folder: Path) -> dict[int, Path]:
    """``{frame: path}`` of the ``f<frame>.png`` files in ``folder``."""
    frames: dict[int, Path] = {}
    for p in folder.iterdir():
        m = FRAME_FILE.match(p.name)
        if m and p.is_file():
            frames[int(m.group(1))] = p
    return frames


def _gop(fps: float) -> int:
    """A keyframe every 0.5 s: quick seeking in Studio."""
    return max(1, round(fps / 2))


def _run_ffmpeg(cmd: list[str]) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SceneBuildError(f"ffmpeg could not be run ({cmd[0]}): {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip().splitlines()[-5:]
        raise SceneBuildError(
            f"ffmpeg failed (exit {proc.returncode}): " + " / ".join(detail or ["no output"])
        )


def encode_frames(ffmpeg: str, frames: dict[int, Path], fps: float, out: Path, workdir: Path) -> int:
    """PNG frames -> mp4, frame i at i / fps. A missing frame repeats the previous one
    (frames before the first file repeat the first). Returns the number of frames written."""
    count = max(frames) + 1
    lst = workdir / "full_replay.frames.txt"
    prev = frames[min(frames)]
    lines = []
    for n in range(count):
        prev = frames.get(n, prev)
        lines.append(f"file '{prev.resolve().as_posix()}'\nduration {1 / fps:.6f}\n")
    lst.write_text("".join(lines), encoding="utf-8")
    try:
        _run_ffmpeg([
            ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
            "-an", "-vf", "format=yuv420p", "-r", str(fps), "-c:v", "libx264", "-crf", "20",
            "-g", str(_gop(fps)), "-movflags", "+faststart", str(out),
        ])
    finally:
        lst.unlink(missing_ok=True)
    return count


def encode_video(ffmpeg: str, video: Path, fps: float, out: Path) -> None:
    """A recorded video -> mp4 at constant ``fps`` (frame 0 = its first frame), no audio."""
    _run_ffmpeg([
        ffmpeg, "-y", "-nostdin", "-loglevel", "error", "-i", str(video),
        "-map", "0:v:0", "-an", "-vf", "format=yuv420p", "-r", str(fps), "-c:v", "libx264", "-crf", "20",
        "-g", str(_gop(fps)), "-movflags", "+faststart", str(out),
    ])


def count_video_frames(ffmpeg: str, video: Path) -> int:
    """The number of video frames in ``video`` (stream copy to null, last ``frame=`` of the stats)."""
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-nostdin", "-i", str(video), "-map", "0:v:0", "-c", "copy", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        raise SceneBuildError(f"ffmpeg could not be run ({ffmpeg}): {exc}") from exc
    found = re.findall(r"frame=\s*(\d+)", proc.stderr)
    if proc.returncode != 0 or not found:
        raise SceneBuildError(f"could not count the frames of {video}")
    return int(found[-1])


def resolve_ffmpeg(ffmpeg: str | None) -> str:
    exe = shutil.which(ffmpeg or "ffmpeg")
    if exe is None:
        raise SceneBuildError(
            f"ffmpeg not found: {ffmpeg or 'ffmpeg'} (install ffmpeg with libx264 and put it on PATH, or pass --ffmpeg)"
        )
    return exe


# ── build ────────────────────────────────────────────────────


@dataclass
class BuildResult:
    out_dir: Path
    frames: int
    events: int
    warnings: list[str] = field(default_factory=list)


def build(
    session_path: Path, project_root: Path, ffmpeg: str | None = None, keep_frames: bool = False,
) -> BuildResult:
    """session.json -> ``<project_root>/Saved/HapticViewer/`` (full_replay.mp4, viewer-data.json, viewer-lib.json)."""
    try:
        session = json.loads(Path(session_path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise SceneBuildError(f"cannot read {session_path}: {exc}") from exc
    problems = validate_session(session)
    if problems:
        raise SceneBuildError("invalid session:\n" + "\n".join(f"  {p}" for p in problems))

    fps = session["fps"]
    frames_dir = video_in = None
    frames: dict[int, Path] = {}
    if "frames" in session:
        frames_dir = project_root / session["frames"]
        if not frames_dir.is_dir():
            raise SceneBuildError(f"frames folder not found: {frames_dir}")
        frames = find_frames(frames_dir)
        if not frames:
            raise SceneBuildError(f"no f<frame>.png in {frames_dir}")
    else:
        video_in = project_root / session["video"]
        if not video_in.is_file():
            raise SceneBuildError(f"video not found: {video_in}")
    exe = resolve_ffmpeg(ffmpeg)

    out_dir = project_root / VIEWER_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    # Encode next to the target and swap in at the end: a failed run leaves the old video.
    tmp = out_dir / "full_replay.tmp.mp4"
    try:
        if frames_dir is not None:
            expected = encode_frames(exe, frames, fps, tmp, out_dir)
        else:
            encode_video(exe, video_in, fps, tmp)
            expected = None
        count = count_video_frames(exe, tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    warnings: list[str] = []
    if expected is not None and count != expected:
        warnings.append(f"the video has {count} frames for {expected} input frames")
    if count < 1:
        tmp.unlink(missing_ok=True)
        raise SceneBuildError("the encoded video has no frames")
    try:
        os.replace(tmp, out_dir / VIDEO_NAME)
    except PermissionError as exc:
        tmp.unlink(missing_ok=True)
        raise SceneBuildError(
            f"{VIDEO_NAME} is open in another program (a video player?): close it and run again"
        ) from exc

    late = sum(1 for e in session["events"] if e["frame"] >= count)
    if late:
        warnings.append(f"{late} event(s) at or after frame {count} (past the end of the video)")
    if "levels" in session and len(session["levels"]["rows"]) != count:
        warnings.append(
            f"levels has {len(session['levels']['rows'])} rows for {count} frames (padded with 0 / cut)"
        )
    write_json(out_dir / "viewer-data.json", build_viewer_data(session, count))
    write_json(out_dir / "viewer-lib.json", session["lib"])
    if frames_dir is not None and not keep_frames:
        shutil.rmtree(frames_dir)
    return BuildResult(out_dir=out_dir, frames=count, events=len(session["events"]), warnings=warnings)
