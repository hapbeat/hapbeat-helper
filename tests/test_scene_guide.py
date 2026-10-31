"""``scene guide`` / ``scene template``: the guide and templates ship as package data and match the log format."""

import re
from pathlib import Path

import pytest

from hapbeat_helper import cli, scene_guide, scene_log

ROOT = Path(__file__).resolve().parents[1]


def test_guide_has_every_engine_and_the_commands():
    text = scene_guide.guide()
    for engine in scene_guide.ENGINES:
        assert scene_guide.guide(engine).startswith(text.split("\n", 1)[0])
    for command in ("cues scan", "cues init", "cues validate", "scene session", "scene check", "scene template"):
        assert command in text
    assert "No Network" in text and "sound" in text


def test_templates_use_the_log_format():
    unity = scene_guide.template_files("unity")
    assert set(unity) == {"HapbeatSceneLog.cs", "HapbeatSceneSdkCapture.cs"}
    unreal = scene_guide.template_files("unreal")["HapbeatSceneLog.h"]
    js = scene_guide.template_files("js")["hapbeat-scene-log.mjs"]
    assert f'"{scene_log.MARKER}"' in unity["HapbeatSceneLog.cs"]
    assert scene_log.MARKER + "%s" in unreal
    for text in (unity["HapbeatSceneLog.cs"], unreal, js):
        for record in ("start", "take", "take_end", "cue", "level", "fail", "end"):
            assert re.search(r"[\\\"']" + record + r"[\\\"']", text), record   # "start", \"start\" or 'start'
        assert "schema_version" in text
    assert '"unity:" + mapGuid + "/"' in unity["HapbeatSceneSdkCapture.cs"]


def test_package_data_lists_the_files():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = data["tool"]["setuptools"]["package-data"]["hapbeat_helper.scene_guide"]
    folder = ROOT / "src" / "hapbeat_helper" / "scene_guide"
    files = [p for p in folder.rglob("*") if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts]
    for f in files:
        rel = f.relative_to(folder).as_posix()
        assert any(Path(rel).match(pat) for pat in patterns), rel


def test_cli_guide_and_template(tmp_path, capsys):
    assert cli.main(["scene", "guide", "--engine", "unreal"]) == 0
    out = capsys.readouterr().out
    assert "## Unreal Engine" in out and "## Unity" not in out
    assert cli.main(["scene", "template", "unity", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "HapbeatSceneLog.cs").is_file() and (tmp_path / "HapbeatSceneSdkCapture.cs").is_file()
    (tmp_path / "HapbeatSceneLog.cs").write_text("edited", encoding="utf-8")
    assert cli.main(["scene", "template", "unity", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "HapbeatSceneLog.cs").read_text(encoding="utf-8") == "edited"   # kept without --force
    assert "kept" in capsys.readouterr().out
    assert cli.main(["scene", "template", "unity", "--out", str(tmp_path), "--force"]) == 0
    assert (tmp_path / "HapbeatSceneLog.cs").read_text(encoding="utf-8") != "edited"
