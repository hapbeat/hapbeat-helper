# applied: WS close 時に log_tail_worker thread を全停止 (v0.2.2)

**起点 repo:** hapbeat-sdk-workspace (workspace session)
**作成日:** 2026-05-08
**関連:** `hapbeat-device-firmware/instructions/applied/applied-tcp-displace-log-stream-202605082359.md` (firmware 側のセット修正)

---

## 背景

ユーザー報告 (2026-05-08): しばらく Studio を放置すると `TCP handshake failed: None` が連発し、power cycle 以外で復帰しない。

helper ログ:
```
TCP handshake failed (192.168.0.147): None
TCP recovery: waiting 6.0s for firmware idle-timeout to release stuck client
TCP recovery handshake also failed (192.168.0.147): None
```

## 原因

`server.py:_handler` の `finally` ブロックは以下しかしない:

```python
finally:
    self._clients.discard(ws)
```

`self._log_threads` (= `_log_tail_worker` 背景スレッド) は止めない。
結果:

1. ユーザーが Studio LogDrawer を開く → helper の `subscribe_logs` ハンドラが
   `_log_tail_worker` thread を起動。device に TCP 接続を張り `log_stream` cmd
   を送信。firmware は `s_log_stream = true` にラッチ
2. ユーザーが Studio タブ閉じ / リロード / ネットワーク瞬断 → WS が切断される
3. `_handler` の finally は ws を discard するだけ。`_log_tail_worker` は
   生き続け、TCP 接続も保持
4. firmware から見ると client は `s_log_stream=true` のまま
5. 新規 helper request が SYN を投げると、firmware の displacement gate
   (`!s_log_stream` 条件) で拒否されハンドシェイク失敗
6. 6 秒待っても s_log_stream は false に戻らない → 永久ループ

## 適用した変更

### `_handler` の finally で「最後の WS が消えたら全 log_tail を止める」

```python
finally:
    self._clients.discard(ws)
    if not self._clients and self._log_threads:
        logger.info(
            "last WS client gone — stopping %d log_tail thread(s)",
            len(self._log_threads),
        )
        ips_to_stop = list(self._log_threads.keys())
        for ip in ips_to_stop:
            stop = self._log_stop_flags.pop(ip, None)
            self._log_threads.pop(ip, None)
            if stop:
                stop.set()
```

stop event を set すれば `_log_tail_worker` 内のループが抜けて TCP も
クローズされる (worker 側は `stop.is_set()` を周期チェック済み)。

### Version bump 0.2.1 → 0.2.2

- `pyproject.toml`
- `src/hapbeat_helper/__init__.py`

## 確認 / TODO

- [x] `python -c "import ast; ast.parse(...)"` 構文 OK
- [ ] LogDrawer 開いた状態で Studio タブを閉じる → helper のログに
      `last WS client gone — stopping N log_tail thread(s)` が出るか
- [ ] 上記後すぐ Studio を再起動して deploy → 1 発で通るか

## 残課題 (本指示書の範疇外)

- **複数 Studio タブ対応:** 今は「最後の WS が消えたら止める」と一括処理。
  複数タブ運用ケースで 1 タブだけ閉じた時に他タブの log subscription を
  巻き添えにしないか確認が必要。helper MVP は単一タブ前提なので当面 OK
- **ネットワーク瞬断後の WS 自動再接続:** `websockets` lib は close を
  検出するが、その後 Studio が reconnect した時に subscribe_logs を再送
  しないと log は流れない。Studio の LogDrawer 側に reconnect-on-error
  の subscribe redo を入れるかは別途検討

## ユーザーの取込み手順

```bash
cd C:\GitHub\Hapbeat\hapbeat-sdk-workspace\hapbeat-helper
pip install -e .
# helper 再起動 (Startup folder の VBS shim から、または手動で hapbeat-helper start)
```
