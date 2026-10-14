"""Scene recording -> Saved/HapticViewer/ (scene_build.py, ``scene build``).

The session is a copy of hapbeat-contracts fixtures/sample-scene-recording.json.
The encoding tests need ffmpeg with libx264 on PATH and are skipped without it;
the frames are tiny PNGs written here (no image library).
"""

import json
import shutil
import struct
import zlib
from pathlib import Path

import pytest

from hapbeat_helper import cli, scene_build
from hapbeat_helper.scene_build import (
    build_viewer_data,
    pad_levels,
    validate_session,
    validate_viewer_lib,
)

FIXTURES = Path(__file__).parent / "fixtures"
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _sample() -> dict:
    return json.loads((FIXTURES / "sample-scene-recording.json").read_text(encoding="utf-8"))


def _png(path: Path, w: int = 16, h: int = 16, rgb=(200, 40, 40)) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# ── checks ──


def test_fixture_session_and_libs_are_valid():
    s = _sample()
    assert validate_session(s["session"]) == []
    assert validate_viewer_lib(s["viewer_lib_minimal"]) == []
    assert validate_viewer_lib(s["viewer_lib_layers"]) == []


def test_session_problems():
    s = _sample()["session"]
    s["video"] = "replay.mp4"
    s["fps"] = 0
    s["events"][0]["frame"] = -1
    s["events"][1]["cue"] = "Footstep"
    s["events"][3]["hand"] = "up"
    s["levels"]["rows"][1] = [0.1]
    s["lib"]["paths"].pop("sounds")
    problems = validate_session(s)
    for needle in (
        "exactly one of frames / video",
        "fps must be",
        "events[0].frame",
        "events[1].cue",
        "events[3].hand",
        "levels.rows[1] has 1 values for 2 columns",
        "lib.paths.sounds",
    ):
        assert any(needle in p for p in problems), (needle, problems)


# ── viewer-data ──


def test_events_t_is_frame_over_fps():
    s = _sample()["session"]
    s["fps"] = 60
    s["events"].append({"frame": 7, "cue": "roar"})
    data = build_viewer_data(s, 240)
    assert [e["t"] for e in data["events"]] == [0.5, 1.25, 2.0, 3.0, 3.3333, 0.1167]
    assert data["events"][3] == {
        "t": 3.0, "name": "grab", "hand": "right", "gain": 0.9, "note": "right hand on the bone",
    }
    assert data["events"][0] == {"t": 0.5, "name": "footstep:approach", "dist": 1800}
    assert (data["schema_version"], data["fps"], data["frames"], data["video"]) == (1, 60, 240, "full_replay.mp4")


def test_levels_padded_and_cut():
    levels = {"columns": ["rub:level:left", "rub:level:right"], "rows": [[0, 0], [0, 0.4], [0, 0.9]]}
    assert pad_levels(levels, 5)["rows"] == [[0, 0], [0, 0.4], [0, 0.9], [0, 0], [0, 0]]
    assert pad_levels(levels, 2)["rows"] == [[0, 0], [0, 0.4]]
    assert pad_levels(levels, 2)["columns"] == levels["columns"]
    s = _sample()["session"]
    del s["levels"]
    assert "levels" not in build_viewer_data(s, 10)


# ── build ──


def _project(tmp_path: Path, frames: list[int]) -> tuple[Path, Path]:
    s = _sample()["session"]
    folder = tmp_path / "Saved" / "HapticRecord" / "frames"
    folder.mkdir(parents=True)
    for n in frames:
        _png(folder / f"f{n}.png")
    session = tmp_path / "Saved" / "HapticRecord" / "session.json"
    session.write_text(json.dumps(s), encoding="utf-8")
    return session, folder


@needs_ffmpeg
def test_build_writes_three_files_and_keeps_ui(tmp_path, monkeypatch, capsys):
    # A gap (f3, f4) and frames past the levels rows (4 rows).
    session, folder = _project(tmp_path, [0, 1, 2, 5, 6, 7])
    out = tmp_path / "Saved" / "HapticViewer"
    out.mkdir(parents=True)
    ui = out / "studio-scene-ui.json"
    ui.write_text('{"panels": 1}\n', encoding="utf-8")
    (out / "notes.txt").write_text("keep", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert cli.main(["scene", "build", "--session", str(session)]) == 0
    assert "8 frames" in capsys.readouterr().out

    assert (out / "full_replay.mp4").stat().st_size > 0
    assert scene_build.count_video_frames(shutil.which("ffmpeg"), out / "full_replay.mp4") == 8
    data_text = (out / "viewer-data.json").read_text(encoding="utf-8")
    assert data_text.endswith("}\n") and data_text.startswith('{\n  "schema_version": 1,')
    data = json.loads(data_text)
    assert data["frames"] == 8 and data["fps"] == 30 and data["video"] == "full_replay.mp4"
    assert data["events"][0] == {"t": 1.0, "name": "footstep:approach", "dist": 1800}
    assert len(data["levels"]["rows"]) == 8 and data["levels"]["rows"][4:] == [[0, 0]] * 4
    assert json.loads((out / "viewer-lib.json").read_text(encoding="utf-8")) == _sample()["session"]["lib"]
    assert ui.read_text(encoding="utf-8") == '{"panels": 1}\n'
    assert (out / "notes.txt").read_text(encoding="utf-8") == "keep"
    assert not folder.exists()  # deleted without --keep-frames
    assert not (out / "full_replay.tmp.mp4").exists()


@needs_ffmpeg
def test_build_keep_frames_and_project_root(tmp_path, monkeypatch):
    session, folder = _project(tmp_path, [0, 1, 2])
    monkeypatch.chdir(tmp_path.parent)
    assert cli.main([
        "scene", "build", "--session", str(session), "--project-root", str(tmp_path), "--keep-frames",
    ]) == 0
    assert folder.is_dir() and len(list(folder.glob("f*.png"))) == 3
    data = json.loads((tmp_path / "Saved" / "HapticViewer" / "viewer-data.json").read_text(encoding="utf-8"))
    assert data["frames"] == 3
    assert data["levels"]["rows"] == [[0, 0], [0, 0.4], [0, 0.9]]


def test_build_rejects_invalid_session(tmp_path, capsys):
    session, _ = _project(tmp_path, [0])
    s = json.loads(session.read_text(encoding="utf-8"))
    s["events"][0]["cue"] = "Bad"
    session.write_text(json.dumps(s), encoding="utf-8")
    rc = cli.main(["scene", "build", "--session", str(session), "--project-root", str(tmp_path)])
    assert rc == 1
    assert "events[0].cue" in capsys.readouterr().err
    assert not (tmp_path / "Saved" / "HapticViewer").exists()


def test_build_without_ffmpeg(tmp_path, capsys):
    session, folder = _project(tmp_path, [0])
    rc = cli.main([
        "scene", "build", "--session", str(session), "--project-root", str(tmp_path),
        "--ffmpeg", str(tmp_path / "no-such-ffmpeg"),
    ])
    assert rc == 1
    assert "ffmpeg not found" in capsys.readouterr().err
    assert folder.is_dir()
