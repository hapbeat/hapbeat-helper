# OTA 詰まり (helper 再起動で直る) の根治

**作成日:** 2026-05-09
**起点:** workspace device-firmware セッション (v0.1.0 release 後)
**優先度:** 高 (OTA 信頼性のリリースブロッカー)

## 症状

- v0.0.37 / v0.1.0 device で OTA が稀に「詰まる」
- 失敗ログは出ない、Studio UI が「送信中…」のまま無反応
- **helper を再起動すると次の OTA は通る** → helper 側の状態残留が真因

## 仮説と原因候補 (優先度順)

### 候補 A — `_tcp_locks[target]` (asyncio.Lock) がスタック中
**もっとも怪しい。**

`_handle_ota` (server.py:875) で `async with self._get_tcp_lock(target):` が executor 内の `_do_ota_to_device` を await。executor thread が Python socket sendall でブロックすると、コルーチン側は永久に await のまま → lock 解放されず → 以降 **同じ IP への TCP 操作 (OTA, get_info, set_*, 含む log_tail 以外全部) が無限待機**。

helper 再起動だけで直る挙動と完全一致する。

ただし `tcp_client.send_raw` には `timeout` 引数で `settimeout` していて、Python sendall は `socket.timeout` を投げるはず。理論上は OSError → caught → `finally` で `ota_in_progress.discard` → lock 解放、になるはず。

実装上の注意: `socket.sendall` 内部の `send` は timeout を尊重するが、Windows で WinError 10054 とのレース時に **ごく稀に block する** との報告あり (ユーザー環境は Windows 10/11)。

**対策:** executor 呼び出し全体を `asyncio.wait_for(...)` で包み、想定時間 (1.7 MB / 5 KB/s = ~340s なので余裕を持って 600s) を超えたら `TimeoutError` で強制 cancel + 状態 cleanup。

```python
try:
    ok, msg = await asyncio.wait_for(
        loop.run_in_executor(None, _do_ota_to_device, ...),
        timeout=600.0,
    )
except asyncio.TimeoutError:
    ok, msg = False, "phase=stuck: OTA executor blocked >600s — recovered"
    logger.error("OTA executor stuck for IP %s — forcing recovery", target)
finally:
    self._ota_in_progress.discard(target)
    # Note: the executor thread may still be alive, but the asyncio task
    # has returned, so the lock is released and subsequent OTA can proceed.
```

### 候補 B — log_tail supervisor の displacement race

OTA 直前に Studio が log_tail を張っていた場合:
1. `_handle_ota` が `ota_in_progress.add(target)` → lock 取得 → executor 起動
2. executor 内 `TcpRawConnection.connect()` → 既存 log_tail を device 側で displace
3. log_tail thread が `_log_tail_worker` から exit
4. supervisor が `target in ota_in_progress` を確認して sleep — **OK**

ただし、もし `_log_tail_worker` の close 処理が socket level で時間が掛かる (CLOSE_WAIT 待機) と、OTA 開始の `connect()` が device 側で旧スロット解放を待つ間にタイムアウトする可能性。

**対策:** OTA 開始直前に明示的に log_tail を stop して合流 (join with timeout)、OTA 完了後に再起動。displacement race を完全排除。

```python
async def _handle_ota(self, ws, payload):
    target = ...
    # Pause log_tail BEFORE acquiring lock so device's TCP slot is free.
    paused_log = self._pause_log_tail(target)  # set stop event, join 1.0s
    try:
        self._ota_in_progress.add(target)
        try:
            async with self._get_tcp_lock(target):
                ok, msg = await asyncio.wait_for(
                    loop.run_in_executor(None, _do_ota_to_device, ...),
                    timeout=600.0,
                )
        finally:
            self._ota_in_progress.discard(target)
    finally:
        if paused_log:
            self._resume_log_tail(target)  # spawn fresh supervisor
```

### 候補 C — 失敗 OTA 後の device TCP slot リーク

device 側 `processOtaData` の disconnect 検知:
```cpp
if (!s_client.connected()) {
    Update.abort();
    s_ota_active = false;
    return;
}
```
`s_client.stop()` を呼んでいない。次の `s_server.available()` で displace されるはずだが、displace 前に helper が新 OTA を開始した場合、connect が waiting 状態で詰まる可能性。

**対策 (device 側、別 instruction):** disconnect 検知時に `s_client.stop()` を明示呼び出し。

## 検証手順

### 再現

1. Studio で LogDrawer を開く (log_tail 起動)
2. OTA を実行 → 成功
3. すぐ次の OTA を実行 → 「送信中…」で詰まる (再現率は環境依存; 5 回中 1-2 回程度)

### 修正後の確認

1. OTA 連続 10 回で詰まりなしを確認
2. 詰まりが起きてもタイムアウト後に自動回復することを確認 (helper 再起動不要)
3. helper ログに `OTA executor stuck` が出ないことを確認 (= 候補 A の症状が消えた)

## 関連

- 既存: `instructions/completed/instructions-log-tail-backoff-during-ota-202605092045.md` (log_tail backoff)
- 既存: `instructions/completed/applied-log-tail-supervisor-202605090140.md` (supervisor 化)
- 関連 device 側: `s_client.stop()` 追加は別途 device repo に instruction を作る予定 (本指示書では helper 側のみ対応)

## 完了条件

- [ ] `_handle_ota` に `asyncio.wait_for` 600s safety net 追加
- [ ] OTA 開始前 log_tail pause / 完了後 resume の実装 (displacement race 排除)
- [ ] helper version bump (0.2.4)
- [ ] CHANGELOG / README に記載
- [ ] 本ファイルを `instructions/completed/` へ移動
