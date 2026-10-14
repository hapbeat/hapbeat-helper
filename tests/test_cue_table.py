"""Cue table checks (cue_table.py, ``cues validate``): schema + cue-table spec section 6.

The fixture is a copy of hapbeat-contracts fixtures/sample-cue-table.json.
"""

import copy
import json
from pathlib import Path

import pytest

from hapbeat_helper import cli
from hapbeat_helper.cue_table import validate_cue_table

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def table() -> dict:
    return json.loads((FIXTURES / "sample-cue-table.json").read_text(encoding="utf-8"))


def _wavs(table: dict, root: Path) -> tuple[Path, Path]:
    """Empty WAV files for every clip / sound the table names."""
    clips, sounds = root / "clips", root / "sounds"
    clips.mkdir()
    sounds.mkdir()
    for name in table["clips"]:
        (clips / f"{name}.wav").write_bytes(b"")
    for name in ["Footstep_A", "Footstep_B", "Roar_A", "Roar_B", "Rub"]:
        (sounds / f"{name}.wav").write_bytes(b"")
    return clips, sounds


def _problems(table: dict, **kw) -> list[str]:
    return validate_cue_table(table, **kw)


def test_fixture_is_valid(table, tmp_path):
    assert _problems(table) == []
    clips, sounds = _wavs(table, tmp_path)
    assert _problems(table, clips_dir=clips, sounds_dir=sounds) == []


def _one(table: dict, needle: str, **kw) -> None:
    problems = _problems(table, **kw)
    assert any(needle in p for p in problems), problems


def test_structure(table):
    t = copy.deepcopy(table)
    t["schema_version"] = 2
    _one(t, "schema_version must be 1")
    t = copy.deepcopy(table)
    del t["cues"]
    _one(t, "cues must be an object")
    t = copy.deepcopy(table)
    t["cues"]["roar"]["preview"] = {}
    _one(t, "preview is not a field")


def test_rule1_kit(table):
    table["kit"] = "Trex_Kit"
    _one(table, "kit must be a Kit name")


def test_rule2_clips(table, tmp_path):
    t = copy.deepcopy(table)
    t["clips"]["Bad Name"] = {"intensity": 1, "loop": False}
    _one(t, "clip name Bad Name")
    t = copy.deepcopy(table)
    t["clips"]["roar"]["intensity"] = 1.5
    _one(t, "clip roar: intensity must be 0..1")
    t = copy.deepcopy(table)
    del t["clips"]["roar"]["loop"]
    _one(t, "clip roar: loop must be true/false")
    t = copy.deepcopy(table)
    t["clips"]["roar"]["syncMs"] = -1
    _one(t, "clip roar: syncMs")
    clips, sounds = _wavs(table, tmp_path)
    (clips / "roar.wav").unlink()
    _one(table, "roar.wav missing", clips_dir=clips, sounds_dir=sounds)


def test_rule3_sounds(table, tmp_path):
    t = copy.deepcopy(table)
    t["sounds"]["9bad"] = {"intensity": 1}
    _one(t, "sound name 9bad")
    t = copy.deepcopy(table)
    t["sounds"]["Rub"]["intensity"] = -0.1
    _one(t, "sound Rub: intensity must be 0..1")
    t = copy.deepcopy(table)
    t["sounds"]["Rub"]["syncMs"] = 700000
    _one(t, "sound Rub: syncMs")
    clips, sounds = _wavs(table, tmp_path)
    (sounds / "Rub.wav").unlink()
    _one(table, "Rub.wav missing", clips_dir=clips, sounds_dir=sounds)


def test_rule4_names(table):
    t = copy.deepcopy(table)
    t["cues"]["Bad-Cue"] = {}
    _one(t, "cue name Bad-Cue")
    t = copy.deepcopy(table)
    t["cues"]["footstep"]["variants"]["Loud"] = {}
    _one(t, "variant name Loud")


def test_rule5_sfx(table, tmp_path):
    t = copy.deepcopy(table)
    t["cues"]["roar"]["sfx"]["sound"] = "Roar_A"
    _one(t, "sfx needs one of sound / sounds, not both")
    t = copy.deepcopy(table)
    t["cues"]["roar"]["sfx"] = {"volume": 1}
    _one(t, "or alternates when nothing is starred")
    t = copy.deepcopy(table)
    t["cues"]["roar"]["sfx"]["sounds"] = []
    _one(t, "sfx.sounds must be a non-empty list")
    t = copy.deepcopy(table)
    t["cues"]["roar"]["sfx"]["sounds"] = ["roar-a"]
    _one(t, "bad sound 'roar-a'")
    t = copy.deepcopy(table)
    t["cues"]["roar"]["sfx"]["volume"] = 3
    _one(t, "sfx volume must be 0..2")
    t = copy.deepcopy(table)
    t["cues"]["roar"]["sfx"]["alternates"] = ["Rub", "Rub"]
    _one(t, "alternates list Rub twice")
    t = copy.deepcopy(table)
    t["cues"]["roar"]["sfx"]["alternates"] = ["Roar_A"]
    _one(t, "alternates Roar_A are also starred")
    clips, sounds = _wavs(table, tmp_path)
    t = copy.deepcopy(table)
    t["cues"]["roar"]["sfx"]["sounds"] = ["Roar_C"]
    _one(t, "Roar_C.wav missing", clips_dir=clips, sounds_dir=sounds)


def test_rule6_routes(table):
    t = copy.deepcopy(table)
    t["cues"]["grab"]["haptics"][0]["clips"] = ["grab"]
    _one(t, "needs one of clip / clips, not both")
    t = copy.deepcopy(table)
    t["cues"]["grab"]["haptics"][0] = {"at": "hand", "gain": 1}
    _one(t, "needs clip / clips, or alternates")
    t = copy.deepcopy(table)
    t["cues"]["grab"]["haptics"][0]["clip"] = "nope"
    _one(t, "unknown clip 'nope'")
    t = copy.deepcopy(table)
    t["cues"]["grab"]["haptics"][0]["clip"] = "rub_loop"
    _one(t, "clip rub_loop loop=true does not fit this one-shot cue")
    t = copy.deepcopy(table)
    t["cues"]["grab"]["haptics"][0]["at"] = "pos_head"
    _one(t, "at must be one of")
    t = copy.deepcopy(table)
    t["cues"]["grab"]["haptics"][0]["gain"] = 2.5
    _one(t, "gain must be 0..2")
    t = copy.deepcopy(table)
    t["cues"]["grab"]["haptics"][0]["alternates"] = ["ghost"]
    _one(t, "alternates must be clip names of this table")
    t = copy.deepcopy(table)
    t["cues"]["grab"]["haptics"][0]["alternates"] = ["grab"]
    _one(t, "route alternates grab are also starred")


def test_rule7_level_map(table):
    t = copy.deepcopy(table)
    t["cues"]["grab"]["haptics"][0]["levelMap"] = {"points": [[1, 1]]}
    _one(t, "route levelMap is for loop cues")
    t = copy.deepcopy(table)
    t["cues"]["rub"]["sfx"]["levelMap"] = {"points": [[1, 1], [0.5, 2]]}
    _one(t, "sfx levelMap must be")
    t = copy.deepcopy(table)
    t["cues"]["rub"]["haptics"][0]["levelMap"] = {"points": [[1, 5]]}
    _one(t, "route levelMap must be")


def test_rule8_variation(table):
    t = copy.deepcopy(table)
    t["cues"]["footstep"]["variation"]["gainJitterDb"] = 13
    _one(t, "variation.gainJitterDb must be 0..12")
    t = copy.deepcopy(table)
    t["cues"]["footstep"]["variation"]["pick"] = "shuffle"
    _one(t, "variation.pick")
    t = copy.deepcopy(table)
    t["cues"]["roar"]["haptics"][0]["clips"] = ["roar"]
    _one(t, "variation.paired: route 1 has 1 clip(s) for 2 sound(s)")
    # Paired is checked on what a variant plays, inherited fields included.
    t = copy.deepcopy(table)
    t["cues"]["roar"]["variants"] = {"short": {"sfx": {"sound": "Roar_A", "volume": 1}}}
    _one(t, "roar:short: variation.paired: route 1 has 2 clip(s) for 1 sound(s)")


def test_rule9_variant_scale(table):
    t = copy.deepcopy(table)
    t["cues"]["footstep"]["variants"]["approach"]["rampTo"] = 2.5
    _one(t, "footstep:approach: rampTo must be 0..2")
    t = copy.deepcopy(table)
    t["cues"]["footstep"]["variants"]["approach"]["sfx"] = None
    _one(t, "sfxVolume is for an inherited sfx")
    t = copy.deepcopy(table)
    del t["cues"]["footstep"]["variants"]["approach"]["rampTo"]
    _one(t, "rampCurve needs rampTo")
    t = copy.deepcopy(table)
    t["cues"]["footstep"]["variants"]["leave"]["rampTo"] = 1
    _one(t, "rampSteps replaces rampTo / rampCurve")
    t = copy.deepcopy(table)
    t["cues"]["footstep"]["variants"]["leave"]["rampSteps"] = []
    _one(t, "rampSteps must be 1-64 numbers 0..2")


def test_rule10_distance_falloff(table):
    t = copy.deepcopy(table)
    t["cues"]["footstep"]["distanceFalloff"]["nearCm"] = 3000
    _one(t, "distanceFalloff needs 0 <= nearCm < farCm")
    t = copy.deepcopy(table)
    t["cues"]["footstep"]["distanceFalloff"]["curve"] = "smooth"
    _one(t, "distanceFalloff.curve")


def test_rule11_emit(table):
    t = copy.deepcopy(table)
    t["cues"]["purr"]["emit"]["during"] = "grab"
    _one(t, "emit.during must be a loop cue")
    t = copy.deepcopy(table)
    t["cues"]["purr"]["emit"]["intervalSec"] = 0.01
    _one(t, "emit.intervalSec must be 0.05..30")
    t = copy.deepcopy(table)
    t["cues"]["purr"]["emit"]["extra"] = 1
    _one(t, "purr: emit must be")
    t = copy.deepcopy(table)
    t["cues"]["rub"]["emit"] = {"during": "rub", "intervalSec": 1}
    _one(t, "emit is for one-shot cues")


def test_rule12_review(table):
    t = copy.deepcopy(table)
    t["cues"]["footstep"]["review"]["sfx"] = "done"
    _one(t, "review.sfx must be tentative or approved")
    t = copy.deepcopy(table)
    t["cues"]["footstep"]["review"]["notes"] = "approved"
    _one(t, "review.notes is not a field")


# ── CLI ──


def test_cli_ok_without_wavs(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["cues", "validate", str(FIXTURES / "sample-cue-table.json")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "WAV checks skipped" in out
    assert out.rstrip().endswith("OK (6 cues)")


def test_cli_with_viewer_lib(capsys, table, tmp_path):
    _wavs(table, tmp_path)
    (tmp_path / "Saved" / "HapticViewer").mkdir(parents=True)
    lib = {"schema_version": 1, "title": "t", "project_name": "t",
           "paths": {"cues": "cues.json", "clips": "clips", "sounds": "sounds"}}
    (tmp_path / "Saved" / "HapticViewer" / "viewer-lib.json").write_text(json.dumps(lib), encoding="utf-8")
    path = tmp_path / "cues.json"
    path.write_text(json.dumps(table), encoding="utf-8")
    assert cli.main(["cues", "validate", str(path), "--project-root", str(tmp_path)]) == 0
    assert "OK (6 cues)" in capsys.readouterr().out
    (tmp_path / "clips" / "grab.wav").unlink()
    assert cli.main(["cues", "validate", str(path), "--project-root", str(tmp_path)]) == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines == ["clip grab: " + (tmp_path / "clips").as_posix() + "/grab.wav missing"]


def test_variant_cannot_hold_cue_fields(table):
    for key, value in (("loop", True), ("emit", {"during": "rub", "intervalSec": 1}), ("variants", {})):
        t = copy.deepcopy(table)
        t["cues"]["footstep"]["variants"]["approach"][key] = value
        _one(t, f"a variant cannot have {key}")
