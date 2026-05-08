# Instructions: OTA `sendall` タイムアウト延長 + チャンク送信間 inter-packet pacing

**発行日:** 2026-05-09
**起票:** workspace session 2026-05-09 (device-firmware ↔ helper)
**優先度:** 中 (device-firmware v0.0.29 で OTA は通るようになったが、helper の `READ_TIMEOUT=1.5` は依然として OTA 送信で詰まりやすい)
**対象 helper:** 0.2.x 以降

---

## 背景

ユーザー報告 (2026-05-09):
> ota update… のまま進みません。以前は progress のパーセント表示があった。毎回以下のところで止まる。
> 27% / 28% / 28% — 送信中 503,808/1,755,952
> ✗ phase=io: TimeoutError: timed out

device-firmware 側 (v0.0.29) で:
- `processOtaData` の TCP drain rate を 1 KB → 4 KB/loop に拡大
- OTA 中は main loop の他の重処理 (audio / wifi / udp / ESP-NOW pump) を skip して tight loop 化

これで OTA は通るようになった。しかし helper 側の制約が残っている。

## 残課題

`hapbeat-helper/src/hapbeat_helper/tcp_client.py` の `READ_TIMEOUT = 1.5` を OTA 経路でも `socket.sendall()` のブロック上限として使っている (Python の socket は send/recv に同じ timeout を共有)。

ESP32-S3 のフラッシュは:
- 4 KB セクター erase が typ 50 ms / max 400 ms
- 連続して未書込みセクター (新規 OTA slot) を書き込む際、初回 erase の rush でデバイス側 TCP 受信が一時的に詰まる
- 1.5 秒の send timeout は flash erase 列が固まると越える可能性がある

device 側の改善 (4 KB drain + tight loop) でかなり余裕が出たが、`READ_TIMEOUT=1.5` は通常応答 (set_name など 100 B 程度) に最適化された値で、**OTA の 1.7 MB ストリーム送信には短すぎる**。安全マージンを広げたい。

## 修正方針

### A. `TcpRawConnection` に **OTA 用の長い socket timeout** を一時設定する

`server.py` の `_ota_blocking()` (line 1287-1346) で、`with TcpRawConnection(ip) as conn:` の中で OTA 開始直前に socket timeout を一時的に長く (例: 10 秒) 設定し、完了後に戻す。

#### 推奨実装 (server.py)

```python
async def _ota_blocking(...):
    file_size = len(bin_bytes)
    with TcpRawConnection(ip) as conn:
        ...
        try:
            connected = conn.connect()
        except OSError as exc:
            ...
        if not connected:
            ...
        try:
            conn.send_json({"cmd": "ota_begin", "size": file_size})
            resp = conn.read_response(timeout=5.0)
            if not resp or resp.get("status") != "ok":
                return False, f"phase=ota_begin: nack {resp}"

            # ★ ここから OTA: send/recv timeout を長めに設定
            #   1.5s デフォルトはフラッシュ erase の rush を吸収できないので
            #   10s まで許容する。helper 全体でフリーズ感を出さないため
            #   完了後 (finally) に元に戻す。
            prev_timeout = conn.sock.gettimeout()
            conn.sock.settimeout(10.0)
            try:
                chunk_size = 4096
                sent = 0
                for off in range(0, file_size, chunk_size):
                    chunk = bin_bytes[off : off + chunk_size]
                    conn.send_raw(chunk)
                    sent += len(chunk)
                    pct = int(sent / file_size * 95) + 1
                    progress("upload", pct, f"送信中 {sent:,}/{file_size:,}")
                    # 任意: 数十チャンクごとに小さい sleep を入れて
                    # device 側の flash erase に追いつく時間を与える
                    # (実機テストで不要なら削る)
                    # if (off // chunk_size) % 64 == 0:
                    #     await asyncio.sleep(0.01)

                progress("flash", 96, "デバイス書込待ち…")
                while True:
                    resp = conn.read_response(timeout=30.0)
                    ...
            finally:
                # OTA が成功でも失敗でも socket timeout を戻す
                try:
                    conn.sock.settimeout(prev_timeout)
                except Exception:
                    pass
        except OSError as exc:
            return False, f"phase=io: {type(exc).__name__}: {exc}"
```

### B. もう少し綺麗な代替: `TcpRawConnection.send_raw` に optional timeout 引数

`tcp_client.py` の `send_raw` を:

```python
def send_raw(self, data: bytes, *, timeout: float | None = None) -> None:
    if not self.sock:
        raise RuntimeError("not connected")
    if timeout is not None:
        prev = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            self.sock.sendall(data)
        finally:
            self.sock.settimeout(prev)
    else:
        self.sock.sendall(data)
```

server.py 側:
```python
conn.send_raw(chunk, timeout=10.0)
```

シンプルでスコープが OTA 送信のみに閉じる利点。チャンクごとに settimeout のオーバーヘッドが少しあるが負荷は微小。

**A と B のどちらを採用するかは設計の好み次第。B のほうが API 的に自然。**

## 検証手順

1. helper を `pip install -e .` で更新 → 再起動
2. Studio から OTA 実行 (1.7 MB のファーム)
3. **期待:** 0% → 100% まで進捗を出して完了。途中の 28%, 50%, 75% などで stall しない
4. **期待:** デバイス flash 書込フェーズ (96-99% 表示) で 30 秒以内に reboot 完了

## 副作用評価

- 通常応答 (set_name / set_group / get_info など短い JSON) は影響なし。tcp_client の `READ_TIMEOUT=1.5` をそのまま使う
- OTA 中の helper の応答性: 10 秒 timeout は worst case のみ。通常は数十 ms で完了するため UI は普通
- WS broadcast (`ota_progress`) は別タスクで動くので止まらない

## 関連

- device 側修正: `hapbeat-device-firmware` v0.0.29 の `processOtaData` 4KB drain + main loop の OTA tight loop 化
- 関連ファイル:
  - `src/hapbeat_helper/server.py` (OTA worker)
  - `src/hapbeat_helper/tcp_client.py` (TcpRawConnection)
- 関連ユーザー報告: 2026-05-09 「OTA が 28% で止まる」「TimeoutError: timed out」
