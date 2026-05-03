---
title: hapbeat-helper のインストール
description: Hapbeat Studio とデバイスを橋渡しする CLI daemon `hapbeat-helper` の OS 別インストール手順。macOS / Windows / Linux 共通。
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

## 必要環境

- **Python 3.10 以上**（`pipx` 経由で別 venv に入るため、システム Python のバージョンに気を遣う必要はありません）
- **Hapbeat デバイスと同じ Wi-Fi LAN にぶら下がっている PC**（Windows / macOS / Linux）
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

### Linux

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
# 新しいシェルを開く
pipx install hapbeat-helper
```

## 起動

### ログイン時自動起動（推奨）

1 回だけコマンドを実行すると、以降はログインするたびに Helper が自動起動します。Studio を開けばすぐ接続済みの状態になります。

```bash
hapbeat-helper install-service
```

| OS | 仕組み |
|---|---|
| macOS | `~/Library/LaunchAgents/com.hapbeat.helper.plist`（launchd） |
| Linux | `~/.config/systemd/user/hapbeat-helper.service`（systemd --user） |
| Windows | タスク スケジューラ エントリ `HapbeatHelper`（ログイン時起動） |

登録状態を確認するには:

```bash
hapbeat-helper service-status
```

自動起動を解除するには:

```bash
hapbeat-helper uninstall-service
```

### フォアグラウンド起動（開発・デバッグ用）

別ターミナルで手動起動することもできます。

```bash
hapbeat-helper start --foreground
```

正常起動すると以下のようなログが出ます:

```
WebSocket server listening on ws://127.0.0.1:7703
mDNS browser started for _hapbeat._udp.local.
```

このターミナルは開きっぱなしにしておきます。Studio を使い終わったら `Ctrl+C` で止めます。

## 動作確認

ブラウザで <https://devtools.hapbeat.com/studio/> を開きます。Studio 上部の Helper 接続ステータスが **緑「Helper 接続中」** になれば接続成功です。

コマンドライン側の単体確認は以下のいずれかで行えます:

```bash
hapbeat-helper status     # daemon が 7703 で応答するか
hapbeat-helper version    # 入っているバージョン
```

## アップデート

```bash
pipx upgrade hapbeat-helper
```

> Studio の log drawer に `ERROR: unknown type: <message>` が出る場合、Helper が古い Studio との不整合です。`pipx upgrade hapbeat-helper` してから Helper を再起動してください。

## アンインストール

```bash
pipx uninstall hapbeat-helper
```

## トラブルシューティング

| 症状 | 対処 |
|---|---|
| Studio に「Helper 接続中」が出ない | `hapbeat-helper install-service` で自動起動を設定するか、ターミナルで `hapbeat-helper start --foreground` を実行 / ポート 7703 が他プロセスに使われていないか (`lsof -i :7703` / Windows は `netstat -ano \| findstr :7703`) |
| ブラウザから `ws://localhost:7703` に繋がらない (Firefox) | `about:config` → `network.websocket.allowInsecureFromHTTPS` を `true` に。Chrome / Edge は不要 |
| デバイスがサイドバーに出てこない | Helper と Hapbeat が同一 Wi-Fi LAN か確認 / hotspot/AP モードによっては UDP broadcast / mDNS が遮断される |
| ポート 7700 / 7703 が既に使われている | 旧 `hapbeat-manager` を起動していないか確認。Helper と Manager は同時起動できません |
| **macOS 14 (Sonoma) 以上で Wi-Fi scan が空** | `airport -s` が deprecated 化されたため、Helper の SSID 自動取得が動かないことがあります。Studio の Wi-Fi 設定で SSID を**手入力**で追加してください（パスワードは正常に設定できます） |
| Mac で USB Serial 書き込みが動かない | デバイス名が `/dev/cu.usbmodem*` 系で出ているか確認 (`ls /dev/cu.*`)。出ない場合はデータ通信対応の USB-C ケーブルか確認 (充電専用ケーブルは不可) |

## 次のステップ

- [Hapbeat 初期セットアップ](/docs/studio/initial-setup/) — Hapbeat デバイスを最初に Wi-Fi に乗せる手順（Studio のオンボーディング ウィザード）
- [最初の Kit を作る](/docs/studio/getting-started/) — Studio で振動コンテンツをデザインする
