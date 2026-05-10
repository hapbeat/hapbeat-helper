# Instructions: helper 常駐化動作確認 + 本番 PyPI publish (v0.1.1 release)

**発行日:** 2026-05-10
**起票:** workspace session 2026-05-10 (PyPI release 準備 + multi-target OTA)
**優先度:** 即着手

## 背景

helper 0.1.1 が TestPyPI に publish 済 (GitHub Actions OIDC 経由)。
本セッションで完了したのは以下:

- 全機能修正・cleanup・dead code 削除
- TestPyPI に v0.1.1 アップロード成功 (`https://test.pypi.org/project/hapbeat-helper/0.1.1/`)
- Studio dev plugin が `build_version.h` 優先で読むよう修正済
- multi-target OTA (Helper v0.2.10) と sequential 順次対応済

残作業 (本 instruction):
1. 常駐化 (auto-start) 機能の動作検証
2. 動作 OK なら本番 PyPI に v0.1.1 を publish

## タスク

### 1. TestPyPI 版で動作確認

TestPyPI から install して foreground / 常駐起動の双方を試す。

```powershell
# 既存 helper を停止 (前回セッションの check で動いてたら)
hapbeat-helper stop
tasklist | findstr python    # 残ってたら taskkill /F /PID <番号>

# TestPyPI から install
pipx install --force --pip-args="--extra-index-url https://pypi.org/simple/" --index-url https://test.pypi.org/simple/ hapbeat-helper==0.1.1

# バージョン確認
hapbeat-helper version    # → hapbeat-helper 0.1.1

# foreground 起動で基本動作 OK か (Studio 接続 / OTA / Wi-Fi 設定)
hapbeat-helper start
```

実機 OTA / Wi-Fi 設定 / log_tail を 1 周してみる。
v0.2.10 で multi-target OTA も入っているので、複数 device 選択で順次 OTA 走るかを併せて確認できると尚良。

### 2. 常駐化動作確認 (本セッションのメインスコープ)

Windows の Startup folder VBS shim 方式 (v0.2.0 以降) で、**ログイン時自動起動** が想定通り動くかを検証。

```powershell
# foreground を Ctrl+C で停止してから
hapbeat-helper install-service

# 状態確認
hapbeat-helper service-status     # → registered, running

# Startup folder の shim ファイル確認
Test-Path "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\hapbeat-helper.vbs"
# → True

# log file の場所確認
hapbeat-helper logs -n 50         # → 末尾 50 行表示

# 一旦サインアウト → 再サインイン (or PC 再起動) して、
# 自動起動できているか確認:
hapbeat-helper status             # → reachable (自動起動成功)
hapbeat-helper logs -n 100        # → 起動ログ出ている
```

確認すべきポイント:
- [ ] サインイン後に hidden window で helper が立ち上がるか (タスクマネージャに python.exe が居るか)
- [ ] Studio から接続できるか (devtools.hapbeat.com/studio で「Helper 接続中」)
- [ ] log file が `%LOCALAPPDATA%\hapbeat-helper\hapbeat-helper.log` に書かれているか
- [ ] `hapbeat-helper logs -f` で stream 追跡できるか

### 3. uninstall フローの確認

```powershell
hapbeat-helper uninstall-service
hapbeat-helper service-status     # → not registered
Test-Path "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\hapbeat-helper.vbs"
# → False
```

uninstall 後にサインアウト → 再サインインで helper が起動 **しない** ことも確認。

### 4. 本番 PyPI への publish (検証 OK 後のみ)

**事前準備 (一度だけ):**

1. **PyPI 本番アカウント** を作成 (TestPyPI とは別)
   - <https://pypi.org/account/register/>
2. **Pending Publisher 登録**
   - <https://pypi.org/manage/account/publishing/>
   - "Add a new pending publisher" で:
     - PyPI Project Name: `hapbeat-helper`
     - Owner: `hapbeat`
     - Repository name: `hapbeat-helper`
     - Workflow filename: `publish.yml`
     - Environment name: `pypi`
3. **GitHub repo Environment 作成**
   - <https://github.com/hapbeat/hapbeat-helper/settings/environments>
   - `New environment` → `pypi` (既に作成済みなら skip)
   - 任意で Required reviewers 設定 (本番 publish に承認フロー)

**Publish 実行:**

タグ push で publish-pypi job が自動起動する仕組み。`-` を含まないタグ (例: `v0.1.1`) が本番 PyPI に published される。

```bash
cd /c/GitHub/Hapbeat/hapbeat-sdk-workspace/hapbeat-helper
git tag v0.1.1
git push origin v0.1.1
```

GitHub Actions タブで `Publish to PyPI` workflow が走るのを待つ。
成功すると <https://pypi.org/project/hapbeat-helper/0.1.1/> に表示。

**Publish 後の確認:**

```powershell
# TestPyPI 版を一旦消す
pipx uninstall hapbeat-helper

# 本番 PyPI から install
pipx install hapbeat-helper

hapbeat-helper version       # → hapbeat-helper 0.1.1
hapbeat-helper start         # 動作確認
```

### 5. README に PyPI install 案内を反映

公開後は README.md の install 手順を「pipx install hapbeat-helper」だけに整理。
Getting Started docs (`docs/getting-started.md`) も同様。

## 完了条件

- [ ] TestPyPI 0.1.1 で foreground / 常駐両方動作確認
- [ ] `install-service` → サインアウト/イン → 自動起動の確認
- [ ] `uninstall-service` で確実に解除されることの確認
- [ ] PyPI 本番 Pending Publisher + Environment 設定完了
- [ ] `git tag v0.1.1 && git push origin v0.1.1` で本番 PyPI publish 成功
- [ ] `pipx install hapbeat-helper` で本番版が install できることを確認
- [ ] README / getting-started の install コマンドを最終形に整理 (pipx install hapbeat-helper)
- [ ] 本ファイルを `instructions/completed/` に移動

## 依存関係

- **Required**: なし (TestPyPI publish は完了済、本セッション資産で完結)
- **Downstream**:
  - 本番 publish 後、Studio や hapbeat-devtools-site の install 案内更新を検討
  - `claude-shared-config` 等で「first-time-setup スクリプト」を作るなら本物の `pipx install hapbeat-helper` を組み込む

## 補足: 過去の経験 (CF block / filename 予約)

- TestPyPI で local twine upload が CF block されるケースあり (本セッション経験)
- ファイル名は globally unique forever (削除後も再 upload 不可)
- 本セッションで Trusted Publishing (CI) 経由が確立しているので、**本番 publish は CI 経由のみ** とする
- 詳細は `dev-notes/helper/pypi-publishing-notes.md` (workspace private) 参照
