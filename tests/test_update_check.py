"""Update notice (release feed) の挙動テスト。

守るべき性質は 3 つ (hapbeat-contracts specs/release-feed.md §5, DEC-053):
  1. 新しい版があれば 1 回だけ知らせる
  2. 一度知らせた版は、より新しい版が出るまで二度と知らせない
  3. 取得できない / 分からない時は黙る (誤報を出さない・失敗を通知しない)
"""
import json

import pytest

from hapbeat_helper import update_check as uc

ENTRY = {
    "name": "hapbeat-helper",
    "channel": "pypi",
    "latest": "0.4.0",
    "severity": "info",
    "upgrade": "pipx upgrade hapbeat-helper",
}


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """状態ファイルを tmp に閉じ込め、opt-out env の混入も断つ。"""
    monkeypatch.setattr(uc, "config_dir", lambda: tmp_path)
    monkeypatch.delenv("HAPBEAT_NO_UPDATE_CHECK", raising=False)
    yield


@pytest.fixture
def feed_ok(monkeypatch):
    monkeypatch.setattr(uc, "_fetch_entry", lambda: dict(ENTRY))


@pytest.fixture
def feed_down(monkeypatch):
    def _boom():
        raise OSError("network unreachable")
    monkeypatch.setattr(uc, "_fetch_entry", _boom)


# --------------------------------------------------------------------------
# version parsing


@pytest.mark.parametrize(("raw", "expected"), [
    ("0.3.1", (0, 3, 1)),
    ("v0.3.1", (0, 3, 1)),
    ("0.3.1d4", (0, 3, 1)),      # dev ビルドは同版扱い
    ("1.0", (1, 0)),
    ("", ()),
    (None, ()),
    ("nightly", ()),             # 解釈不能
])
def test_parse_version(raw, expected):
    assert uc.parse_version(raw) == expected


def test_is_newer():
    assert uc.is_newer("0.4.0", "0.3.1")
    assert uc.is_newer("0.3.10", "0.3.9")
    assert not uc.is_newer("0.3.1", "0.3.1")
    assert not uc.is_newer("0.3.1d4", "0.3.1")   # dev ビルドに更新を促さない
    assert not uc.is_newer("0.3.0", "0.3.1")
    assert not uc.is_newer("nightly", "0.3.1")   # 比較不能 → 黙る


# --------------------------------------------------------------------------
# notice lifecycle


def test_notifies_once_then_stays_quiet(feed_ok):
    first = uc.pending_notice("0.3.1")
    assert first is not None and "0.4.0" in first and "pipx upgrade" in first

    uc.mark_notified("0.3.1")
    assert uc.pending_notice("0.3.1") is None


def test_newer_release_breaks_the_silence(feed_ok, monkeypatch):
    uc.pending_notice("0.3.1")
    uc.mark_notified("0.3.1")
    assert uc.pending_notice("0.3.1") is None

    monkeypatch.setattr(uc, "_fetch_entry", lambda: {**ENTRY, "latest": "0.5.0"})
    # キャッシュを無効化して再取得させる (24h TTL を跨いだ状況)
    state = json.loads((uc.config_dir() / uc.STATE_FILENAME).read_text())
    state["checked_at"] = 0
    (uc.config_dir() / uc.STATE_FILENAME).write_text(json.dumps(state))

    again = uc.pending_notice("0.3.1")
    assert again is not None and "0.5.0" in again


def test_version_command_ignores_dismissal(feed_ok):
    """`hapbeat-helper version` は「見に行く場所」なので毎回出す。"""
    uc.mark_notified("0.3.1")
    assert uc.pending_notice("0.3.1") is None
    assert uc.pending_notice("0.3.1", respect_dismissed=False) is not None


def test_quiet_when_up_to_date(feed_ok):
    assert uc.pending_notice("0.4.0") is None
    assert uc.pending_notice("0.9.0") is None    # feed より先行 (dev checkout)


def test_silent_when_feed_unreachable(feed_down):
    """オフライン現場で「確認できませんでした」を出さない。"""
    assert uc.pending_notice("0.1.0") is None


def test_opt_out_env(feed_ok, monkeypatch):
    monkeypatch.setenv("HAPBEAT_NO_UPDATE_CHECK", "1")
    assert uc.opted_out()
    assert uc.pending_notice("0.1.0") is None


def test_opt_out_env_ignores_falsey_values(monkeypatch):
    for v in ("", "0", "false"):
        monkeypatch.setenv("HAPBEAT_NO_UPDATE_CHECK", v)
        assert not uc.opted_out()


def test_cache_avoids_refetch(monkeypatch):
    calls = {"n": 0}

    def _count():
        calls["n"] += 1
        return dict(ENTRY)

    monkeypatch.setattr(uc, "_fetch_entry", _count)
    uc.latest_release()
    uc.latest_release()
    assert calls["n"] == 1


def test_unwritable_state_dir_does_not_raise(feed_ok, monkeypatch):
    """状態を残せなくても落ちない (通知が再度出るのは許容)。"""
    monkeypatch.setattr(uc, "config_dir", lambda: "\0invalid")
    assert uc.pending_notice("0.3.1") is not None
    uc.mark_notified("0.3.1")  # must not raise
