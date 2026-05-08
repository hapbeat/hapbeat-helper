# applied: log_tail に self-healing supervisor 追加 (v0.2.3)

**起点 repo:** hapbeat-sdk-workspace (workspace session)
**作成日:** 2026-05-09
**前回からの差分:** 0.2.2 (WS-close cleanup) + 本変更 = 0.2.3

## 背景

ユーザー報告 (2026-05-09): firmware 0.0.22 を OTA 後、UI 設定で色を変更 → Deploy → 物理ボタンを長押し、しても LogDrawer に何も出ない。

## 真因

helper 0.2.2 / firmware 0.0.22 の組み合わせで以下の連鎖が発生:

1. Studio が LogDrawer を開く → helper `_log_tail_worker` 起動 → firmware
   `s_log_stream=true`、TCP slot を log_tail subscriber が占有
2. ユーザーが Deploy / OTA / get_info などを実行 → helper が新しい TCP を
   firmware に張る → firmware の displacement gate (`!s_log_stream` を
   2026-05-08 に撤去済) で **log_tail が bump される**
3. log_tail thread の TCP は close され thread は exit → でも `_log_threads`
   dict エントリは残る (Thread 自身では削除しない)
4. Studio LogDrawer は `subscribedIp` を保持したまま。再 subscribe しない
5. → 以降のすべての log は s_log_stream=false ゲートで握り潰される

## 修正

`_handle_subscribe_logs` 内で `_log_tail_worker` を **supervisor ラッパで起動**:

```python
def _supervised_worker() -> None:
    backoff = 0.3
    while not stop.is_set():
        try:
            _log_tail_worker(target, stop, relay)
        except Exception:
            logger.exception("log tail (%s) crashed; restarting", target)
        if stop.is_set():
            break
        time.sleep(backoff)
        backoff = min(backoff * 1.5, 2.0)
        if not stop.is_set():
            logger.info("log tail (%s) auto-restart", target)
    # Natural exit cleanup (= unsubscribe / WS close path)
    self._log_stop_flags.pop(target, None)
    self._log_threads.pop(target, None)
```

挙動:
- log_tail が natural exit (= displace / peer drop) → **自動再開**
  (backoff 0.3 → 0.45 → 0.675 → ... → max 2s)
- `stop` event が立った時のみ完全停止 (= 明示的 unsubscribe / WS close)
- supervisor 完全停止時に dict エントリも掃除

これで「Deploy で displace → 自動再 subscribe → fade override log が再び流れる」
が成立。

## Version bump

- `pyproject.toml`: 0.2.2 → 0.2.3
- `src/hapbeat_helper/__init__.py`: 同上

## 確認手順

```bash
cd hapbeat-helper
pip install -e .
# helper 再起動
```

期待ログ (Studio LogDrawer):
```
log tail (192.168.0.147) auto-restart    ← Deploy 後の自動復帰
[Button] hold_fire_ms set to 1000
[Button] hold_feedback_start_ms set to 300
[Button] hold_feedback_color set to (255, 0, 0)
[Button] hold_feedback_brightness set to 255
[LED] fade override start: rgb=(255,0,0) br=255 progress=0.07 -> out=(237,0,0)
```

## 関連変更

- firmware `applied-tcp-displace-log-stream-202605082359.md` (displacement で
  log_stream を bump 可能にした、bump 時 s_log_stream リセット)
- helper `applied-stop-log-threads-on-ws-close-202605082359.md` (WS 切断時の
  cleanup) — 0.2.2 で入った別系統の修正、本指示書の supervisor ロジックと
  両立する

## 副次的な恩恵

- helper / Studio の WS が瞬断した場合も log_tail が自動再開する → UX 改善
- firmware が一瞬詰まった (kit transfer 中など) 場合も backoff 後に復活
