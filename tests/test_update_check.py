"""Update notice (release feed) の挙動テスト。

守るべき性質 (hapbeat-contracts specs/release-feed.md §5, DEC-053):
  1. 新しい版があれば起動ごとに 1 行知らせる (§5.1 B — 閉じる操作が無い表示なので
     版ごとの永続抑制はしない。daemon の起動自体が稀でうるさくならない)
  2. 取得できない / 分からない時は黙る (誤報を出さない・失敗を通知しない)
  3. 起動をブロックしない
"""
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


def test_notifies_on_every_start(feed_ok):
    """閉じる操作の無い 1 行なので、起動のたびに出してよい (§5.1 B)。

    版ごとに永続抑制すると「一度見逃したら二度と出ない」方の害が大きい。
    daemon の起動は稀なのでうるさくならない。
    """
    first = uc.pending_notice("0.3.1")
    assert first is not None and "0.4.0" in first and "pipx upgrade" in first
    # 2 回目 (= 次の起動) でも同じように出る
    assert uc.pending_notice("0.3.1") == first


def test_notice_is_english(feed_ok):
    """CLI 出力は英語で統一 (ログ・コンソール出力の既存慣習に合わせる)。"""
    msg = uc.pending_notice("0.3.1")
    assert msg is not None
    assert not any("぀" <= ch <= "鿿" for ch in msg)


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
    """キャッシュを保存できない環境でも落ちない (毎回 fetch するだけ)。"""
    monkeypatch.setattr(uc, "config_dir", lambda: "\0invalid")
    assert uc.pending_notice("0.3.1") is not None


def test_notify_in_background_prints_and_never_raises(feed_down, capsys):
    """取得に失敗しても daemon を巻き込まない。"""
    import threading
    uc.notify_in_background("0.1.0")
    for t in threading.enumerate():
        if t.name == "hapbeat-update-check":
            t.join(timeout=5)
    assert capsys.readouterr().err == ""
