"""``cues init`` (cue_init.py): inventory -> first cue table + recording bindings (cue-table section 10)."""

import json
from pathlib import Path

import pytest

from hapbeat_helper import cli
from hapbeat_helper.cue_inventory import normalize_name
from hapbeat_helper.cue_table import validate_cue_table

from tests.test_unity_scan import project, write_wav  # noqa: F401  (fixture)

L, R = "player_1/pos_l_wrist", "player_1/pos_r_wrist"


@pytest.mark.parametrize("name, cue", [
    ("GrabLight", "grab_light"), ("UI-Click-Light", "ui_click_light"), ("HTTPRequest", "http_request"),
    ("2ndHit", "c_2nd_hit"), ("PushFeedback", "push_feedback"), ("  __Odd--Name__ ", "odd_name"),
    ("", "cue_ce65e997"), ("-+-", "cue_ce65e997"),
])
def test_normalize_name(name, cue):
    assert normalize_name(name, "unity:ce65e997e9d54d2eabde0d646dea67c7") == cue


def ev(source, name, target=L, **kw):
    out = {"source": f"unity:{source}", "name": name, "loop": False, "target": target, "gain": 1}
    out.update(kw)
    return out


def inventory(tmp_path: Path, events: list, project_name="game") -> Path:
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"schema_version": 1, "engine": "unity", "project": project_name, "events": events}), encoding="utf-8")
    return path


def run(capsys, *argv) -> tuple[int, dict]:
    code = cli.main(["cues", "init", *argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


def scan_inventory(proj: Path, tmp_path: Path, capsys) -> Path:
    out = tmp_path / "scan"
    assert cli.main(["cues", "scan", "--unity", str(proj), "--out", str(out)]) == 0
    capsys.readouterr()
    return out / "inventory.json"


def files(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


@pytest.fixture
def scanned(project, tmp_path, capsys):  # noqa: F811
    """The fixture project scanned; the table goes into a separate game folder."""
    inv = scan_inventory(project, tmp_path, capsys)
    game = tmp_path / "game"
    game.mkdir()
    return inv, project, game


def init_scanned(capsys, scanned, *extra):
    inv, proj, game = scanned
    return run(
        capsys, "--inventory", str(inv), "--out", str(game / "Haptics" / "cues.json"),
        "--project-root", str(game), "--source-root", str(proj), *extra,
    )


def test_inventory_to_table(capsys, scanned):
    code, report = init_scanned(capsys, scanned)
    assert code == 0, report
    game = scanned[2]
    table = json.loads((game / "Haptics" / "cues.json").read_text(encoding="utf-8"))
    clips = game / "Haptics" / "Clips"
    assert validate_cue_table(table, clips) == []
    assert table["kit"] == "proj-kit" and table["sounds"] == {}
    # The left / right pair -> one route at the acting hand; levelMap and intensity carried over.
    assert table["cues"]["grab_light"]["haptics"] == [{"clip": "grab_light", "at": "hand", "gain": 1}]
    assert table["clips"]["grab_light"] == {"intensity": 0.08, "loop": False, "description": "from Assets/Clips/grab.wav"}
    assert table["cues"]["rub"]["loop"] is True
    assert table["cues"]["rub"]["haptics"][0]["levelMap"] == {"points": [[1, 2]], "curve": "linear"}
    assert table["cues"]["grab_light"]["review"] == {"haptics": "tentative"}
    assert "sfx" not in table["cues"]["grab_light"]  # sound effects undecided
    # Two "Hit" items on different positions -> hit and hit_2.
    assert table["cues"]["hit"]["haptics"][0]["at"] == "pos_chest"
    assert table["cues"]["hit_2"]["haptics"][0]["at"] == "pos_neck"
    assert report["renamed"] == [{"from": "hit", "to": "hit_2", "sources": ["unity:b0000000000000000000000000000002/2000000000000000000000000000000e"]}]
    # gain 2.5 is limited to 2.
    assert table["cues"]["tap"]["haptics"][0]["gain"] == 2
    assert any("limited to 2" in w for w in report["warnings"])
    assert (clips / "grab_light.wav").read_bytes() == (scanned[1] / "Assets" / "Clips" / "grab.wav").read_bytes()
    assert set(report) == {"ok", "table", "config", "added", "bindings", "undecided", "renamed", "warnings", "errors"}


def test_broadcast_command_entry_is_undecided(capsys, scanned):
    _, report = init_scanned(capsys, scanned)
    table = json.loads((scanned[2] / "Haptics" / "cues.json").read_text(encoding="utf-8"))
    assert "haptics" not in table["cues"]["ping"]
    reasons = {u["cue"]: u["reason"] for u in report["undecided"]}
    assert "everyone" in reasons["ping"] and "no WAV" in reasons["ping"]
    assert any("UnusedMap.asset is not referenced" in w for w in report["warnings"])


def test_bindings_with_hand_for_the_pair(capsys, scanned):
    _, report = init_scanned(capsys, scanned)
    cfg = json.loads((scanned[2] / "hapbeat-scene.json").read_text(encoding="utf-8"))
    bindings = {b["source"]: b for b in cfg["bindings"]}
    assert bindings["unity:b0000000000000000000000000000001/1000000000000000000000000000000a"] == {
        "source": "unity:b0000000000000000000000000000001/1000000000000000000000000000000a", "cue": "grab_light", "hand": "left",
    }
    assert bindings["unity:b0000000000000000000000000000002/2000000000000000000000000000000a"]["hand"] == "right"
    assert "hand" not in bindings["unity:b0000000000000000000000000000002/2000000000000000000000000000000e"]
    assert bindings["unity:b0000000000000000000000000000002/2000000000000000000000000000000e"]["cue"] == "hit_2"
    assert cfg["lib"]["paths"] == {"cues": "Haptics/cues.json", "clips": "Haptics/Clips", "sounds": "Haptics/Sounds"}
    assert report["bindings"] == cfg["bindings"]


def test_second_run_changes_nothing(capsys, scanned):
    inv, proj, game = scanned
    assert init_scanned(capsys, scanned)[0] == 0
    before = files(game)
    code, report = run(
        capsys, "--inventory", str(inv), "--into", str(game / "Haptics" / "cues.json"),
        "--project-root", str(game), "--source-root", str(proj),
    )
    assert code == 0, report
    assert report["added"] == {"cues": [], "variants": [], "clips": []}
    assert files(game) == before


def test_into_keeps_existing_values(capsys, tmp_path):
    root = tmp_path / "game"
    write_wav(root / "src" / "a.wav")
    table_path = root / "cues.json"
    existing = {
        "schema_version": 1, "kit": "mine-kit", "clips": {"grab_light": {"intensity": 0.5, "loop": False}},
        "sounds": {}, "cues": {"grab_light": {"description": "decided", "sfx": None, "haptics": [{"clip": "grab_light", "at": "both", "gain": 0.3}]}},
    }
    table_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    write_wav(root / "Clips" / "grab_light.wav", b"\x09\x00")
    inv = inventory(tmp_path, [ev("a1", "GrabLight", wav="src/a.wav"), ev("a2", "Snap", wav="src/a.wav")])
    code, report = run(capsys, "--inventory", str(inv), "--into", str(table_path), "--project-root", str(root))
    assert code == 0, report
    table = json.loads(table_path.read_text(encoding="utf-8"))
    assert table["cues"]["grab_light"] == existing["cues"]["grab_light"]
    assert table["clips"]["grab_light"] == existing["clips"]["grab_light"]
    assert table["kit"] == "mine-kit"
    assert report["added"]["cues"] == ["snap"] and report["added"]["clips"] == ["snap"]


def test_wav_name_collision_gets_2(capsys, tmp_path):
    root = tmp_path / "game"
    write_wav(root / "src" / "a.wav", b"\x01\x00")
    write_wav(root / "Clips" / "grab_light.wav", b"\x02\x00")  # same name, other content
    before = (root / "Clips" / "grab_light.wav").read_bytes()
    inv = inventory(tmp_path, [ev("a1", "GrabLight", wav="src/a.wav")])
    code, report = run(capsys, "--inventory", str(inv), "--out", str(root / "cues.json"), "--project-root", str(root))
    assert code == 0, report
    table = json.loads((root / "cues.json").read_text(encoding="utf-8"))
    assert table["cues"]["grab_light"]["haptics"][0]["clip"] == "grab_light_2"
    assert (root / "Clips" / "grab_light.wav").read_bytes() == before  # never overwritten
    assert (root / "Clips" / "grab_light_2.wav").read_bytes() == (root / "src" / "a.wav").read_bytes()


def test_same_wav_already_in_folder_is_reused(capsys, tmp_path):
    root = tmp_path / "game"
    write_wav(root / "src" / "a.wav", b"\x01\x00")
    write_wav(root / "Clips" / "grab_light.wav", b"\x01\x00")
    inv = inventory(tmp_path, [ev("a1", "GrabLight", wav="src/a.wav")])
    code, _ = run(capsys, "--inventory", str(inv), "--out", str(root / "cues.json"), "--project-root", str(root))
    assert code == 0
    table = json.loads((root / "cues.json").read_text(encoding="utf-8"))
    assert table["cues"]["grab_light"]["haptics"][0]["clip"] == "grab_light"


def test_explicit_name_conflict_is_exit_1(capsys, tmp_path):
    root = tmp_path / "game"
    write_wav(root / "src" / "a.wav")
    inv = inventory(tmp_path, [
        ev("a1", "One", cue="punch", wav="src/a.wav"), ev("a2", "Two", cue="punch", wav="src/a.wav", gain=0.5),
    ])
    code, report = run(capsys, "--inventory", str(inv), "--out", str(root / "cues.json"), "--project-root", str(root))
    assert code == 1 and report["ok"] is False
    assert any("cannot be merged" in e for e in report["errors"])
    assert not (root / "cues.json").exists() and not (root / "hapbeat-scene.json").exists()
    assert not (root / "Clips").exists()


def test_loop_mismatch_is_exit_1(capsys, tmp_path):
    root = tmp_path / "game"
    write_wav(root / "src" / "a.wav")
    inv = inventory(tmp_path, [
        ev("a1", "Rub", cue="rub", wav="src/a.wav"), ev("a2", "Rub fast", cue="rub:fast", wav="src/a.wav", loop=True),
    ])
    code, report = run(capsys, "--inventory", str(inv), "--out", str(root / "cues.json"), "--project-root", str(root))
    assert code == 1
    assert any("mix loop and one-shot" in e for e in report["errors"])
    assert not (root / "cues.json").exists()


def test_variant_items(capsys, tmp_path):
    root = tmp_path / "game"
    write_wav(root / "src" / "a.wav")
    inv = inventory(tmp_path, [ev("a1", "Soft", cue="punch:soft", wav="src/a.wav"), ev("a2", "Hard", cue="punch:hard", target="")])
    code, report = run(capsys, "--inventory", str(inv), "--out", str(root / "cues.json"), "--project-root", str(root))
    assert code == 0, report
    punch = json.loads((root / "cues.json").read_text(encoding="utf-8"))["cues"]["punch"]
    assert "haptics" not in punch  # no item for the cue itself
    assert punch["variants"]["soft"]["haptics"][0]["clip"] == "punch_soft"
    # An undecided variant gets [] (it would inherit the cue's route otherwise).
    assert punch["variants"]["hard"]["haptics"] == [] and punch["variants"]["hard"]["review"] == {"haptics": "tentative"}
    assert report["added"]["variants"] == ["punch:soft", "punch:hard"]


def test_dry_run_writes_nothing(capsys, scanned):
    _, _, game = scanned
    code, report = init_scanned(capsys, scanned, "--dry-run")
    assert code == 0 and report["added"]["clips"]
    assert files(game) == {}


def test_out_refuses_an_existing_table(capsys, tmp_path):
    (tmp_path / "cues.json").write_text("{}", encoding="utf-8")
    inv = inventory(tmp_path, [])
    code, _ = run(capsys, "--inventory", str(inv), "--out", str(tmp_path / "cues.json"), "--project-root", str(tmp_path))
    assert code == 2
    code, _ = run(capsys, "--inventory", str(inv), "--into", str(tmp_path / "missing.json"), "--project-root", str(tmp_path))
    assert code == 2


def test_add_and_loop(capsys, tmp_path):
    out = tmp_path / "cues.json"
    code, report = run(capsys, "--add", "roar,step", "--loop", "growl", "--out", str(out), "--project", "trex", "--project-root", str(tmp_path))
    assert code == 0, report
    table = json.loads(out.read_text(encoding="utf-8"))
    assert table == {"schema_version": 1, "kit": "trex-kit", "clips": {}, "sounds": {},
                     "cues": {"roar": {}, "step": {}, "growl": {"loop": True}}}
    assert validate_cue_table(table) == []
    assert report["config"] is None and not (tmp_path / "hapbeat-scene.json").exists()
    code, _ = run(capsys, "--add", "Bad-Name", "--out", str(tmp_path / "x.json"), "--project", "trex")
    assert code == 2


def test_from_session(capsys, tmp_path):
    session = {
        "schema_version": 1, "project": "boxing", "fps": 30, "frames": "Saved/f",
        "events": [{"frame": 1, "cue": "punch:soft"}, {"frame": 2, "cue": "block"}],
        "lib": {"schema_version": 1, "title": "Boxing", "project_name": "boxing",
                "paths": {"cues": "c.json", "clips": "C", "sounds": "S"},
                "layers": [{"cue": "rope", "level": [0, 1], "colors": []}]},
    }
    path = tmp_path / "session.json"
    path.write_text(json.dumps(session), encoding="utf-8")
    out = tmp_path / "cues.json"
    code, report = run(capsys, "--from-session", str(path), "--out", str(out), "--project-root", str(tmp_path))
    assert code == 0, report
    table = json.loads(out.read_text(encoding="utf-8"))
    assert table["kit"] == "boxing-kit"
    assert table["cues"] == {"block": {}, "punch": {}, "rope": {"loop": True}}


def test_text_output_lists_what_to_decide(capsys, scanned):
    inv, proj, game = scanned
    code = cli.main([
        "cues", "init", "--inventory", str(inv), "--out", str(game / "cues.json"),
        "--project-root", str(game), "--source-root", str(proj),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "to decide:" in out and "ping: haptics undecided" in out and "hit -> hit_2" in out
    assert "UnusedMap.asset is not referenced" in out


def test_split_target():
    from hapbeat_helper.cue_init import split_target

    assert split_target("player_1/pos_l_wrist") == ("pos_l_wrist", "player_1/*")
    assert split_target("pos_neck") == ("pos_neck", "")
    assert split_target("*/pos_chest") == ("pos_chest", "*/*")
    assert split_target("player_1") == (None, "player_1")
    assert split_target("player_1/pos_*") == (None, "player_1/pos_*")
    assert split_target("") == (None, "")


def test_rest_of_target_must_match(capsys, tmp_path):
    root = tmp_path / "game"
    write_wav(root / "src" / "a.wav")
    inv = inventory(tmp_path, [
        ev("a1", "Hit", target="player_1/pos_chest", wav="src/a.wav"),
        ev("a2", "Hit", target="player_2/pos_chest", wav="src/a.wav"),
    ])
    code, report = run(capsys, "--inventory", str(inv), "--out", str(root / "cues.json"), "--project-root", str(root))
    assert code == 0, report
    assert [r["to"] for r in report["renamed"]] == ["hit_2"]
    assert any("player_1/*" in w for w in report["warnings"]) and any("player_2/*" in w for w in report["warnings"])


def test_derived_loop_difference_is_exit_1(capsys, tmp_path):
    root = tmp_path / "game"
    write_wav(root / "src" / "a.wav")
    inv = inventory(tmp_path, [ev("a1", "Rub", wav="src/a.wav"), ev("a2", "Rub", wav="src/a.wav", loop=True, target=R)])
    code, report = run(capsys, "--inventory", str(inv), "--out", str(root / "cues.json"), "--project-root", str(root))
    assert code == 1 and any("mix loop" in e for e in report["errors"])


def test_existing_binding_names_the_item(capsys, tmp_path):
    root = tmp_path / "game"
    write_wav(root / "src" / "a.wav")
    cfg = {
        "schema_version": 1, "project": "game", "fps": 30, "frames": "Saved/f",
        "lib": {"schema_version": 1, "title": "Game", "project_name": "game",
                "paths": {"cues": "cues.json", "clips": "Clips", "sounds": "Sounds"}},
        "bindings": [{"source": "unity:a1", "cue": "grab_soft", "hand": "left"}],
    }
    (root / "hapbeat-scene.json").write_text(json.dumps(cfg), encoding="utf-8")
    inv = inventory(tmp_path, [ev("a1", "GrabLight", wav="src/a.wav"), ev("a2", "GrabLight", target=R, wav="src/a.wav")])
    code, report = run(capsys, "--inventory", str(inv), "--out", str(root / "cues.json"), "--project-root", str(root))
    assert code == 0, report
    table = json.loads((root / "cues.json").read_text(encoding="utf-8"))
    assert set(table["cues"]) == {"grab_soft", "grab_light"}
    saved = json.loads((root / "hapbeat-scene.json").read_text(encoding="utf-8"))
    assert saved["bindings"][0] == {"source": "unity:a1", "cue": "grab_soft", "hand": "left"}  # kept as it is
    assert saved["bindings"][1] == {"source": "unity:a2", "cue": "grab_light"}
    # A name the inventory gives replaces the binding.
    inv = inventory(tmp_path, [ev("a1", "GrabLight", cue="grab_firm", wav="src/a.wav")])
    code, report = run(capsys, "--inventory", str(inv), "--into", str(root / "cues.json"), "--project-root", str(root))
    assert code == 0, report
    saved = json.loads((root / "hapbeat-scene.json").read_text(encoding="utf-8"))
    assert saved["bindings"][0] == {"source": "unity:a1", "cue": "grab_firm"}


def test_into_fills_undecided_haptics_and_skips_loop_mismatch(capsys, tmp_path):
    root = tmp_path / "game"
    write_wav(root / "src" / "a.wav")
    table_path = root / "cues.json"
    existing = {"schema_version": 1, "kit": "game-kit", "clips": {}, "sounds": {},
                "cues": {"grab_light": {"description": "from Studio"}, "rub": {}}}
    table_path.write_text(json.dumps(existing), encoding="utf-8")
    inv = inventory(tmp_path, [ev("a1", "GrabLight", wav="src/a.wav"), ev("a2", "Rub", wav="src/a.wav", loop=True)])
    code, report = run(capsys, "--inventory", str(inv), "--into", str(table_path), "--project-root", str(root))
    assert code == 0, report
    table = json.loads(table_path.read_text(encoding="utf-8"))
    assert table["cues"]["grab_light"]["description"] == "from Studio"
    assert table["cues"]["grab_light"]["haptics"] == [{"clip": "grab_light", "at": "pos_l_wrist", "gain": 1}]
    assert table["cues"]["rub"] == {} and "rub" not in table["clips"]
    assert any("rub: the table has loop=false" in w for w in report["warnings"])


def test_wav_already_in_clip_folder_is_not_copied(capsys, tmp_path):
    root = tmp_path / "game"
    write_wav(root / "Clips" / "grab_light.wav")
    inv = inventory(tmp_path, [ev("a1", "GrabLight", wav="Clips/grab_light.wav")])
    code, report = run(
        capsys, "--inventory", str(inv), "--out", str(root / "cues.json"), "--project-root", str(root), "--clips", "Clips",
    )
    assert code == 0, report
    table = json.loads((root / "cues.json").read_text(encoding="utf-8"))
    assert table["cues"]["grab_light"]["haptics"][0]["clip"] == "grab_light"
    assert sorted(p.name for p in (root / "Clips").iterdir()) == ["grab_light.wav"]
