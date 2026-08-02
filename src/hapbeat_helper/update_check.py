"""Update notice — 「新しい hapbeat-helper が出ています」を 1 回だけ伝える。

情報源は Hapbeat の release feed (``https://devtools.hapbeat.com/releases.json``)。
生成は devtools-site の CI、仕様は hapbeat-contracts ``specs/release-feed.md``
(DEC-053)。feed は GitHub のタグではなく **PyPI に実際に上がっている版** を
載せているので、ここに出る版は必ず ``pipx upgrade`` で取得できる。

守っている作法 (spec §5):

* **1 版につき 1 回だけ**通知する。一度出した版は記録し、より新しい版が出るまで
  黙る。起動のたびに同じ行を出すのは、版を意図的に固定している人にとって
  ノイズでしかない。
* 取得失敗は**完全にサイレント**。Hapbeat は外部ネットワークの無い現場でも
  使われるので、「最新版を確認できませんでした」は出さない。
* 起動を**ブロックしない** (バックグラウンドスレッドで実行)。
* ``HAPBEAT_NO_UPDATE_CHECK=1`` または ``--no-update-check`` で無効化できる。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

FEED_URL = "https://devtools.hapbeat.com/releases.json"
PRODUCT_ID = "helper"
TIMEOUT_S = 3.0
CACHE_TTL_S = 24 * 60 * 60
STATE_FILENAME = "update-check.json"


def config_dir() -> Path:
    """設定・状態ファイルの置き場 (OS 慣習に従う)。"""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home())))
        return base / "hapbeat-helper"
    return Path.home() / ".config" / "hapbeat-helper"


def opted_out() -> bool:
    return os.environ.get("HAPBEAT_NO_UPDATE_CHECK", "").strip() not in ("", "0", "false")


# --------------------------------------------------------------------------
# version compare


def parse_version(v: str | None) -> tuple[int, ...]:
    """``0.3.1`` / ``v0.3.1`` / ``0.3.1d4`` → ``(0, 3, 1)``.

    dev ビルドの ``dN`` 接尾辞は落とす — ``0.3.1d4`` を使っている人に
    「0.3.1 へ更新してください」と促さないため。解釈できない値は ``()``。
    """
    if not v:
        return ()
    s = str(v).strip().lstrip("vV")
    head = s.split("-", 1)[0].split("+", 1)[0]
    out: list[int] = []
    for part in head.split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break  # "1d4" → 1
        if not digits:
            return ()
        out.append(int(digits))
    return tuple(out)


def is_newer(candidate: str | None, baseline: str | None) -> bool:
    """``candidate`` が ``baseline`` より新しいか。比較不能なら False (黙る側に倒す)。"""
    a, b = parse_version(candidate), parse_version(baseline)
    if not a or not b:
        return False
    return a > b


# --------------------------------------------------------------------------
# state file


def _state_path() -> Path:
    return config_dir() / STATE_FILENAME


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass  # 状態を残せないだけ。通知が再度出るのは許容


# --------------------------------------------------------------------------
# feed


def _fetch_entry() -> dict[str, Any] | None:
    req = urllib.request.Request(FEED_URL, headers={"User-Agent": "hapbeat-helper"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as res:  # noqa: S310 (fixed https URL)
        feed = json.loads(res.read().decode("utf-8"))
    if feed.get("schema_version") != 1:
        return None
    entry = feed.get("products", {}).get(PRODUCT_ID)
    return entry if isinstance(entry, dict) else None


def latest_release(*, use_cache: bool = True) -> dict[str, Any] | None:
    """feed から helper のエントリを得る。取得できなければ None。

    24 時間はキャッシュを使う (feed は deploy 単位でしか変わらない)。
    """
    state = _load_state()
    if use_cache:
        cached = state.get("entry")
        checked_at = state.get("checked_at", 0)
        if cached and (time.time() - checked_at) < CACHE_TTL_S:
            return cached

    try:
        entry = _fetch_entry()
    except Exception:
        return None  # offline / timeout / DNS — 静かに諦める
    if not entry:
        return None

    state["entry"] = entry
    state["checked_at"] = time.time()
    _save_state(state)
    return entry


# --------------------------------------------------------------------------
# public API


def pending_notice(current: str, *, respect_dismissed: bool = True) -> str | None:
    """通知すべきなら 1 行のメッセージを返す。無ければ None。

    ``respect_dismissed=False`` で「1 版 1 回」の抑制を無視する
    (``version`` サブコマンドのような *ユーザーが自分で聞きに来た* 場面用)。
    """
    if opted_out():
        return None
    entry = latest_release()
    if not entry:
        return None
    latest = entry.get("latest")
    if not is_newer(latest, current):
        return None

    if respect_dismissed:
        notified = _load_state().get("notified")
        # まだ何も通知していなければ出す。通知済みなら、それより新しい版の時だけ。
        # (is_newer は baseline が無いと False を返すので、None を先に弾く)
        if notified and not is_newer(latest, notified):
            return None

    upgrade = entry.get("upgrade") or "pipx upgrade hapbeat-helper"
    return f"  → hapbeat-helper {latest} が公開されています:  {upgrade}"


def mark_notified(current: str) -> None:
    """いま通知した版を記録し、次回以降は黙るようにする。"""
    entry = latest_release()
    if entry and entry.get("latest"):
        state = _load_state()
        state["notified"] = entry["latest"]
        _save_state(state)


def notify_in_background(current: str, *, stream=None) -> None:
    """起動をブロックせずにチェックし、必要なら 1 行だけ出す。

    daemon スレッドなので、チェックが終わる前にプロセスが落ちても後腐れがない。
    """
    if opted_out():
        return

    out = stream if stream is not None else sys.stderr

    def _run() -> None:
        try:
            msg = pending_notice(current)
            if msg:
                print(msg, file=out, flush=True)
                mark_notified(current)
        except Exception:
            pass  # 更新通知が原因で daemon を壊さない

    threading.Thread(target=_run, name="hapbeat-update-check", daemon=True).start()
