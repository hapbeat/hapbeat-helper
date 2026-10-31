"""A session against the cue table (scene_check.py, ``scene check``; specs/scene-recording.md section 7).

The cue table is fixtures/sample-cue-table.json: footstep (variants approach / leave, at both),
roar (pos_neck), grab (at hand), rub (loop, at hand), purr (emit during rub), bite (undecided).
"""

import json
import shutil
from pathlib import Path

from hapbeat_helper import cli
from hapbeat_helper.scene_build import validate_session
from hapbeat_helper.scene_check import check_session, matches

FIXTURES = Path(__file__).parent / "fixtures"


def _table() -> dict:
    return json.loads((FIXTURES / "sample-cue-table.json").read_text(encoding="utf-8"))


def _session(events: list[dict], **extra) -> dict:
    s = {
        "schema_version": 1,
        "project": "sample-scene",
        "fps": 30,
        "frames": "Saved/HapticRecord/frames",
        "events": events,
        **extra,
        "lib": {
            "schema_version": 1,
            "title": "Sample scene",
            "project_name": "sample-scene",
            "paths": {"cues": "Haptics/cues.json", "clips": "Haptics/Clips", "sounds": "Haptics/Sounds"},
        },
    }
    return s


ALL = [
    {"frame": 1, "cue": "footstep:approach"},
    {"frame": 2, "cue": "footstep:leave"},
    {"frame": 3, "cue": "roar"},
    {"frame": 4, "cue": "grab", "hand": "left"},
    {"frame": 5, "cue": "rub", "hand": "right"},
    {"frame": 6, "cue": "bite"},
]
RUB_LEVELS = {"columns": ["rub:level:right"], "rows": [[0]] * 10}


def test_matches():
    assert matches("footstep", "footstep") and matches("footstep", "footstep:approach")
    assert matches("footstep:approach", "footstep:approach")
    assert not matches("footstep:approach", "footstep") and not matches("footstep:approach", "footstep:leave")
    assert not matches("foot", "footstep")


def test_clean_session():
    r = check_session(_session(ALL, levels=RUB_LEVELS), _table(), require_all=True)
    assert r.errors == [] and r.warnings == []
    assert r.coverage == {
        "footstep:approach": 1, "footstep:leave": 1, "roar": 1, "grab": 1, "rub": 1, "bite": 1,
    }


def test_invalid_session_returns_only_its_errors():
    r = check_session(_session([{"frame": -1, "cue": "lightning"}]), _table(), require_all=True)
    assert r.errors == validate_session(_session([{"frame": -1, "cue": "lightning"}]))
    assert r.warnings == [] and r.coverage == {}


def test_unknown_cue_and_variant():
    r = check_session(_session([{"frame": 1, "cue": "lightning"}, {"frame": 2, "cue": "roar:far"}]), _table())
    assert any("lightning is not in the cue table" in e for e in r.errors)
    assert any("roar has no variant far" in e for e in r.errors)


def test_level_columns():
    levels = {"columns": ["roar:level", "thunder:rate:left"], "rows": [[0, 0]]}
    r = check_session(_session([{"frame": 1, "cue": "rub", "hand": "left"}], levels=levels), _table())
    assert any("roar:level: roar is not a loop cue" in e for e in r.errors)
    assert any("thunder:rate:left: thunder is not in the cue table" in e for e in r.errors)
    assert any("loop cue rub fired but levels has no rub:level" in e for e in r.errors)
    r = check_session(_session([{"frame": 1, "cue": "rub", "hand": "left"}]), _table())
    assert any("loop cue rub fired" in e for e in r.errors)
    # a rate column alone does not count
    levels = {"columns": ["rub:rate"], "rows": [[1]]}
    r = check_session(_session([{"frame": 1, "cue": "rub", "hand": "left"}], levels=levels), _table())
    assert any("loop cue rub fired" in e for e in r.errors)


def test_takes_unexpected_and_missing():
    takes = [
        {"start": 0, "end": 10, "label": "step", "expect": ["footstep"], "allow": ["roar"]},
        {"start": 10, "end": 20, "label": "grab", "expect": ["grab", "footstep:leave"]},
    ]
    events = [
        {"frame": 1, "cue": "footstep:approach"},
        {"frame": 2, "cue": "roar"},
        {"frame": 11, "cue": "grab", "hand": "left"},
        {"frame": 12, "cue": "footstep:approach"},
        {"frame": 25, "cue": "roar"},
    ]
    r = check_session(_session(events, takes=takes), _table())
    assert r.takes == [
        {"label": "step", "start": 0, "end": 10, "ok": True,
         "fired": ["footstep:approach", "roar"], "missing": [], "unexpected": []},
        {"label": "grab", "start": 10, "end": 20, "ok": False,
         "fired": ["grab", "footstep:approach"], "missing": ["footstep:leave"], "unexpected": ["footstep:approach"]},
    ]
    assert any("'grab'" in e and "footstep:approach fired at frame 12" in e for e in r.errors)
    assert any("'grab'" in e and "expected footstep:leave did not fire" in e for e in r.errors)
    assert len(r.errors) == 2
    assert any("events[4] (frame 25): roar is outside every take" in w for w in r.warnings)


def test_overlapping_takes_are_a_session_error():
    takes = [{"start": 0, "end": 10, "label": "a"}, {"start": 5, "end": 20, "label": "b"}]
    r = check_session(_session([], takes=takes), _table())
    assert any("takes[1] starts at 5" in e for e in r.errors)


def test_require_all_with_variants_and_emit():
    events = [{"frame": 1, "cue": "footstep:approach"}, {"frame": 2, "cue": "roar"}]
    r = check_session(_session(events), _table(), require_all=True)
    assert sorted(r.errors) == sorted([
        "variant footstep:leave never fired",
        "cue grab never fired",
        "cue rub never fired",
        "cue bite never fired",
    ])  # footstep counts as fired (its variant did); purr has emit
    r = check_session(_session(events), _table())
    assert r.errors == []
    assert "cue grab never fired" in r.warnings and "variant footstep:leave never fired" in r.warnings
    assert not any("purr" in w for w in r.warnings)


def test_emit_cue_variants_are_not_counted():
    table = _table()
    table["cues"]["purr"]["variants"] = {"soft": {"hapticsGain": 0.5}}
    r = check_session(_session(ALL, levels=RUB_LEVELS), table, require_all=True)
    assert r.errors == [] and not any("purr" in w for w in r.warnings)


def test_without_a_cue_table():
    takes = [{"start": 0, "end": 3, "label": "t", "expect": ["roar"]}]
    events = [{"frame": 1, "cue": "lightning", "hand": "left"}, {"frame": 5, "cue": "rub"}]
    levels = {"columns": ["thunder:level"], "rows": [[0]]}
    r = check_session(_session(events, levels=levels, takes=takes), None, require_all=True)
    # only the takes are checked: lightning is unexpected and roar is missing
    assert len(r.errors) == 2 and all(e.startswith("take 't'") for e in r.errors)
    assert any("no cue table yet" in w for w in r.warnings)
    assert any("outside every take" in w for w in r.warnings)
    assert not any("hand" in w and "event(s)" in w for w in r.warnings)
    assert r.coverage == {"lightning": 1, "rub": 1}


def test_hand_warnings():
    table = _table()
    # a variant with its own haptics decides for itself
    table["cues"]["roar"]["variants"] = {"close": {"haptics": [{"clip": "roar", "at": "hand", "gain": 1}]}}
    table["cues"]["grab"]["variants"] = {"soft": {"hapticsGain": 0.5}}
    table["cues"]["bite"]["variants"] = {"hard": {"description": "still undecided"}}
    events = [
        {"frame": 1, "cue": "grab"},
        {"frame": 2, "cue": "grab:soft"},
        {"frame": 3, "cue": "roar", "hand": "left"},
        {"frame": 4, "cue": "roar:close"},
        {"frame": 5, "cue": "roar:close", "hand": "both"},
        {"frame": 6, "cue": "roar", "hand": "both"},  # both is not warned
        {"frame": 7, "cue": "bite", "hand": "left"},  # undecided haptics: neither warning
        {"frame": 8, "cue": "bite:hard"},
        {"frame": 9, "cue": "bite:hard", "hand": "right"},
    ]
    r = check_session(_session(events), table)
    assert r.errors == []
    hand = [w for w in r.warnings if "hand" in w]
    assert len(hand) == 4, hand
    assert any(w.startswith("grab: 1 event(s) without hand") for w in hand)
    assert any(w.startswith("grab:soft: 1 event(s) without hand") for w in hand)  # inherits at: hand
    assert any(w.startswith("roar:close: 1 event(s) without hand (first at frame 4)") for w in hand)
    assert any(w.startswith("roar: 1 event(s) with hand") and "not used" in w for w in hand)


# ── CLI ──


def _wavs(root: Path) -> None:
    """Empty WAVs for every clip / sound of the cue table (the CLI checks they exist)."""
    table = json.loads((root / "Haptics" / "cues.json").read_text(encoding="utf-8"))
    for folder, names in (("Clips", table["clips"]), ("Sounds", table.get("sounds", {}))):
        (root / "Haptics" / folder).mkdir(parents=True, exist_ok=True)
        for name in names:
            (root / "Haptics" / folder / f"{name}.wav").write_bytes(b"")


def _project(tmp_path: Path, session: dict) -> Path:
    root = tmp_path / "game"
    (root / "Haptics").mkdir(parents=True)
    shutil.copy(FIXTURES / "sample-cue-table.json", root / "Haptics" / "cues.json")
    _wavs(root)
    path = root / "session.json"
    path.write_text(json.dumps(session), encoding="utf-8")
    return root


def test_cli_json_shape(tmp_path, monkeypatch, capsys):
    takes = [{"start": 0, "end": 4, "label": "steps", "expect": ["footstep"]}]
    root = _project(tmp_path, _session(ALL, levels=RUB_LEVELS, takes=takes))
    monkeypatch.chdir(root)
    rc = cli.main(["scene", "check", "--session", "session.json", "--json"])
    captured = capsys.readouterr()
    assert rc == 1 and captured.err == ""
    result = json.loads(captured.out)
    assert set(result) == {"ok", "errors", "warnings", "coverage", "takes"}
    assert result["ok"] is False
    assert len(result["errors"]) == 1 and "roar fired at frame 3" in result["errors"][0]
    assert result["takes"] == [{
        "label": "steps", "start": 0, "end": 4, "ok": False,
        "fired": ["footstep:approach", "footstep:leave", "roar"], "missing": [], "unexpected": ["roar"],
    }]
    assert result["coverage"]["grab"] == 1
    assert any("outside every take" in w for w in result["warnings"])


def test_cli_text_and_project_root(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, _session(ALL, levels=RUB_LEVELS))
    monkeypatch.chdir(tmp_path)
    rc = cli.main([
        "scene", "check", "--session", str(root / "session.json"), "--project-root", str(root), "--require-all",
    ])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "OK: 6 events, 5/5 cues fired"
    # no table under the cwd: checked without one (section 7) / a named table must exist
    assert cli.main(["scene", "check", "--session", str(root / "session.json")]) == 0
    assert cli.main(["scene", "check", "--session", str(root / "session.json"), "--cues", "nope.json"]) == 2
    assert cli.main(["scene", "check", "--session", str(root / "nope.json")]) == 2


def test_cli_without_a_cue_table_yet(tmp_path, monkeypatch, capsys):
    root = _project(tmp_path, _session([{"frame": 1, "cue": "lightning"}]))
    (root / "Haptics" / "cues.json").unlink()
    monkeypatch.chdir(root)
    assert cli.main(["scene", "check", "--session", "session.json"]) == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "OK: 1 events, no cue table yet"
    assert "no cue table yet" in captured.err
