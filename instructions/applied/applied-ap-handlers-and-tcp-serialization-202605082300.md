# applied: AP-mode WS handlers + per-device TCP serialization

**起点 repo:** hapbeat-sdk-workspace (workspace session)
**作成日:** 2026-05-08
**workspace セッションで直接編集** (横断小規模変更ルールに該当)

---

## 背景

ユーザー報告 (2026-05-08): hapbeat 起動 → helper 起動 → ブラウザに `ERROR: unknown type: get_ap_status` が出て OTA を押しても進まなくなる。再現性あり、何度修正してもこの種の問題が起きる。

## 原因 (2 段)

### 1. AP-mode 5 ハンドラが helper にない

Studio (`DeviceDetail.tsx:100`) はデバイス選択直後に `get_ap_status` を必ず送信する。helper は対応する `elif msg_type == "get_ap_status"` を持たないため `unknown type` エラーで返す → HelperToastBridge がトーストとして surface する。

同様に未対応だった: `enter_ap_mode` / `enter_sta_mode` / `set_ap_pass` / `clear_ap_pass`。
firmware (`tcp_server.cpp`) には全部実装済みなのに helper が relay していなかった。

### 2. firmware の TCP は単一スロット、helper は parallel TCP を投げる

`hapbeat-device-firmware/src/tcp_server.cpp` は `static WiFiClient s_client;` で **1 接続のみ** 受け付ける。新しい接続が来ると古い接続は idle-timeout displaced される。

helper 側は `loop.run_in_executor(None, ...)` で並列 TCP を許してしまっていた。
Studio はデバイス選択時に 5 件のクエリを連射する:
- `list_wifi_profiles`, `get_info`, `get_wifi_status`, `get_ap_status`, `get_oled_brightness`

これらが thread pool で並列発火 → firmware は 1 つしか受けられず displacement 連鎖 →
そのタイミングで OTA を始めると:
- OTA の TCP も displaced される / 直前の query が drain せずに OTA bytes が混ざる
- ユーザーには「OTA 押したのに progress が動かない」状態に見える

## 適用した修正

### A. AP-mode 5 ハンドラ追加 (server.py)

`clear_wifi` の直後に追加:

- `get_ap_status` → `_handle_query` で `ap_status_result` を返す
- `enter_ap_mode` / `enter_sta_mode` → `_handle_tcp_command`
- `set_ap_pass` → `_handle_tcp_command` (`password` / `pass` 両キー受容)
- `clear_ap_pass` → `_handle_tcp_command`

### B. 自前 `asyncio.Lock` で per-IP TCP serialization

`__init__` に `self._tcp_locks: dict[str, asyncio.Lock]` を追加し、新ヘルパ `_get_tcp_lock(ip)` で lazy 作成。

ロックを取得する箇所:
- `_handle_tcp_command` (write_ui_config, set_wifi, etc)
- `_handle_query` (get_info, get_wifi_status, get_oled_brightness, etc)
- `_handle_passthrough_query` (kit_list, debug_dump)
- `_handle_volume_query`
- `_handle_ota_data` (= 長時間ロック保持)
- `_handle_deploy_kit_data` (per-target inside the loop)

別デバイス間は並列維持、同一デバイス宛は順序保証される。

### C. version bump 0.2.0 → 0.2.1

- `pyproject.toml`
- `src/hapbeat_helper/__init__.py`

## 検証 / TODO

- [x] `python -c "import ast; ast.parse(...)"` 構文 OK
- [ ] 実機で hapbeat → helper の順に起動して `unknown type: get_ap_status` トーストが消えるか確認
- [ ] 連続デバイス選択 → OTA 即押下 で 5 連続成功するか確認
- [ ] kit deploy + 別 query 並走で displacement なしを確認

## 残作業 (本指示書の範疇外)

`subscribe_logs` の `_log_tail_worker` は別 thread で長期 TCP を保持しており、
`_get_tcp_lock` の管理外。log stream ON 中に OTA するシナリオは引き続き
displacement が起きうる。次セッションで `_log_tail_worker` も同じロック下に
入れるか、log stream 中は OTA を block する UI ガードを Studio に入れる。

## ユーザー向けアクション

`pipx upgrade hapbeat-helper` (or local editable: `pip install -e .`) を実行して
0.2.1 を取り込めば、上記の get_ap_status エラーは消える + OTA の安定性が改善する想定。

---

## レビュー観点 (helper repo セッションでの確認用)

- [ ] AP ハンドラ 5 件が `_handle_query` / `_handle_tcp_command` の正しい引数で呼ばれているか
- [ ] `_get_tcp_lock` の dict が無制限に増えないか (デバイスは数台しかないので OK)
- [ ] `async with self._get_tcp_lock(ip):` が `loop.run_in_executor` を **囲って** いるか (= ロック取った状態で executor に投げて完了まで保持)
- [ ] 既存の write_progress 通知が lock 内/外どちらで送られるか (現状: lock 外で OK、進捗だけは並列で更新できる)
