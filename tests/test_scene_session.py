"""Recording log + hapbeat-scene.json -> session.json (scene_session.py, ``scene session``).

The cue table is fixtures/sample-cue-table.json. No ffmpeg is run: ``--build`` is checked with
``scene_build.build`` replaced, and with an ffmpeg that does not exist.
"""

import json
import shutil
from pathlib import Path

from hapbeat_helper import cli, scene_build
from hapbeat_helper.scene_log import parse_log_text
from hapbeat_helper.scene_session import make_session

FIXTURES = Path(__file__).parent / "fixtures"
SRC_A = "unity:d7dd6680a2c34f1e9b0c5d4e3f2a1b0c/ce65e997e9d54d2eabde0d646dea67c7"
SRC_B = "unity:5e1c0b9a8f7d4c3b2a1908f7e6d5c4b3/0b1f4e2a8c3d4b5e9f60718293a4b5c6"
SRC_RUB = "unity:5e1c0b9a8f7d4c3b2a1908f7e6d5c4b3/9a8b7c6d5e4f40312a1b0c9d8e7f6a5b"


def _cfg() -> dict:
    return {
        "schema_version": 1,
        "project": "sample-scene",
        "fps": 30,
        "frames": "Saved/HapticRecord/frames",
        "lib": {
            "schema_version": 1,
            "title": "Sample scene",
            "project_name": "sample-scene",
            "paths": {"cues": "Haptics/cues.json", "clips": "Haptics/Clips", "sounds": "Haptics/Sounds"},
        },
        "bindings": [
            {"source": SRC_A, "cue": "grab", "hand": "left"},
            {"source": SRC_B, "cue": "footstep:approach"},
            {"source": SRC_RUB, "cue": "rub", "hand": "right"},
        ],
    }


def _log(*records: dict, start: dict | None = None, end: int | None = 100) -> str:
    lines = [start or {"t": "start", "schema_version": 1, "fps": 30, "project": "sample-scene"}]
    lines += list(records)
    if end is not None:
        lines.append({"t": "end", "frames": end})
    return "\n".join(json.dumps(r) for r in lines) + "\n"


def _make(*records: dict, **kw):
    return make_session(_cfg(), parse_log_text(_log(*records, **kw)))


def test_minimal_session_and_key_order():
    made = _make({"t": "cue", "frame": 5, "cue": "roar", "gain": 0.5, "dist": 900, "note": "n"})
    assert made.errors == [] and made.warnings == []
    s = made.session
    assert list(s) == ["schema_version", "project", "fps", "frames", "events", "lib"]
    assert s["events"] == [{"frame": 5, "cue": "roar", "gain": 0.5, "dist": 900, "note": "n"}]


def test_sparse_level_hold():
    made = _make(
        {"t": "level", "frame": 2, "values": {"rub:level:left": 0.4}},
        {"t": "level", "frame": 4, "values": {"rub:level:right": 1, "rub:level:left": 0}},
        {"t": "level", "frame": 4, "values": {"rub:level:right": 0.5}},
        {"t": "level", "frame": 1, "values": {"rub:rate": 2}},
        end=6,
    )
    assert made.errors == []
    levels = made.session["levels"]
    assert levels["columns"] == ["rub:level:left", "rub:level:right", "rub:rate"]
    assert levels["rows"] == [  # rate is 1 before its first write, level 0
        [0, 0, 1], [0, 0, 2], [0.4, 0, 2], [0.4, 0, 2], [0, 0.5, 2], [0, 0.5, 2],
    ]
    assert list(made.session)[-3:] == ["events", "levels", "lib"]


def test_level_by_source_and_config_column_order():
    cfg = _cfg()
    cfg["columns"] = ["rub:level:left", "rub:level:right"]
    made = make_session(cfg, parse_log_text(_log(
        {"t": "level", "frame": 1, "source": SRC_RUB, "value": 0.3},  # the binding's hand: right
        {"t": "level", "frame": 2, "source": SRC_RUB, "value": 0.6, "hand": "left"},
        {"t": "level", "frame": 2, "source": SRC_RUB, "value": 0.7, "hand": "left"},  # same frame: the later
        {"t": "level", "frame": 3, "source": SRC_B, "value": 1, "hand": "both"},  # footstep:approach -> footstep:level
        end=4,
    )))
    assert made.errors == []
    assert made.session["levels"] == {
        "columns": ["rub:level:left", "rub:level:right", "footstep:level"],
        "rows": [[0, 0, 0], [0, 0.3, 0], [0.7, 0.3, 0], [0.7, 0.3, 1]],
    }


def test_level_by_unmapped_source():
    made = _make({"t": "level", "frame": 1, "source": "unity:aaaa/bbbb", "value": 1})
    assert made.session is None
    assert any("line 2" in e and "unmapped" in e for e in made.errors)


def test_takes_from_take_end_and_back_to_back():
    made = _make(
        {"t": "take", "frame": 0, "label": "one", "expect": ["grab"]},
        {"t": "take_end", "frame": 10},
        {"t": "take", "frame": 20, "label": "two", "allow": ["roar"]},
        {"t": "take", "frame": 30, "label": "three"},
        end=50,
    )
    assert made.errors == []
    assert made.session["takes"] == [
        {"start": 0, "end": 10, "label": "one", "expect": ["grab"], "allow": []},
        {"start": 20, "end": 30, "label": "two", "expect": [], "allow": ["roar"]},
        {"start": 30, "end": 50, "label": "three", "expect": [], "allow": []},
    ]
    assert list(made.session)[-2:] == ["takes", "lib"]


def test_take_end_without_take():
    made = _make({"t": "take_end", "frame": 3})
    assert made.session is None
    assert any("take_end with no take open" in e for e in made.errors)


def test_take_end_at_its_take_frame():
    made = _make({"t": "take", "frame": 5, "label": "x"}, {"t": "take_end", "frame": 5})
    assert made.session is None
    assert any("line 3" in e and "not after its take's frame (5)" in e for e in made.errors)


def test_leftover_frames_from_an_earlier_recording(tmp_path):
    folder = tmp_path / "Saved" / "HapticRecord" / "frames"
    folder.mkdir(parents=True)
    for n in (0, 9):
        (folder / f"f{n}.png").write_bytes(b"")
    log = parse_log_text(_log(end=10))
    assert make_session(_cfg(), log, tmp_path).errors == []
    (folder / "f10.png").write_bytes(b"")
    made = make_session(_cfg(), log, tmp_path)
    assert made.session is None and any("f10.png" in e and "earlier recording" in e for e in made.errors)
    assert make_session(_cfg(), log).errors == []  # no root: not checked
    cfg = _cfg()
    del cfg["frames"]
    cfg["video"] = "Saved/HapticRecord/replay.mp4"
    assert make_session(cfg, log, tmp_path).errors == []  # a video: not checked


def test_not_a_log_is_only_no_start():
    made = make_session(_cfg(), parse_log_text("engine text\nmore\nand more\n"))
    assert made.session is None and len(made.errors) == 1 and "no start" in made.errors[0]


def test_source_through_bindings_and_hand():
    made = _make(
        {"t": "cue", "frame": 1, "source": SRC_A},
        {"t": "cue", "frame": 2, "source": SRC_A, "hand": "right"},
        {"t": "cue", "frame": 3, "source": SRC_B, "gain": 0.7},
    )
    assert made.errors == []
    assert made.session["events"] == [
        {"frame": 1, "cue": "grab", "hand": "left"},
        {"frame": 2, "cue": "grab", "hand": "right"},
        {"frame": 3, "cue": "footstep:approach", "gain": 0.7},
    ]


def test_unmapped_source():
    made = _make({"t": "cue", "frame": 1, "source": "unity:ffff/0000"})
    assert made.session is None
    assert any("line 2" in e and "unity:ffff/0000" in e and "unmapped" in e for e in made.errors)


def test_missing_start_and_end():
    made = make_session(_cfg(), parse_log_text('{"t":"cue","frame":1,"cue":"roar"}\n'))
    assert made.session is None
    assert any("no start" in e for e in made.errors)
    assert any("no end" in e for e in made.errors)
    made = _make({"t": "cue", "frame": 1, "cue": "roar"}, end=None)
    assert made.session is None and any("no end" in e for e in made.errors)


def test_fail_lists_the_reasons():
    made = _make(
        {"t": "fail", "frame": 40, "reason": "take 3 fired grab"},
        {"t": "fail", "reason": "timed out"},
    )
    assert made.session is None
    assert len(made.errors) == 1
    assert "take 3 fired grab (line 2, frame 40)" in made.errors[0] and "timed out (line 3)" in made.errors[0]


def test_fps_and_project_mismatch():
    made = _make(start={"t": "start", "schema_version": 1, "fps": 60, "project": "other"})
    assert made.session is None
    assert any("fps 60" in e for e in made.errors)
    assert any("project 'other'" in e for e in made.errors)
    made = _make(start={"t": "start", "schema_version": 1, "fps": 30.0})  # project is optional
    assert made.errors == []


def test_out_of_order_sorted_with_warning():
    made = _make(
        {"t": "cue", "frame": 9, "cue": "roar"},
        {"t": "cue", "frame": 3, "cue": "grab", "hand": "left"},
        {"t": "cue", "frame": 3, "cue": "grab", "hand": "right"},
    )
    assert [(e["frame"], e.get("hand")) for e in made.session["events"]] == [(3, "left"), (3, "right"), (9, None)]
    assert any("out of frame order" in w for w in made.warnings)


def test_event_after_end_dropped_with_warning():
    made = _make({"t": "cue", "frame": 9, "cue": "roar"}, {"t": "cue", "frame": 10, "cue": "roar"}, end=10)
    assert [e["frame"] for e in made.session["events"]] == [9]
    assert any("line 3" in w and "frame 10" in w for w in made.warnings)


def test_log_errors_stop_the_session():
    made = _make({"t": "cue", "frame": 1, "cue": "roar", "source": SRC_A})
    assert made.session is None and any(e.startswith("line 2:") for e in made.errors)


# ── CLI ──


def _wavs(root: Path) -> None:
    """Empty WAVs for every clip / sound of the cue table (the CLI checks they exist)."""
    table = json.loads((root / "Haptics" / "cues.json").read_text(encoding="utf-8"))
    for folder, names in (("Clips", table["clips"]), ("Sounds", table.get("sounds", {}))):
        (root / "Haptics" / folder).mkdir(parents=True, exist_ok=True)
        for name in names:
            (root / "Haptics" / folder / f"{name}.wav").write_bytes(b"")


def _project(tmp_path: Path, log: str) -> Path:
    root = tmp_path / "game"
    (root / "Haptics").mkdir(parents=True)
    shutil.copy(FIXTURES / "sample-cue-table.json", root / "Haptics" / "cues.json")
    _wavs(root)
    cfg = _cfg()
    (root / "hapbeat-scene.json").write_text(json.dumps(cfg), encoding="utf-8")
    (root / "Saved" / "HapticRecord").mkdir(parents=True)
    (root / "Saved" / "HapticRecord" / "events.jsonl").write_text(log, encoding="utf-8")
    return root


GOOD = (
    {"t": "take", "frame": 0, "label": "grab it", "expect": ["grab"]},
    {"t": "cue", "frame": 5, "source": SRC_A},
    {"t": "take_end", "frame": 20},
    {"t": "cue", "frame": 30, "cue": "roar"},
)


def test_cli_end_to_end(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, _log(*GOOD))
    monkeypatch.chdir(root)
    assert cli.main(["scene", "session"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("OK: 2 events, 2/5 cues fired")
    out = root / "Saved" / "HapticRecord" / "session.json"
    text = out.read_text(encoding="utf-8")
    assert text.startswith('{\n  "schema_version": 1,') and text.endswith("}\n")
    s = json.loads(text)
    assert s["events"] == [{"frame": 5, "cue": "grab", "hand": "left"}, {"frame": 30, "cue": "roar"}]
    assert s["takes"] == [{"start": 0, "end": 20, "label": "grab it", "expect": ["grab"], "allow": []}]
    assert scene_build.validate_session(s) == []
    assert "outside every take" in captured.err  # the roar at frame 30


def test_cli_config_and_log_from_elsewhere(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, "")
    engine_log = tmp_path / "Editor.log"
    engine_log.write_text(
        "\n".join(f"[x] HAPBEAT_SCENE {line}" for line in _log(*GOOD).splitlines()), encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out" / "s.json"
    assert cli.main([
        "scene", "session", "--config", str(root / "hapbeat-scene.json"),
        "--log", "Editor.log", "--out", str(out), "--json",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True and result["session"] == str(out) and "built" not in result
    assert result["coverage"] == {"grab": 1, "roar": 1}
    assert out.is_file()


def test_cli_errors_write_nothing(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, _log({"t": "cue", "frame": 5, "cue": "lightning"}))
    monkeypatch.chdir(root)
    assert cli.main(["scene", "session"]) == 1
    captured = capsys.readouterr()
    assert "error: " in captured.err and "lightning" in captured.err
    assert captured.out.startswith("FAILED: 1 error")
    assert not (root / "Saved" / "HapticRecord" / "session.json").exists()


def test_cli_bad_inputs(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, _log(*GOOD))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scene", "session"]) == 2  # no ./hapbeat-scene.json
    monkeypatch.chdir(root)
    assert cli.main(["scene", "session", "--keep-frames"]) == 2
    assert cli.main(["scene", "session", "--log", "nope.jsonl"]) == 2
    assert cli.main(["scene", "session", "--cues", "nope.json"]) == 2
    (root / "Haptics" / "cues.json").write_text('{"schema_version": 1}', encoding="utf-8")
    assert cli.main(["scene", "session"]) == 1  # an invalid table is an error, not unreadable
    assert "cue table" in capsys.readouterr().err


def test_cli_build(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, _log(*GOOD))
    monkeypatch.chdir(root)
    calls = []

    def fake_build(session_path, project_root, ffmpeg=None, keep_frames=False):
        calls.append((Path(session_path), Path(project_root), ffmpeg, keep_frames))
        assert Path(session_path).is_file()
        return scene_build.BuildResult(out_dir=Path(project_root) / scene_build.VIEWER_DIR, frames=100, events=2)

    monkeypatch.setattr(scene_build, "build", fake_build)
    assert cli.main(["scene", "session", "--build", "--ffmpeg", "ff", "--keep-frames", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    here = Path.cwd()  # the default config is ./hapbeat-scene.json, so the root is the cwd
    assert calls == [(here / "Saved" / "HapticRecord" / "session.json", here, "ff", True)]
    assert result["built"] == str(here / scene_build.VIEWER_DIR)


def test_cli_build_failure(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, _log(*GOOD))
    monkeypatch.chdir(root)
    assert cli.main(["scene", "session", "--build", "--ffmpeg", str(tmp_path / "no-such-ffmpeg")]) == 1
    captured = capsys.readouterr()
    # The frames folder does not exist here; either way the build fails and says why.
    assert "error: " in captured.err
    assert (root / "Saved" / "HapticRecord" / "session.json").is_file()


def test_cli_missing_wav_is_an_error(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, _log(*GOOD))
    (root / "Haptics" / "Clips" / "roar.wav").unlink()
    monkeypatch.chdir(root)
    assert cli.main(["scene", "session"]) == 1
    assert "roar.wav" in capsys.readouterr().err
    assert not (root / "Saved" / "HapticRecord" / "session.json").exists()


def test_cli_lib_only(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, "")  # no usable log: --lib-only does not read it
    monkeypatch.chdir(root)
    assert cli.main(["scene", "session", "--lib-only"]) == 0
    lib = json.loads((root / "Saved" / "HapticViewer" / "viewer-lib.json").read_text(encoding="utf-8"))
    assert lib == _cfg()["lib"]
    assert not (root / "Saved" / "HapticRecord" / "session.json").exists()


def test_cli_without_a_cue_table_yet(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, _log({"t": "cue", "frame": 5, "cue": "lightning", "hand": "left"}))
    (root / "Haptics" / "cues.json").unlink()
    monkeypatch.chdir(root)
    assert cli.main(["scene", "session"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("OK: 1 events, no cue table yet")
    assert "no cue table yet" in captured.err
    assert (root / "Saved" / "HapticRecord" / "session.json").is_file()
    assert cli.main(["scene", "session", "--cues", "Haptics/cues.json"]) == 2  # named explicitly: must exist
