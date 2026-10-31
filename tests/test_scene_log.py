"""The recording's log (scene_log.py, hapbeat-contracts specs/scene-recording.md section 5)."""

from hapbeat_helper.scene_log import MARKER, parse_log_text, read_log


def test_unreal_prefixed_lines():
    text = "\n".join([
        "[2026.10.31-10.00.00:000][  0]LogInit: Display: Engine started",
        '[2026.10.31-10.00.01:000][  1]LogTemp: Display: HAPBEAT_SCENE {"t":"start","schema_version":1,"fps":30}',
        '[2026.10.31-10.00.02:000][ 31]LogTemp: Display: HAPBEAT_SCENE {"t":"cue","frame":30,"cue":"roar"} trailing',
        "[2026.10.31-10.00.03:000][ 61]LogTemp: Display: something else",
        '[2026.10.31-10.00.04:000][ 91]LogTemp: Display: HAPBEAT_SCENE {"t":"end","frames":90}',
    ])
    log = parse_log_text(text)
    assert [r["t"] for r in log.records] == ["start", "cue", "end"]
    assert log.lines == [2, 3, 5]
    assert log.records[1] == {"t": "cue", "frame": 30, "cue": "roar"}
    assert log.errors == [] and log.warnings == []


def test_unity_debug_log_with_stack_trace():
    text = "\n".join([
        'HAPBEAT_SCENE {"t":"start","schema_version":1,"fps":30,"project":"handdemo"}',
        "UnityEngine.Debug:Log (object)",
        "HandDemoRecorder:Start () (at Assets/HandDemo/HandDemoRecorder.cs:42)",
        "",
        'HAPBEAT_SCENE {"t":"cue","frame":3,"source":"unity:d7dd6680a2c34f1e9b0c5d4e3f2a1b0c/ce65e997e9d54d2eabde0d646dea67c7"}',
        "UnityEngine.Debug:Log (object)",
        "",
        'HAPBEAT_SCENE {"t":"end","frames":10}',
        "UnityEngine.Debug:Log (object)",
    ])
    log = parse_log_text(text)
    assert [r["t"] for r in log.records] == ["start", "cue", "end"]
    assert log.errors == [] and log.warnings == []


def test_plain_jsonl_file(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(
        "﻿" + '{"t":"start","schema_version":1,"fps":30}\n\n'
        '{"t":"level","frame":2,"values":{"rub:level:left":0.4}}\n'
        '{"t":"end","frames":5}\n',
        encoding="utf-8",
    )
    log = read_log(path)
    assert [r["t"] for r in log.records] == ["start", "level", "end"]
    assert log.lines == [1, 3, 4]
    assert log.errors == [] and log.warnings == []


def test_undecodable_bytes_are_replaced(tmp_path):
    path = tmp_path / "Player.log"
    path.write_bytes(b"\xff\xfe garbage\n" + (MARKER + '{"t":"start","schema_version":1,"fps":30}\n').encode())
    log = read_log(path)
    assert [r["t"] for r in log.records] == ["start"]


def test_last_start_wins():
    text = "\n".join([
        '{"t":"start","schema_version":1,"fps":30}',
        '{"t":"cue","frame":1,"cue":"Bad Name"}',
        "not json at all",
        '{"t":"start","schema_version":1,"fps":60}',
        '{"t":"cue","frame":2,"cue":"roar"}',
        '{"t":"end","frames":10}',
    ])
    log = parse_log_text(text)
    assert log.records[0]["fps"] == 60
    assert [r["t"] for r in log.records] == ["start", "cue", "end"]
    assert log.lines == [4, 5, 6]
    assert log.errors == [] and log.warnings == []  # problems before the last start are not this recording's


def test_malformed_marker_line_is_a_warning():
    text = "\n".join([
        'x HAPBEAT_SCENE {"t":"start","schema_version":1,"fps":30}',
        'x HAPBEAT_SCENE {"t":"cue","frame":',
        'x HAPBEAT_SCENE [1, 2]',
        'x HAPBEAT_SCENE {"t":"end","frames":3}',
    ])
    log = parse_log_text(text)
    assert [r["t"] for r in log.records] == ["start", "end"]
    assert len(log.errors) == 2
    assert log.errors[0].startswith("line 2:") and log.errors[1].startswith("line 3:")
    assert log.warnings == []


def test_malformed_plain_line_is_an_error():
    log = parse_log_text('{"t":"start","schema_version":1,"fps":30}\n{"t":"cue",\n{"t":"end","frames":3}\n')
    assert log.is_log and len(log.errors) == 1 and log.errors[0].startswith("line 2:")


def test_not_a_log():
    log = parse_log_text('Mono path[0] = ...\nsome engine text\n{"t":"start"}\nmore text\n')
    assert log.is_log is False
    assert log.records == [] and log.errors == [] and log.warnings == []


def test_lines_after_end_are_left_out():
    text = "\n".join([
        '{"t":"start","schema_version":1,"fps":30}',
        '{"t":"end","frames":3}',
        '{"t":"cue","frame":1,"cue":"Bad Name"}',
        "not json",
        '{"t":"end","frames":9}',
    ])
    log = parse_log_text(text)
    assert [r["t"] for r in log.records] == ["start", "end"]
    assert log.errors == []
    assert len(log.warnings) == 1 and "3 line(s) after the end record (line 2)" in log.warnings[0]


def test_level_by_source():
    src = "unity:d7dd6680a2c34f1e9b0c5d4e3f2a1b0c/6a24573fe8fc487a88a93a3871cc25e7"
    text = "\n".join([
        '{"t":"start","schema_version":1,"fps":30}',
        '{"t":"level","frame":1,"source":"%s","value":0.5,"hand":"left"}' % src,
        '{"t":"level","frame":1,"source":"%s","value":-1,"hand":"up"}' % src,
        '{"t":"level","frame":1,"source":"%s","value":1,"values":{"rub:level":1}}' % src,
        '{"t":"end","frames":3}',
    ])
    log = parse_log_text(text)
    assert not any(e.startswith("line 2:") for e in log.errors)
    assert any(e.startswith("line 3: level: value") for e in log.errors)
    assert any(e.startswith("line 3: level: hand") for e in log.errors)
    assert any(e.startswith("line 4: level: exactly one of values / source") for e in log.errors)


def test_unknown_type_is_a_warning():
    log = parse_log_text('{"t":"start","schema_version":1,"fps":30}\n{"t":"camera","frame":1}\n{"frame":2}\n')
    assert [r["t"] for r in log.records] == ["start"]
    assert len(log.warnings) == 2 and "camera" in log.warnings[0] and log.warnings[1].startswith("line 3:")


def test_record_errors():
    text = "\n".join([
        '{"t":"start","schema_version":2,"fps":0}',
        '{"t":"cue","frame":1,"cue":"roar","source":"unity:abc/def"}',
        '{"t":"cue","frame":-1,"cue":"Roar"}',
        '{"t":"cue","frame":1}',
        '{"t":"level","frame":1,"values":{"rub:volume":1,"rub:level":-1}}',
        '{"t":"take","frame":0,"label":3,"expect":["Grab"],"allow":"grab"}',
        '{"t":"take_end","frame":1.5}',
        '{"t":"fail","frame":"x","reason":"no grab"}',
        '{"t":"end","frames":0}',
    ])
    log = parse_log_text(text)
    for needle in (
        "line 1: start: schema_version",
        "line 1: start: fps",
        "line 2: cue: exactly one of cue / source",
        "line 3: cue: frame",
        "line 3: cue: cue must be",
        "line 4: cue: exactly one of cue / source",
        "line 5: level: rub:volume is not a column name",
        "line 5: level: rub:level must be a number >= 0",
        "line 6: take: label",
        "line 6: take: expect",
        "line 6: take: allow",
        "line 7: take_end: frame",
        "line 8: fail: frame",
        "line 9: end: frames",
    ):
        assert any(e.startswith(needle) for e in log.errors), (needle, log.errors)
