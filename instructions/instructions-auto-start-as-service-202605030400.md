# Instructions: Helper を OS サービスとして自動起動できるようにする

**発行日:** 2026-05-03
**起票:** mac 動作検証セッション (workspace) — Studio 起動 UX 改善 ADD task
**優先度:** 中（毎回 Helper 起動の手間を解消する重要 UX 改善）
**対象 repo:** `hapbeat-helper` + `hapbeat-studio`

## 背景

現状、ユーザーは Studio を使うたびにターミナルを開いて
`hapbeat-helper start --foreground` を打つ必要がある。これが毎回の障壁
になっており、「Hapbeat を使う = ターミナル操作を要する」印象を残してしまう。

ブラウザは security 上、任意プロセスを起動できないため、ブラウザボタンから
直接 Helper を立ち上げることは不可能。代わりに **OS サービスとして
ログイン時自動起動** にすればこの問題自体が消える。

Helper README にも `OS-level service installation (launchd / systemd /
Windows Service) is planned for a future release.` と既に明記されており、
方針として確定済み。

## ゴール

1. **初回 1 コマンドで Helper が auto-start サービスとして登録される**
2. **以降、ユーザーがログインすると Helper が自動起動 → Studio を開けば即接続**
3. **Studio 側の「Helper 未接続」バッジは原因と install-service コマンドを
   提示するモーダルに昇格** (バッジ → クリック → モーダル)

## Helper 側 — 新規 CLI

### サブコマンド

```bash
hapbeat-helper install-service     # 自動起動サービスを登録 + 即起動
hapbeat-helper uninstall-service   # 解除 + 停止
hapbeat-helper service-status      # 登録状態 / 起動状態を表示
hapbeat-helper start --foreground  # 既存 (dev / debug 用、残す)
```

### OS 別実装

#### macOS (launchd)

- 生成先: `~/Library/LaunchAgents/com.hapbeat.helper.plist`
- 起動: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.hapbeat.helper.plist`
  (`launchctl load` は deprecated、`bootstrap` 推奨)
- ログ出力: `~/Library/Logs/hapbeat-helper.log` (`StandardOutPath` /
  `StandardErrorPath`)
- plist テンプレート:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>          <string>com.hapbeat.helper</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/hapbeat-helper</string>
    <string>start</string>
    <string>--foreground</string>
  </array>
  <key>RunAtLoad</key>      <true/>
  <key>KeepAlive</key>      <true/>
  <key>StandardOutPath</key> <string>/Users/.../Library/Logs/hapbeat-helper.log</string>
  <key>StandardErrorPath</key><string>/Users/.../Library/Logs/hapbeat-helper.log</string>
</dict>
</plist>
```

`/path/to/hapbeat-helper` は `shutil.which("hapbeat-helper")` で解決して
plist に焼き込む（pipx の shim path）。

#### Linux (systemd --user)

- 生成先: `~/.config/systemd/user/hapbeat-helper.service`
- 起動: `systemctl --user enable --now hapbeat-helper.service`
- 注意: `loginctl enable-linger <user>` を案内する必要あり (ログアウト後も
  service を維持する場合)

```ini
[Unit]
Description=Hapbeat Helper daemon
After=network.target

[Service]
ExecStart=/path/to/hapbeat-helper start --foreground
Restart=on-failure

[Install]
WantedBy=default.target
```

#### Windows (Task Scheduler)

最小依存で実装するなら `schtasks` を subprocess 呼び出し:

```powershell
schtasks /create /tn HapbeatHelper /sc onlogon /tr `
  "C:\Users\<user>\.local\bin\hapbeat-helper.exe start --foreground" `
  /rl highest /f
```

代替案 NSSM (Non-Sucking Service Manager):
- メリット: 真の Windows Service として動く / GUI ログ出力 / 失敗時自動再起動が標準
- デメリット: 別 install (chocolatey or 手動 DL)。MVP は schtasks で十分

WIN: `--foreground` のままだとコンソールウィンドウが開くため、`pythonw.exe`
で実行 or PowerShell の `Start-Process -WindowStyle Hidden` ラッパが必要。
詳細実装で要検討。

### 実装の方針

- `src/hapbeat_helper/service/` ディレクトリ新設
- `service/macos.py`, `service/linux.py`, `service/windows.py` で OS 別実装
- `cli.py` から OS 判定して dispatch
- 各 install/uninstall/status 関数は副作用 (ファイル生成 / launchctl 呼び出し)
  をそれぞれ自己完結
- pytest は subprocess を mock しつつ plist/service ファイル生成内容を検証

### 完了条件 (Helper 側)

- [ ] `hapbeat-helper install-service` で 3 OS 共に常駐起動できる
- [ ] OS 再起動後 / ログイン後に自動起動する
- [ ] `hapbeat-helper uninstall-service` で完全に解除できる (残骸ファイルなし)
- [ ] `hapbeat-helper service-status` で 「未登録 / 登録済停止 / 登録済起動中」
      の 3 状態を識別できる
- [ ] README に install-service 手順を追加 + Manager の `start` 系より上に配置
- [ ] docs/getting-started.md にも「ログイン時自動起動 (推奨)」セクションを追加

## Studio 側 — 「Helper 未接続」バッジを情報モーダルに

### UI

現状:
- 上部右に `Helper 未接続` バッジ (赤) が表示されるだけ

改善後:
- バッジクリック可能 → モーダル展開
- モーダル内容:
  - 状況説明: 「Helper が起動していないか、ポート 7703 が使用中です」
  - **推奨**: 「Helper を自動起動サービスにする (1 回のみ)」セクション
    - OS 別タブ (Mac / Win / Linux)
    - コピペ用 1 コマンドを表示 + コピーボタン
    - `hapbeat-helper install-service`
  - **今だけ起動**: `hapbeat-helper start --foreground` + コピーボタン
  - フッタ: `[接続を再試行]` ボタン と `[Helper のドキュメント →]` リンク

### Phase 2 (オプション): カスタム URL スキーム

Helper の install-service が `hapbeat-helper://` スキームを OS register する
ようにし、Studio モーダルに `[ 自動起動を有効にする ]` ボタンを追加。
クリック → ブラウザが OS にスキーム解決を依頼 → Helper が install-service を
実行 → モーダルが状態を polling して自動的に閉じる。

これは Phase 1 完成後の polish。Phase 1 だけでも十分価値ある。

### 実装ファイル候補

- `src/components/HelperConnectionBadge.tsx` (新規 or 既存改修) — クリック → モーダル
- `src/components/HelperOnboardingModal.tsx` (新規) — モーダル本体
- `src/hooks/useHelperConnection.ts` — 既存 (再試行 API を export)

### 完了条件 (Studio 側)

- [ ] 「Helper 未接続」バッジがクリック可能になっている
- [ ] モーダルに 3 OS 別の install-service コマンド + コピーボタン
- [ ] 接続成功でモーダル自動 close
- [ ] バッジの hover で「クリックで詳細」が表示される (現状 hover なし)

## 段階リリース計画

| Phase | スコープ | 期間目安 |
|---|---|---|
| 1a | Helper macOS launchd 実装 | 0.5 日 |
| 1b | Helper Linux systemd 実装 | 0.5 日 |
| 1c | Helper Windows Task Scheduler 実装 | 1 日 (pythonw / hidden window 検証) |
| 1d | Studio バッジ → モーダル UI | 0.5 日 |
| 2 | (Phase 2) URL scheme 統合 | 1〜2 日 |

各 OS 別に実機検証が必要なため、Helper 側は **1a を先行リリース → mac で
動作確認 → 1b/1c を順次** が現実的。

## 関連ファイル

- `hapbeat-helper/src/hapbeat_helper/cli.py` (主対象)
- `hapbeat-helper/src/hapbeat_helper/service/` (新設ディレクトリ)
- `hapbeat-helper/README.md`, `docs/getting-started.md` (案内追加)
- `hapbeat-studio/src/components/HelperConnectionBadge.tsx` (バッジ改修)
- `hapbeat-studio/src/components/HelperOnboardingModal.tsx` (新設)

## 注意

1. **macOS Notarization**: launchd plist は問題ないが、Helper 自体が pipx
   経由の Python script なので Gatekeeper の問題は出ない。気にする必要なし
2. **Windows Defender**: 初回起動時に Defender が反応する可能性。pyinstaller
   などで .exe 化していないため (pipx の shim 経由)、誤検知リスクは低い
3. **既存の `hapbeat-helper start --foreground` を残す**: dev workflow と
   一時的な debug 用。`install-service` は重ねて呼ばれた場合 idempotent に
   する (既存 plist がある場合は overwrite + reload)
