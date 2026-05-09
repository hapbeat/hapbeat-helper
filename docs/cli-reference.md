# CLI リファレンス

`hapbeat-helper` コマンドの一覧と用例。バージョン 0.2.7 時点。

すべてのコマンドは `hapbeat-helper <subcommand> [options]` の形式。
オプションなしで実行すると help が表示される。

## クイックリファレンス

| コマンド | 用途 |
|---|---|
| [`start`](#start) | フォアグラウンド起動（ログがそのまま流れる） |
| [`stop`](#stop) | 自動起動された helper を停止 |
| [`status`](#status) | 7703 ポートで応答するか確認 |
| [`version`](#version) | バージョン表示 |
| [`logs`](#logs) | 自動起動 helper のログファイルを表示・追跡 |
| [`install-service`](#install-service) | OS のログイン時自動起動に登録 |
| [`uninstall-service`](#uninstall-service) | 自動起動を解除 |
| [`service-status`](#service-status) | 自動起動の登録状態を確認 |
| [`config show`](#config-show) | 設定ファイルのパス・内容を表示 |

`--verbose` / `-v` はトップレベル・`start` 両方で使える（DEBUG ログ）。

---

## コマンド詳細

### `start`

helper をフォアグラウンドで起動。ログはそのターミナルに直接流れる。`Ctrl+C` で停止。

```powershell
hapbeat-helper start
hapbeat-helper start --port 7703    # ポート指定（既定 7703）
hapbeat-helper start --verbose      # DEBUG ログ
```

出力例:
```
hapbeat-helper 0.2.7 starting on ws://localhost:7703
Press Ctrl+C to stop.
20:48:09 INFO hapbeat_helper.udp_listener: UDP listener started on 0.0.0.0:7700
20:48:09 INFO hapbeat_helper.mdns_scanner: mDNS browsing started for _hapbeat._udp.local.
20:48:09 INFO websockets.server: server listening on 127.0.0.1:7703
```

> **注意**: 自動起動された background helper が同じポートで動いている場合は先に `stop` してから起動してください（衝突します）。

---

### `stop`

OS の自動起動で立ち上がっている background helper を停止。Foreground (`start` で立ち上げたもの) には効きません — そちらは元のターミナルで `Ctrl+C` してください。

```powershell
hapbeat-helper stop
```

---

### `status`

`localhost:7703` に TCP 接続できるかだけを確認するチープな疎通テスト。

```powershell
hapbeat-helper status
hapbeat-helper status --port 7703
```

出力例:
```
hapbeat-helper: reachable on ws://localhost:7703
```
あるいは
```
hapbeat-helper: not running (no listener on 7703)
```

---

### `version`

```powershell
hapbeat-helper version
# → hapbeat-helper 0.2.7
```

---

### `logs`

**自動起動** された helper の stdout/stderr が書き出される log file を表示。Foreground の `start` セッションには無関係（あちらは画面に直接流れる）。

```powershell
hapbeat-helper logs              # 末尾 50 行
hapbeat-helper logs -n 200       # 末尾 200 行
hapbeat-helper logs -f           # 末尾を follow（Ctrl+C で停止）
hapbeat-helper logs -n 200 -f    # 末尾 200 行を出してから follow
```

ログファイルの場所:

| OS | パス |
|---|---|
| Windows | `%LOCALAPPDATA%\hapbeat-helper\hapbeat-helper.log` |
| macOS | `~/Library/Logs/hapbeat-helper.log` |

---

### `install-service`

OS のログイン時自動起動に登録。

| OS | 仕組み |
|---|---|
| Windows | スタートアップフォルダに `hapbeat-helper.vbs` shim を配置（Hidden window で `cmd /c hapbeat-helper.exe start` を起動） |
| macOS | `~/Library/LaunchAgents/com.hapbeat.helper.plist`（launchd） |

```powershell
hapbeat-helper install-service
```

> ⚠️ `pipx uninstall hapbeat-helper` する **前に必ず** `uninstall-service` を実行してください。エントリだけ残ると次回ログイン時にコマンド未検出エラーが出ます。

---

### `uninstall-service`

自動起動の登録を解除。helper のバイナリ自体は消しません（pipx で別途 uninstall）。

```powershell
hapbeat-helper uninstall-service
```

---

### `service-status`

自動起動の登録状態を確認。

```powershell
hapbeat-helper service-status
```

出力例（戻り値も状態に対応）:
| 表示 | 戻り値 | 意味 |
|---|---|---|
| `not registered` | 1 | install-service していない |
| `registered, stopped` | 1 | 登録済みだが現在は止まっている |
| `registered, running` | 0 | 登録済みかつ動作中 |

---

### `config show`

設定ファイルの場所と内容を表示。

```powershell
hapbeat-helper config show
```

設定ディレクトリ:
| OS | パス |
|---|---|
| Windows | `%APPDATA%\hapbeat-helper\config.toml` |
| macOS | `~/.config/hapbeat-helper/config.toml` |

---

## よく使う組み合わせ

### A. 初回セットアップ（自動起動 + 確認）

```powershell
pipx install hapbeat-helper
hapbeat-helper install-service     # ログイン時起動を有効化
hapbeat-helper service-status      # → registered, running
hapbeat-helper status              # → reachable
```

### B. アップデート

```powershell
hapbeat-helper stop                # 走っていれば止める
pipx upgrade hapbeat-helper        # または: pipx install --force hapbeat-helper
hapbeat-helper version             # 上がっているか確認
hapbeat-helper start               # foreground で起動 or 自動起動を待つ
```

### C. OTA / 通信トラブル時のデバッグ

```powershell
# 1. 自動起動を一旦止めて衝突を避ける
hapbeat-helper stop
tasklist | findstr python          # 残骸があれば taskkill /F /PID <番号>

# 2. foreground で起動 → ログがそのまま流れる
hapbeat-helper start

# 3. 別ターミナルや Studio から OTA を実行 → ログを観察
#    OTA streaming 中は 5% ごとに以下が出る:
#      INFO OTA <ip>: sent=N% device=M% (X/Y bytes)
#    詰まれば 8s 以内に
#      ERROR ... phase=stall: chunk 送信開始から 8s 経つが device は 0% のまま
```

### D. 自動起動 helper のログだけ追いたい

```powershell
hapbeat-helper logs -f
```

### E. 完全アンインストール

```powershell
hapbeat-helper uninstall-service   # 必ず先に
pipx uninstall hapbeat-helper
```

---

## トラブルシュート

| 症状 | 確認 |
|---|---|
| `start` が起動直後に終わる | `status` で 7703 が他プロセスに使われていないか / 自動起動 helper がすでに動いていないか |
| `status` が unreachable | `service-status` で running か / Windows なら `netstat -ano \| findstr :7703` で listening を確認 |
| `logs` が "log file does not exist" | 自動起動が起動していない（`service-status` 確認）/ Windows でフォアグラウンド `start` した後の出力は `logs` には残らない（標準出力だけ） |
| Studio に「Helper 接続中」が出ない | helper が動いていない or ポート 7703 を Firewall がブロック |

詳細は [Getting Started](./getting-started.md) も参照。
