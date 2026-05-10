---
title: セキュリティと動作の影響
description: hapbeat-helper を常駐させた時の PC 作業への影響と、Hapbeat / Helper / Studio が抱えるセキュリティリスクの全体像。公共 Wi-Fi 利用ガイド付き。
---

このページは `hapbeat-helper` を **常駐させた時に PC とネットワークに何が起きるか**、また **Hapbeat と組み合わせた場合のセキュリティ上の注意点** をまとめています。導入手順は [hapbeat-helper のインストール](../getting-started/) を参照してください。

## PC 作業への影響

常駐するとはいえ、Helper は LAN ローカル限定の軽量プロセスです。普段の PC 作業に体感できる影響はほぼありません。

| 観点 | 影響 | 詳細 |
|---|---|---|
| CPU | ほぼゼロ | アイドル時は WebSocket / mDNS / UDP listener が select() で待機。Studio 接続中も触覚パケットの中継のみ |
| メモリ | 30〜60 MB | Python プロセス 1 個分。常駐 Slack/Discord 等よりずっと軽量 |
| ネットワーク | LAN ローカルのみ | UDP 7700 / TCP 7701 / mDNS マルチキャストを **同一 LAN にしか出さない**。インターネット送信なし |
| ポート占有 | 7700 / 7701 / 7703 | 同一ポートを使う他プロセスと競合する。Helper が二重起動していないか `hapbeat-helper service-status` で確認 |
| ファイアウォール | 初回ダイアログのみ | mac は `Firewall`、Windows は `Defender Firewall`。LAN 通信のため許可推奨 |
| 電源・スリープ | 影響なし | スリープ中は Python プロセスもスリープ。復帰時に自動再開 |
| 管理者権限 | 不要 | macOS: ユーザースコープの LaunchAgent / Windows: ユーザースコープのタスクスケジューラータスク |
| ターミナル/コンソール | 表示されない | macOS: launchd が標準出力をログファイルへ。Windows: WScript の `0` (hidden) で起動 |
| クラッシュ時の挙動 | 自動再起動なし | 次回ログオン時に再起動。常時可用性が必要なら手動でフォアグラウンド起動を併用 |

## セキュリティリスク

Helper は **ローカル devtool** として設計されています。Hapbeat と組み合わせた時のリスクをコード根拠つきでまとめます。

### 1) Helper 自身の安全性

| 観点 | 評価 | 補足 |
|---|---|---|
| WebSocket 7703 | **localhost (127.0.0.1) のみ bind** | 外部 PC からは接続不可。Studio (Web) と同 PC のブラウザのみが接続できる。`server.py:42` `HOST = "localhost"` |
| UDP 7700 | LAN 全体に listen | Hapbeat からの応答パケットを受けるため。`udp_listener.py:64` `sock.bind(("0.0.0.0", self._port))`。ただし内容は触覚パケット (PLAY/STOP/PONG) のみで、PC 内ファイルや認証情報は流さない |
| TCP 7701 | クライアント側のみ | Helper 側で listen しない。デバイス側の TCP 7701 へ接続する側 |
| mDNS | LAN 内マルチキャスト | デバイス検出のため `_hapbeat._udp.local.` を broadcast。LAN 内に Hapbeat 名・IP が公開される |
| 書き込み権限 | ユーザーホームのみ | macOS: `~/Library/LaunchAgents/`、Windows: `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\HapbeatHelper.vbs`。OS 保護領域には書き込まない |
| 外部送信 | なし | 起動時の自己診断・テレメトリ・auto-update 機能なし。LAN 外への送信は一切しない |
| コード署名 | 未対応 | `pipx install` で PyPI から取得。サプライチェーン信頼は PyPI と GitHub Releases に依存。気になる場合は `pipx install -e .` でソースからビルド |
| アンインストール時の残骸 | なし | `uninstall-service` で shim/plist 削除、`pipx uninstall` で venv ごと消える |

### 2) Hapbeat 自体のリスク（Helper の有無に関わらず存在）

Helper を起動していなくても、**Hapbeat と同じ Wi-Fi に繋がっているだけで** Hapbeat の TCP 7701 にコマンドを送れます。

| 観点 | 評価 | 補足 |
|---|---|---|
| TCP 7701 認証 | 認証なし | 同一 LAN 内の任意のデバイスが Hapbeat の TCP 7701 にコマンドを送れる |
| Wi-Fi パスワードの保護 | **write-only** | パスワードは書き込みのみ可能で、保存後はどの経路でも device 外に出さない（API キー方式）。`list_wifi_profiles` は SSID と `has_pass` フラグだけ返す |
| 触覚操作 | 操作可能 | 任意の振動パケット (UDP 7700) を送れる。第三者が悪戯目的で振動を発火させることはできる |
| ファームウェア書込 | 物理アクセスが必要 | OTA は TCP 7701 経由だが、**実機は USB Serial 経由でも書き込み可能**。Helper を入れた PC を他人が触ると任意ファームを書き込める |

### 3) 推奨する運用

| 環境 | Helper 常駐 | Hapbeat 接続 |
|---|---|---|
| 自宅・自分の開発機 | ✅ OK | ✅ OK |
| 信頼できるオフィス LAN | ✅ OK | ✅ OK |
| **公共 Wi-Fi（カフェ・ホテル）** | ✅ OK（Helper 経由のデータ漏洩経路なし） | ⚠️ 触覚を勝手に発火させられる悪戯リスクはあるが、Wi-Fi パスワード等は漏れない |
| 共用 PC・kiosk PC・展示会ブース | ⚠️ 物理アクセスで任意ファーム書込が可能。常駐は避ける | ⚠️ Hapbeat 自体を取り外し可能な状態で展示しない |
| モバイルテザリング | ✅ OK | ✅ OK |

具体的な行動ガイド:
- ✅ 普段は `install-service` で常駐させて快適に開発する
- ⚠️ 不特定多数が触れる場所で Hapbeat を放置しない（USB Serial で任意ファームを焼かれる可能性）

## 自動起動の仕組み

| OS | 仕組み | ファイル / エントリ |
|---|---|---|
| macOS | launchd LaunchAgent | `~/Library/LaunchAgents/com.hapbeat.helper.plist` |
| Windows | Task Scheduler（ユーザーのログオンタスク） | タスク名 `HapbeatHelper`。`powershell.exe -WindowStyle Hidden` アクションで実行 |

どちらもユーザースコープなので**管理者権限不要**です。

> **Windows VBScript について**: Windows 11 24H2 以降、VBScript はデフォルトで無効化されています（Microsoft による段階的廃止）。以前のバージョンでは Startup フォルダへの VBS shim を使用していましたが、現バージョンは Task Scheduler + PowerShell に移行済みです。古い VBS shim が残っている場合は `hapbeat-helper uninstall-service` → `hapbeat-helper install-service` で移行できます。

`install-service` は登録と同時に Helper を**即座に起動**します（次回ログインまで待つ必要はありません）。

### 確認・操作コマンド（OS 共通）

```bash
hapbeat-helper service-status    # "registered, running" / "registered, stopped" / "not registered"
hapbeat-helper logs              # ログファイルのパス + 末尾 50 行
hapbeat-helper logs -f           # 末尾を follow（Ctrl+C で抜ける）
hapbeat-helper logs -n 200       # 末尾 200 行
hapbeat-helper stop              # 起動中のインスタンスを kill（macOS は再起動相当、下記参照）
hapbeat-helper install-service   # 自動起動を設定（実行直後から起動）
hapbeat-helper uninstall-service # 自動起動を解除（起動中なら停止も）
```

ログファイルの場所:
- macOS: `~/Library/Logs/hapbeat-helper.log`
- Windows: `%LOCALAPPDATA%\hapbeat-helper\hapbeat-helper.log`

GUI からの確認:
- macOS: `~/Library/LaunchAgents/` を Finder で開く / `launchctl list | grep hapbeat` / Console.app でログを開く
- Windows: `Win+R` → `shell:startup` で Startup フォルダを開く / `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup` に直接移動

> **macOS の挙動**: `hapbeat-helper stop` は `launchctl bootout` でジョブをアンロードします。プロセスは即停止し、`KeepAlive=true` による respawn も発生しません。plist は残るため次回ログイン時には再起動します。今すぐ再起動したい場合は `hapbeat-helper install-service` を実行してください。永続的に停止するには `hapbeat-helper uninstall-service` を使います。
> **Windows の挙動**: `stop` は taskkill します。Task Scheduler タスクは残るため次回ログイン時に再起動します。再起動したい場合は再度 `install-service` を実行（または再ログイン）してください。

## トラブルシューティング (セキュリティ関連)

| 症状 | 対処 |
|---|---|
| Helper を入れたら Defender / Firewall が警告を出した | LAN 通信のための正常な動作。`プライベート ネットワーク` のみ許可で OK（`パブリック` は不要）|
| Hapbeat にどの SSID が保存されているか確認したい | Studio の **Devices** タブ → Wi-Fi プロファイル → 「⟳ 一覧取得」で SSID 一覧を表示。プロファイル個別削除も同画面から可能（パスワード本体は表示されない） |
