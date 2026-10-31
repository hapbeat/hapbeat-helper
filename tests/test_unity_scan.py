"""``cues scan --unity`` (unity_scan.py): Event Maps and triggers of a Unity project -> inventory.

The fixture under fixtures/unity-scan is hand-made (YAML shaped like the SDK's QuickStart sample):
a left / right map pair, an unreferenced map, a Command entry without a WAV, an entry without an id,
two "Hit" entries on different body positions, and a prefab instance overriding ``_entryId``.
The WAVs are written by the test (no binaries in the repo).
"""

import json
import shutil
import wave
from pathlib import Path

import pytest

from hapbeat_helper import cli, unity_scan
from hapbeat_helper.cue_inventory import validate_inventory

FIXTURE = Path(__file__).parent / "fixtures" / "unity-scan"
LEFT_GRAB = "unity:b0000000000000000000000000000001/1000000000000000000000000000000a"


def write_wav(path: Path, body: bytes = b"\x01\x00") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(body * 64)


@pytest.fixture
def project(tmp_path) -> Path:
    """A copy of the fixture (project + SDK) with its WAVs."""
    shutil.copytree(FIXTURE, tmp_path / "fx")
    proj = tmp_path / "fx" / "proj"
    for i, clip in enumerate(("grab", "rub", "hit", "tap")):
        write_wav(proj / "Assets" / "Clips" / f"{clip}.wav", bytes([i + 1, 0]))
    return proj


def by_source(inv: dict) -> dict:
    return {e["source"]: e for e in inv["events"]}


def test_scan_reads_referenced_maps(project):
    result = unity_scan.scan(project)
    inv = result.inventory
    assert validate_inventory(inv) == []
    assert inv["engine"] == "unity" and inv["project"] == "proj"
    assert [(m["path"], m["referenced"]) for m in inv["maps"]] == [
        ("Assets/Maps/LeftMap.asset", True), ("Assets/Maps/RightMap.asset", True), ("Assets/Maps/UnusedMap.asset", False),
    ]
    # 4 left (one entry without an id is left out) + 4 right; nothing from the unreferenced map.
    assert len(inv["events"]) == 8
    assert not any(e["name"] == "Unused" for e in inv["events"])
    assert not any("cue" in e for e in inv["events"])  # only people / agents name items
    assert any("empty _id" in w for w in result.warnings)


def test_scan_entry_fields(project):
    events = by_source(unity_scan.scan(project).inventory)
    grab = events[LEFT_GRAB]
    assert grab == {
        "source": LEFT_GRAB, "map": "b0000000000000000000000000000001", "name": "GrabLight", "loop": False, "target": "player_1/pos_l_wrist", "gain": 1,
        "wav": "Assets/Clips/grab.wav", "intensity": 0.08,
        "where": ["Assets/Scenes/Main.unity: Left Hand/Grab (HapbeatUnityEventTrigger._entryId)"],
    }
    rub = events["unity:b0000000000000000000000000000001/1000000000000000000000000000000b"]
    assert rub["loop"] is True and "intensity" not in rub  # -1 = not resolved
    assert rub["levelMap"] == {"points": [[1, 2]], "curve": "linear"}  # StreamGain binding, output 0..2


def test_scan_command_entry_has_no_wav(project):
    ping = by_source(unity_scan.scan(project).inventory)["unity:b0000000000000000000000000000001/1000000000000000000000000000000c"]
    assert ping["name"] == "ping"  # eventName for a Command entry
    assert "wav" not in ping and ping["target"] == "" and ping["intensity"] == 0.6
    assert "ui.ping" in ping["note"]


def test_scan_prefab_override_and_gain_multiplier(project):
    events = by_source(unity_scan.scan(project).inventory)
    tap = events["unity:b0000000000000000000000000000002/2000000000000000000000000000000f"]
    assert tap["where"] == ["Assets/Scenes/Main.unity: Button in prefab Button (HapbeatUnityEventTrigger._entryId, prefab override)"]
    hit = events["unity:b0000000000000000000000000000002/2000000000000000000000000000000e"]
    assert hit["where"] == ["Assets/Scenes/Main.unity: Target (HapbeatUnityEventTrigger._entryId, gain x0.4)"]


def test_scan_finds_sdk_via_sdk_root(project, tmp_path):
    (project / "Packages" / "manifest.json").write_text('{"dependencies": {}}\n', encoding="utf-8")
    with pytest.raises(unity_scan.UnityScanError, match="not found"):
        unity_scan.scan(project)
    assert len(unity_scan.scan(project, sdk_root=project.parent / "sdk").inventory["events"]) == 8


def test_scan_binary_asset_is_an_error(project, capsys):
    (project / "Assets" / "Scenes" / "Binary.unity").write_bytes(b"\x00\x01binary scene\x00")
    with pytest.raises(unity_scan.BinaryAssetError):
        unity_scan.scan(project)
    assert cli.main(["cues", "scan", "--unity", str(project), "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and "binary" in out["error"]


def test_scan_skips_binary_assets_that_are_not_scriptable_objects(project):
    lighting = project / "Assets" / "Scenes" / "LightingData.asset"
    lighting.write_bytes(b"\x00\x00binary lighting")
    Path(str(lighting) + ".meta").write_text(
        "fileFormatVersion: 2\nguid: f0000000000000000000000000000001\n"
        "NativeFormatImporter:\n  mainObjectFileID: 112000000\n", encoding="utf-8",
    )
    assert len(unity_scan.scan(project).inventory["events"]) == 8


def test_cli_writes_inventory_and_wiring(project, tmp_path, capsys):
    out = tmp_path / "scan"
    assert cli.main(["cues", "scan", "--unity", str(project), "--out", str(out), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"ok", "inventory", "wiring", "events", "maps", "warnings"}
    assert report["ok"] is True and report["events"] == 8 and report["maps"] == 3
    inv = json.loads((out / "inventory.json").read_text(encoding="utf-8"))
    assert validate_inventory(inv) == []
    wiring = (out / "wiring.md").read_text(encoding="utf-8")
    assert "### GrabLight -> `grab_light`" in wiring
    assert "Left Hand/Grab (HapbeatUnityEventTrigger._entryId)" in wiring
    assert "UnusedMap.asset` (b0000000000000000000000000000003): not referenced" in wiring


def test_cli_default_out_is_saved_haptic_scan(project, capsys):
    assert cli.main(["cues", "scan", "--unity", str(project)]) == 0
    assert (project / "Saved" / "HapticScan" / "inventory.json").is_file()
    assert (project / "Saved" / "HapticScan" / "wiring.md").is_file()


def test_binding_level_map_shapes():
    def custom(keys):
        return {
            "inputMin": "0", "inputMax": "1", "curveType": "4", "outputParameter": "0",
            "outputMin": "0", "outputMax": "1",
            "customCurve": {"m_Curve": [
                {"time": str(t), "value": str(v), "inSlope": str(i), "outSlope": str(o), "weightedMode": "0"}
                for t, v, i, o in keys
            ]},
        }

    # Two linear segments (Hand Demo's scratch).
    lm, _ = unity_scan.binding_level_map(custom([(0, 0, 3.75, 3.75), (0.04, 0.15, 3.75, 0.8854167), (1, 1, 0.8854167, 0.8854167)]))
    assert lm == {"points": [[0.04, 0.15], [1, 1]], "curve": "linear"}
    # easeIn segment: tangents 0 and 2 x slope.
    lm, _ = unity_scan.binding_level_map(custom([(0, 0, 0, 0), (1, 1, 2, 2)]))
    assert lm == {"points": [[1, 1]], "curve": "easeIn"}
    # easeOut then a linear tail that continues the last slope: the tail is the levelMap's extension.
    lm, _ = unity_scan.binding_level_map(custom([(0, 0, 2, 2), (0.5, 0.5, 0, 1), (1, 1, 1, 1)]))
    assert lm == {"points": [[0.5, 0.5]], "curve": "easeOut"}
    # Mixed shapes cannot be one levelMap.
    lm, reason = unity_scan.binding_level_map(custom([(0, 0, 1, 1), (0.5, 0.5, 0, 0), (1, 1, 2, 2)]))
    assert lm is None and "share" in reason
    lm, reason = unity_scan.binding_level_map({"curveType": "3", "inputMax": "1", "outputParameter": "0"})
    assert lm is None and "exponential" in reason
    lm, _ = unity_scan.binding_level_map({"curveType": "0", "inputMin": "0.0165", "inputMax": "0", "outputMin": "0.1", "outputMax": "2"})
    assert lm == {"points": [[1, 2]], "intercept": 0.1, "curve": "linear"}
