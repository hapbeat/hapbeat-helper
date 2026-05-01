# Applied: write_result の詳細化 (per-target diagnostics)

**起点 repo**: `hapbeat-studio` (オンボーディング ウィザード改善セッション)
**日付**: 2026-04-30
**関連 DEC**: なし (ローカルフィックス)

## 横断的な背景

Studio 側でユーザーから「Wi-Fi 設定後に他コマンドが通らない時、ログが
`✗ 0/1 ok` だけで原因が分からない」報告。Studio 側で詳細ログを表示
できるようにするため、helper が WS で返す `write_result` ペイロードを
拡張する必要があった。

## このリポジトリに入った変更

### `src/hapbeat_helper/server.py`

#### 1. `_handle_tcp_command` の payload を拡張

旧:
```json
{
  "type": "write_result",
  "payload": {"success": ..., "device_confirmed": true, "message": "0/1 ok", "results": [...]}
}
```

新:
```json
{
  "type": "write_result",
  "payload": {
    "success": ...,
    "device_confirmed": true,
    "message": "set_name: 0/1 ok\n  ✗ 192.168.0.233: TCP 7701 connect failed (4321 ms). 電源を一度 OFF→ON してください。",
    "summary": "set_name: 0/1 ok",
    "cmd": "set_name",
    "results": [...]
  }
}
```

- `summary`: 1 行サマリー (Studio の floating pill 用)
- `message`: フル multi-line (Studio の log drawer 用)
- `cmd`: コマンド名を明示
- `results`: 既存 (per-target raw)

#### 2. `_send_tcp_to_many` のエラーパスを 3 種に分類

各失敗結果の `response` に `phase` フィールドを追加:
- `"connect"`: TCP 7701 への接続失敗 (timeout / refused / unreachable)
- `"io"`: 接続後の send/read で OSError
- `"no_reply"`: 接続成功したが空レスポンス

具体的な例外名と所要 ms を文字列化:
```json
{
  "error": "TCP 7701 connect failed to 192.168.0.233 (4321 ms). デバイスがオンラインに見えても TCP サーバが起動していない場合があります — 電源を一度 OFF→ON してください。",
  "phase": "connect",
  "cmd": "set_name"
}
```

`time` モジュールはすでに import 済み (line 27)。

#### 3. `no_target` ケースのメッセージ強化

Studio で `payload.ip` が空のまま送信された時のヒントを含める:
```
"set_name: 送信先デバイスが解決できません (payload.ip=''). Devices タブでデバイスを選択してから再実行してください。"
```

## 検証状況

- **import smoke test: ok** (`from hapbeat_helper.server import ...`)
- **pytest: 8/8 pass** (起点セッションで `.venv/Scripts/python.exe -m pytest -q` 実行)
- Studio 側ですでに `summary` / `cmd` / `results` を読む UI は実装済み
  (起点 repo 側 `DeviceDetail.tsx` の write_result handler)

## このリポジトリのエージェントへのアクション

1. 簡単な smoke test:
   - Studio から `set_name` 等を叩いた時、helper のログに新しい
     `phase=connect/io/no_reply` 形式が出るか目視
2. 問題なければ本ファイルを `instructions/completed/` に移動

## 関連ファイル

- `src/hapbeat_helper/server.py` (主要変更)
- 起点 Studio 側: `../hapbeat-studio/src/components/devices/DeviceDetail.tsx`
  の write_result handler が `summary` / `cmd` / `results.phase` を消費
