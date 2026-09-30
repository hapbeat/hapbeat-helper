"""Material ledger (materials.py, the ``materials`` CLI, material_* WS messages).

Everything runs in tmp_path — the user's Downloads / materials dir are never
touched. The WS tests serve only HelperServer._handler (no UDP / mDNS).
"""

import asyncio
import hashlib
import json
import os
import sys
import zipfile
from pathlib import Path

import pytest
import websockets

from hapbeat_helper import cli, materials
from hapbeat_helper.materials import DownloadsWatcher, MaterialStore
from hapbeat_helper.server import HelperServer

# Realistic epoch: Windows cannot localize timestamps near 0.
T0 = 1_750_000_000.0
MAOU_PAGE = "https://maou.audio/se_battle03/"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def zones(monkeypatch):
    """Fake Zone.Identifier streams: {file name: text}. Unlisted = no mark."""
    table: dict[str, str] = {}
    monkeypatch.setattr(materials, "_read_zone_text", lambda path: table.get(Path(path).name))
    return table


def _zone(referrer: str | None, host: str | None = None) -> str:
    lines = ["[ZoneTransfer]", "ZoneId=3"]
    if referrer:
        lines.append(f"ReferrerUrl={referrer}")
    if host:
        lines.append(f"HostUrl={host}")
    return "\r\n".join(lines) + "\r\n"


@pytest.fixture
def downloads(tmp_path):
    d = tmp_path / "Downloads"
    d.mkdir()
    return d


@pytest.fixture
def store(tmp_path):
    return MaterialStore(tmp_path / "HapbeatMaterials")


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


# ── ingest ───────────────────────────────────────────────────


def test_ingest_new_copies_and_records(store, downloads, zones):
    src = _write(downloads / "se_gun01.wav", b"RIFF-gun")
    zones["se_gun01.wav"] = _zone(MAOU_PAGE, "https://maou.audio/sound/se_gun01.wav")

    result = store.ingest_dir(downloads)

    assert len(result.new) == 1 and not result.duplicates and not result.errors
    rec = result.new[0]
    assert rec["format"] == "hapbeat-material@1"
    assert rec["sha256"] == _sha(b"RIFF-gun")
    assert rec["site"] == "maou.audio"
    assert rec["referrerUrl"] == MAOU_PAGE
    assert rec["storePath"].startswith("store/maou.audio/")
    assert rec["archive"] is None
    copied = store.root / rec["storePath"]
    assert copied.read_bytes() == b"RIFF-gun"
    assert not os.access(copied, os.W_OK)  # read-only
    assert src.exists()  # copied, not moved
    assert store.sites_path.exists()  # seed written
    assert "maou.audio" in store.materials_md_path.read_text(encoding="utf-8")
    assert [r["sha256"] for r in store.read_ledger()] == [rec["sha256"]]


def test_ingest_duplicate_and_name_collision(store, downloads, zones):
    _write(downloads / "a.wav", b"one")
    store.ingest_dir(downloads)
    # Same content under another name -> duplicate; other content, same name -> " (2)".
    _write(downloads / "a copy.wav", b"one")
    (downloads / "a.wav").unlink()
    other = downloads / "sub"
    other.mkdir()
    _write(other / "a.wav", b"two")

    dup = store.ingest_dir(downloads, since_ts=0)
    assert not dup.new and len(dup.duplicates) == 1
    assert dup.duplicates[0]["sha256"] == _sha(b"one")

    second = store.ingest_dir(other, since_ts=0)
    assert second.new[0]["storePath"].endswith("a (2).wav")


def test_ingest_unknown_site_needs_review(store, downloads, zones):
    _write(downloads / "x.ogg", b"x")  # no Zone.Identifier
    result = store.ingest_dir(downloads)
    assert result.new[0]["site"] == "_unknown"
    assert result.new[0]["referrerUrl"] is None
    assert len(result.needs_review) == 1


def test_ingest_zip_extracts_audio_members(store, downloads, zones):
    zpath = downloads / "pack.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("sfx/door.ogg", b"door")
        zf.writestr("sfx/readme.txt", b"not audio")
        zf.writestr("__MACOSX/sfx/._door.ogg", b"junk")
    zones["pack.zip"] = _zone("https://kenney.nl/assets/ui-audio")

    result = store.ingest_dir(downloads)

    assert [r["originalName"] for r in result.new] == ["door.ogg"]
    rec = result.new[0]
    assert rec["sha256"] == _sha(b"door")
    assert rec["site"] == "kenney.nl"
    assert rec["archive"] == {
        "sha256": _sha(zpath.read_bytes()), "originalName": "pack.zip", "member": "sfx/door.ogg",
    }
    assert (store.root / rec["storePath"]).read_bytes() == b"door"


def test_ingest_dry_run_writes_nothing(store, downloads, zones):
    _write(downloads / "a.wav", b"a")
    result = store.ingest_dir(downloads, dry_run=True)
    assert len(result.new) == 1
    assert not store.ledger_path.exists()
    assert not (store.root / "store").exists()
    assert not store.state_path.exists()


def test_ingest_since_defaults_to_last_ingest(store, downloads, zones):
    old = _write(downloads / "old.wav", b"old")
    os.utime(old, (1_000_000, 1_000_000))
    now = 2_000_000_000.0
    new = _write(downloads / "new.wav", b"new")
    os.utime(new, (now - 86400, now - 86400))

    first = store.ingest_dir(downloads, now=now)  # first run: last 30 days
    assert [r["originalName"] for r in first.new] == ["new.wav"]
    assert store.load_state()["lastIngestAt"] == now

    later = _write(downloads / "later.wav", b"later")
    os.utime(later, (now + 10, now + 10))
    second = store.ingest_dir(downloads, now=now + 20)
    assert [r["originalName"] for r in second.new] == ["later.wav"]


def test_zone_identifier_parsing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        materials, "_read_zone_text",
        lambda p: _zone("https://www.otologic.jp/free/se/x.html", "about:internet"),
    )
    assert materials.read_zone_identifier(tmp_path / "f.wav") == {
        "hostUrl": None, "referrerUrl": "https://www.otologic.jp/free/se/x.html",
    }


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS alternate data stream")
def test_zone_identifier_real_stream(tmp_path):
    f = _write(tmp_path / "f.wav", b"f")
    try:
        with open(str(f) + ":Zone.Identifier", "w", encoding="utf-8") as ads:
            ads.write(_zone(MAOU_PAGE, "https://maou.audio/f.wav"))
    except OSError:
        pytest.skip("filesystem without alternate data streams")
    assert materials.read_zone_identifier(f) == {
        "hostUrl": "https://maou.audio/f.wav", "referrerUrl": MAOU_PAGE,
    }
    assert materials.read_zone_identifier(_write(tmp_path / "g.wav", b"g")) == {
        "hostUrl": None, "referrerUrl": None,
    }


def test_broken_ledger_line_is_skipped(store, downloads, zones):
    _write(downloads / "a.wav", b"a")
    store.ingest_dir(downloads)
    with open(store.ledger_path, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    assert len(store.read_ledger()) == 1
    assert store.resolve(_sha(b"a"))["kind"] == "material"


# ── license resolution ───────────────────────────────────────


def test_license_order_override_then_site_then_unknown(store, downloads, zones):
    _write(downloads / "m.wav", b"m")
    zones["m.wav"] = _zone("https://maoudamashii.jokersounds.com/list/se1.html")  # alias
    _write(downloads / "u.wav", b"u")
    zones["u.wav"] = _zone("https://example.com/sfx")
    store.ingest_dir(downloads)

    m = store.resolve(_sha(b"m"))["originals"][0]
    assert m["site"] == "maou.audio"
    assert m["license"]["id"] == "CC-BY-4.0" and m["needsReview"] is False

    u = store.resolve(_sha(b"u"))["originals"][0]
    assert u["site"] == "example.com"
    assert u["license"]["id"] == "unknown" and u["needsReview"] is True

    # A site rule added later applies to what is already ingested.
    store.set_site_license("example.com", {"id": "CC0-1.0", "name": "CC0 1.0"})
    u = store.resolve(_sha(b"u"))["originals"][0]
    assert u["license"]["id"] == "CC0-1.0" and u["needsReview"] is False

    # Per-file override wins over the site rule.
    store.set_override(_sha(b"m"), {"id": "site-terms", "name": "special"})
    m = store.resolve(_sha(b"m"))["originals"][0]
    assert m["license"]["id"] == "site-terms"


def test_per_asset_site_needs_review(store, downloads, zones):
    _write(downloads / "f.wav", b"f")
    zones["f.wav"] = _zone("https://freesound.org/people/x/sounds/1/")
    result = store.ingest_dir(downloads)
    assert len(result.needs_review) == 1
    orig = store.resolve(_sha(b"f"))["originals"][0]
    assert orig["license"]["id"] == "unknown" and orig["needsReview"] is True


# ── derived ──────────────────────────────────────────────────


def _ingest_two(store, downloads, zones):
    _write(downloads / "a.wav", b"a")
    zones["a.wav"] = _zone(MAOU_PAGE)
    _write(downloads / "b.wav", b"b")
    zones["b.wav"] = _zone("https://kenney.nl/assets/x")
    store.ingest_dir(downloads)
    return _sha(b"a"), _sha(b"b")


def test_derived_resolves_recursively_and_stops_on_cycle(store, downloads, zones):
    a, b = _ingest_two(store, downloads, zones)
    d1, d2, d3 = "1" * 64, "2" * 64, "3" * 64
    assert store.register_derived(d1, [a], "t", name="step1")
    assert store.register_derived(d2, [d1, b], "t", name="mix")
    assert store.register_derived(d2, [b, d1], "t") is False  # same parents -> no-op
    # Cycle d3 -> d3 and d3 -> d2
    store.register_derived(d3, [d3, d2], "t")

    res = store.resolve(d3)
    assert res["kind"] == "derived"
    assert sorted(o["sha256"] for o in res["originals"]) == sorted([a, b])
    assert store.resolve("f" * 64) == {"kind": "unknown", "originals": []}


def test_derived_depth_limit(store, downloads, zones):
    a, _ = _ingest_two(store, downloads, zones)
    chain = [f"{i:064x}" for i in range(1, 40)]
    store.register_derived(chain[0], [a], "t")
    for child, parent in zip(chain[1:], chain):
        store.register_derived(child, [parent], "t")
    assert store.resolve(chain[5])["originals"][0]["sha256"] == a
    assert store.resolve(chain[-1])["originals"] == []  # deeper than 32


# ── credits ──────────────────────────────────────────────────


def test_credits_markdown(store, downloads, zones):
    a, b = _ingest_two(store, downloads, zones)
    md = store.credits_markdown(
        [("gunshot_1.wav", a), ("click.wav", b), ("ui_tick.wav", "e" * 64)], "test-tool",
    )
    assert md.startswith("<!-- generated by test-tool from the material ledger — do not edit -->")
    assert "## 魔王魂 — CC BY 4.0（https://maou.audio/rule/）\nクレジット: 魔王魂\n" in md
    assert f"- gunshot_1.wav ← a.wav（{MAOU_PAGE}）" in md
    assert "## Kenney (kenney.nl) — CC0 1.0" in md
    assert md.rstrip().endswith("## 出典不明（要確認）\n- ui_tick.wav")


# ── CLI ──────────────────────────────────────────────────────


def test_cli_ingest_list_show_credits(tmp_path, downloads, zones, monkeypatch, capsys):
    root = tmp_path / "HapbeatMaterials"
    monkeypatch.setattr(materials, "default_materials_dir", lambda config=None: root)
    _write(downloads / "se.wav", b"se")
    zones["se.wav"] = _zone(MAOU_PAGE)

    assert cli.main(["materials", "ingest", "--dir", str(downloads), "--dry-run"]) == 0
    assert "(dry run)" in capsys.readouterr().out
    assert not (root / "ledger.jsonl").exists()

    assert cli.main(["materials", "ingest", "--dir", str(downloads)]) == 0
    assert "new 1" in capsys.readouterr().out

    assert cli.main(["materials", "list", "--site", "maou.audio"]) == 0
    assert "(1 materials)" in capsys.readouterr().out

    assert cli.main(["materials", "show", str(downloads / "se.wav")]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["kind"] == "material" and shown["sha256"] == _sha(b"se")

    assert cli.main(["materials", "credits", str(downloads)]) == 0
    assert "- se.wav ← se.wav" in capsys.readouterr().out

    assert cli.main([
        "materials", "set-license", "--sha", _sha(b"se"), "--license-id", "CC0-1.0",
    ]) == 0
    capsys.readouterr()
    assert MaterialStore(root).resolve(_sha(b"se"))["originals"][0]["license"]["id"] == "CC0-1.0"

    assert cli.main(["materials", "where"]) == 0
    assert capsys.readouterr().out.strip() == str(root)


def test_config_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(materials.update_check, "config_dir", lambda: tmp_path)
    assert materials.watch_downloads_enabled() is False
    (tmp_path / "config.toml").write_text(
        'materials_dir = "~/Sounds/Ledger"\nmaterials_watch_downloads = true\n',
        encoding="utf-8",
    )
    assert materials.watch_downloads_enabled() is True
    assert materials.default_materials_dir() == Path("~/Sounds/Ledger").expanduser()
    assert materials._parse_flat_toml(
        'materials_watch_downloads = true\nmaterials_dir = "x"\n[other]\nk = 1\n'
    ) == {"materials_watch_downloads": True, "materials_dir": "x"}


# ── watcher ──────────────────────────────────────────────────


def test_watcher_ingests_after_size_is_stable(store, downloads, zones):
    t = [T0]
    watcher = DownloadsWatcher(store, downloads, clock=lambda: t[0])
    f = downloads / "dl.wav"
    f.write_bytes(b"par")
    os.utime(f, (T0 + 1, T0 + 1))

    assert watcher.poll() is None  # first sighting
    f.write_bytes(b"partial-more")  # still growing
    os.utime(f, (T0 + 2, T0 + 2))
    assert watcher.poll() is None
    t[0] = T0 + 10
    result = watcher.poll()  # same size twice -> complete
    assert result is not None and [r["originalName"] for r in result.new] == ["dl.wav"]
    assert store.load_state()["lastIngestAt"] == T0 + 10
    assert watcher.poll() is None  # handled, not re-ingested


def test_watcher_ignores_files_older_than_last_ingest(store, downloads, zones):
    store.mark_ingested(T0 + 50)
    old = _write(downloads / "old.wav", b"old")
    os.utime(old, (T0 + 40, T0 + 40))
    watcher = DownloadsWatcher(store, downloads, clock=lambda: T0 + 60)
    assert watcher.poll() is None
    assert watcher.poll() is None
    assert not store.ledger_path.exists()


def test_server_watcher_off_by_default():
    server = HelperServer()
    assert server._materials_watch_dir is None
    assert server._materials_store is None


# ── WS messages ──────────────────────────────────────────────


@pytest.fixture
async def ws_server(store):
    server = HelperServer(materials_dir=store.root)
    async with websockets.serve(server._handler, "127.0.0.1", 0) as srv:
        port = srv.sockets[0].getsockname()[1]
        yield server, f"ws://127.0.0.1:{port}"


async def _call(uri: str, msg_type: str, payload: dict, reply_type: str) -> dict:
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": msg_type, "payload": payload}))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3.0
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), deadline - loop.time()))
            if msg["type"] == reply_type:
                return msg["payload"]


async def test_ws_lookup_register_credits(ws_server, store, downloads, zones):
    _, uri = ws_server
    a, _ = _ingest_two(store, downloads, zones)
    derived = "d" * 64

    res = await _call(uri, "material_lookup", {"requestId": "q1", "sha256s": [a.upper()]},
                      "material_lookup_result")
    assert res["requestId"] == "q1"
    assert res["results"][a]["kind"] == "material"

    res = await _call(uri, "material_register_derived", {
        "requestId": "q2", "sha256": derived, "parents": [a],
        "tool": "hapbeat-studio-editor", "name": "gunshot_heavy", "note": "lpf 200Hz",
    }, "material_register_derived_result")
    assert res == {"requestId": "q2", "ok": True}
    # Same sha+parents again: still ok, no second record.
    res = await _call(uri, "material_register_derived", {
        "sha256": derived, "parents": [a], "tool": "hapbeat-studio-editor",
    }, "material_register_derived_result")
    assert res == {"ok": True}
    assert sum(1 for r in store.read_ledger() if r["format"] == "hapbeat-derived@1") == 1

    res = await _call(uri, "material_lookup", {"sha256s": [derived]}, "material_lookup_result")
    assert res["results"][derived]["kind"] == "derived"
    assert res["results"][derived]["originals"][0]["sha256"] == a

    res = await _call(uri, "material_credits", {
        "requestId": "q3", "sha256s": [derived], "toolName": "hapbeat-studio",
    }, "material_credits_result")
    assert res["requestId"] == "q3"
    assert "- gunshot_heavy ← a.wav" in res["markdown"]
    assert "generated by hapbeat-studio" in res["markdown"]


async def test_ws_rejects_bad_payloads(ws_server):
    _, uri = ws_server
    res = await _call(uri, "material_lookup", {"sha256s": ["a" * 64] * 501},
                      "material_lookup_result")
    assert res["results"] == {} and "error" in res
    res = await _call(uri, "material_register_derived", {
        "requestId": 7, "sha256": "a" * 64, "parents": [], "tool": "t",
    }, "material_register_derived_result")
    assert res["requestId"] == 7 and res["ok"] is False
    res = await _call(uri, "material_credits", {"sha256s": ["nope"], "toolName": "t"},
                      "material_credits_result")
    assert res["markdown"] == "" and "error" in res
