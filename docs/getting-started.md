---
title: hapbeat-helper のインストール
description: Hapbeat Studio とデバイスを橋渡しする CLI daemon `hapbeat-helper` の OS 別インストール手順。macOS / Windows 共通。
---

`hapbeat-helper` は **Hapbeat Studio (Web)** と **Hapbeat デバイス (Wi-Fi LAN)** を橋渡しするローカル daemon です。ブラウザ単体では行えない mDNS 検出・UDP broadcast・raw TCP を中継します。

```
ブラウザ (https://devtools.hapbeat.com/studio/)
        │  ws://localhost:7703 (JSON)
        ▼
hapbeat-helper                  ← この CLI
        │  UDP 7700 (PLAY / STOP / PING / streaming)
        │  TCP 7701 (config / kit deploy)
        │  mDNS (_hapbeat._udp.local.)
        ▼
   Hapbeat デバイス (同一 LAN)
```

> Studio 側で「Helper 接続中」（緑バッジ）が出ない場合は、Helper が起動していないかポート 7703 が塞がっています。

## クイックスタート（3 分で完了）

Studio を開いて「Helper 未接続」と表示されている場合は、以下の 3 ステップで解決します。

```bash
# 1. pipx をまだ入れていなければ（1回だけ）
#    macOS:   brew install pipx && pipx ensurepath
#    Windows: py -m pip install --user pipx && py -m pipx ensurepath
#    ↑ その後、新しいターミナルを開く

# 2. helper をインストール
pipx install hapbeat-helper

# 3. ログイン時自動起動を設定（実行直後から起動します）
hapbeat-helper install-service
```

以上で完了です。Studio をリロードすると「Helper 接続中」（緑バッジ）に変わります。

---

## 必要環境

- **Python 3.10 以上**（`pipx` 経由で別 venv に入るため、システム Python のバージョンに気を遣う必要はありません）
- **Hapbeat デバイスと同じ Wi-Fi LAN にぶら下がっている PC**（Windows / macOS）
- **Chrome または Edge**（Studio が Web Serial / File System Access を使うため）

## インストール

`pipx` 経由でインストールします。`pipx` は Python CLI を独立した venv に隔離する標準ツールです。

### macOS

```bash
# 1. Homebrew が無ければ:
#    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# 2. pipx 導入
brew install pipx
pipx ensurepath

# 3. 新しいターミナルを開く（PATH 反映のため）

# 4. helper 本体
pipx install hapbeat-helper

# 5. 動作確認
hapbeat-helper version
```

> 起動時に macOS Firewall ダイアログが出たら **「許可」** を選んでください（Helper が UDP/TCP/mDNS を listen するため必要）。

### Windows

```powershell
# 1. pipx 導入
py -m pip install --user pipx
py -m pipx ensurepath

# 2. 新しいターミナルを開く（PATH 反映のため）
pipx --version

# 3. helper 本体
pipx install hapbeat-helper
```

> **`pipx` not recognized** と言われる場合は、ターミナル再起動後も認識されないことがあります。`py -m pipx install hapbeat-helper` でも同等に動作します。
>
> **OneDrive 同期下のホームディレクトリ** (`C:\Users\<you>\` が OneDrive で同期されている) では `pipx install` が `WinError 448 untrusted mount point` で失敗することがあります。その場合は `pipx` の保存先を OneDrive 外に移してください:
>
> ```powershell
> [Environment]::SetEnvironmentVariable('PIPX_HOME',    'C:\pipx\home', 'User')
> [Environment]::SetEnvironmentVariable('PIPX_BIN_DIR', 'C:\pipx\bin',  'User')
> # ターミナル再起動後:
> py -m pipx ensurepath
> py -m pipx install hapbeat-helper
> ```
>
> 起動時に **Windows Defender Firewall** ダイアログが出たら「アクセスを許可する」を選んでください。

## 起動

### ログイン時自動起動（推奨）

1 回だけコマンドを実行すると、**実行直後から** Helper が起動し、以降はログインするたびに自動起動します。Studio を開けばすぐ接続済みの状態になります。

```bash
hapbeat-helper install-service
```

実行後すぐ以下のような出力が出ます（再ログイン不要）:

```
hapbeat-helper auto-start installed.
  shim: C:\Users\<you>\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup\HapbeatHelper.vbs
  exe:  C:\pipx\bin\hapbeat-helper.exe
  log:  C:\Users\<you>\AppData\Local\hapbeat-helper\hapbeat-helper.log
  next logon: Helper starts automatically (hidden).
```

| OS | 仕組み |
|---|---|
| macOS | `~/Library/LaunchAgents/com.hapbeat.helper.plist`（launchd、`KeepAlive=true`） |
| Windows | **Task Scheduler**（`HapbeatHelper` タスク）に `powershell.exe -WindowStyle Hidden` アクションを登録。VBScript を使わないため Windows 11 24H2+ でも動作。stdout/stderr → `%LOCALAPPDATA%\hapbeat-helper\hapbeat-helper.log` |

登録状態を確認するには:

```bash
hapbeat-helper service-status
```

自動起動を解除するには:

```bash
hapbeat-helper uninstall-service
```

> ⚠️ `pipx uninstall hapbeat-helper` を実行する **前に** 必ず `hapbeat-helper uninstall-service` を先に実行してください。エントリだけ残った状態で実体を消すと、次回ログイン時に「コマンドが見つからない」エラーが出ることがあります。

> 自動起動の挙動・PC 作業への影響・セキュリティリスクの詳細は **[セキュリティと動作の影響](./security.md)** にまとめています。公共 Wi-Fi で使う前に必ず確認してください。

### フォアグラウンド起動（開発・デバッグ用）

別ターミナルで手動起動することもできます。

```bash
hapbeat-helper start
```

正常起動すると以下のようなログが出ます:

```
hapbeat-helper 0.1.1 starting on ws://localhost:7703
Press Ctrl+C to stop.
20:48:09 INFO hapbeat_helper.udp_listener: UDP listener started on 0.0.0.0:7700
20:48:09 INFO hapbeat_helper.mdns_scanner: mDNS browsing started for _hapbeat._udp.local.
20:48:09 INFO websockets.server: server listening on 127.0.0.1:7703
```

このターミナルは開きっぱなしにしておきます。Studio を使い終わったら `Ctrl+C` で止めます。

> **自動起動 helper がすでに動いている場合** はポートが競合して起動に失敗します。先に `hapbeat-helper stop` を実行してから `start` してください。

## 動作確認

ブラウザで <https://devtools.hapbeat.com/studio/> を開きます。Studio 上部の Helper 接続ステータスが **緑「Helper 接続中」** になれば接続成功です。

コマンドライン側の単体確認は以下のいずれかで行えます:

```bash
hapbeat-helper status     # daemon が 7703 で応答するか
hapbeat-helper version    # 入っているバージョン
```

## アップデート

自動起動中の helper を更新する手順:

```bash
# 1. 現在の helper を止める
hapbeat-helper stop

# 2. 新バージョンに更新
pipx upgrade hapbeat-helper

# 3. バージョンを確認
hapbeat-helper version

# 4-a. 自動起動を使っている場合: install-service で即起動
hapbeat-helper install-service

# 4-b. フォアグラウンドで確認したい場合
hapbeat-helper start
```

> **macOS の注意**: `hapbeat-helper stop` は `KeepAlive=true` のため SIGTERM 送信後に launchd が即 respawn する（実質「再起動」）。アップデート前に完全停止したい場合は `hapbeat-helper uninstall-service` → upgrade → `hapbeat-helper install-service` の順で行います。

> Studio の log drawer に `ERROR: unknown type: <message>` が出る場合、Helper が古い Studio との不整合です。上記の手順でアップデートしてください。

## ログの確認

自動起動 helper のログはファイルに書き出されます。

```bash
hapbeat-helper logs          # 末尾 50 行を表示
hapbeat-helper logs -n 200   # 末尾 200 行
hapbeat-helper logs -f       # リアルタイム追跡（Ctrl+C で停止）
```

ログファイルの場所:

| OS | パス |
|---|---|
| Windows | `%LOCALAPPDATA%\hapbeat-helper\hapbeat-helper.log` |
| macOS | `~/Library/Logs/hapbeat-helper.log` |

> フォアグラウンド起動（`hapbeat-helper start`）のログはターミナルに直接流れるため、`logs` コマンドには残りません。

## アンインストール

```bash
hapbeat-helper uninstall-service   # 必ず先に（自動起動を解除）
pipx uninstall hapbeat-helper
```

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| Studio に「Helper 接続中」が出ない | `hapbeat-helper install-service` で自動起動を設定するか、ターミナルで `hapbeat-helper start` を実行 / ポート 7703 が他プロセスに使われていないか (`lsof -i :7703` / Windows は `netstat -ano \| findstr :7703`) |
| ブラウザから `ws://localhost:7703` に繋がらない (Firefox) | `about:config` → `network.websocket.allowInsecureFromHTTPS` を `true` に。Chrome / Edge は不要 |
| デバイスがサイドバーに出てこない | Helper と Hapbeat が同一 Wi-Fi LAN か確認 / hotspot/AP モードによっては UDP broadcast / mDNS が遮断される |
| ポート 7700 または 7703 が既に使われている | Helper が二重起動していないか確認。`hapbeat-helper service-status` で動作中なら `hapbeat-helper stop` してから再起動。それ以外のプロセスが占有している場合は macOS: `lsof -i :7703` / Windows: `netstat -ano \| findstr :7703` で PID を特定し、そのアプリを終了してください |
| **Windows: ログイン後に Helper が自動起動しない** | Windows 11 24H2+ では VBScript がデフォルト無効のため、古い VBS shim はスタートアップフォルダに存在しても実行されません。`hapbeat-helper uninstall-service` → `hapbeat-helper install-service` を実行し直すと Task Scheduler 方式に切り替わります |
| **macOS 14 (Sonoma) 以上で Wi-Fi scan が空** | `airport -s` が deprecated 化されたため、Helper の SSID 自動取得が動かないことがあります。Studio の Wi-Fi 設定で SSID を**手入力**で追加してください（パスワードは正常に設定できます） |
| Mac で USB Serial 書き込みが動かない | デバイス名が `/dev/cu.usbmodem*` 系で出ているか確認 (`ls /dev/cu.*`)。出ない場合はデータ通信対応の USB-C ケーブルか確認 (充電専用ケーブルは不可) |
| macOS で `Ctrl+C` が効かない | `install-service` で登録した Helper はバックグラウンドで動いており、ターミナルにフォアグラウンドプロセスが存在しません。`hapbeat-helper stop` で停止してください |
| macOS で `hapbeat-helper stop` してもすぐ再起動してしまった（旧バージョン） | 旧バージョンは `kickstart -k`（SIGTERM→即 respawn）を使っていました。最新版は `bootout` で正しく停止します。`pipx upgrade hapbeat-helper` でアップデートしてください |

## 次のステップ

- [CLI リファレンス](./cli-reference.md) — 全サブコマンド（`start` / `stop` / `logs` / `status` / `service-status` ほか）一覧と用例
- [Hapbeat 初期セットアップ](/docs/studio/initial-setup/) — Hapbeat デバイスを最初に Wi-Fi に乗せる手順（Studio のオンボーディング ウィザード）
- [最初の Kit を作る](/docs/studio/getting-started/) — Studio で振動コンテンツをデザインする
