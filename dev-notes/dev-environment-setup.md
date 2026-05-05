# 開発環境セットアップメモ（内部用）

> このファイルは **メンテナ向け** の内部開発知見。devtools.hapbeat.com には集約されない (`docs/` ではなく `dev-notes/` 配下のため)。ユーザー向けのインストール手順は `docs/getting-started.md` を参照。

## 推奨: リポジトリ直下の plain venv（pipx ではなく）

頻繁にコード編集する開発機では **pipx ではなく `.venv` を使う**。pipx は CLI 配布用には便利だが、Windows / OneDrive / git-bash の組み合わせで symlink 周りの不具合が多い。

### セットアップ

```bash
cd /c/GitHub/Hapbeat/hapbeat-sdk-workspace/hapbeat-helper
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/hapbeat-helper.exe version          # 動作確認
.venv/Scripts/hapbeat-helper.exe install-service  # 自動起動登録
```

### git-bash エイリアス

`~/.bashrc`:

```bash
alias hapbeat-helper='/c/GitHub/Hapbeat/hapbeat-sdk-workspace/hapbeat-helper/.venv/Scripts/hapbeat-helper.exe'
```

`source ~/.bashrc` 後、`hapbeat-helper start` / `logs -f` が直接叩ける。

### コード編集 → 反映ループ

`.py` の編集は editable install のため即時反映。プロセス再起動だけで OK:

```bash
hapbeat-helper stop          # 自動起動 shim 経由なら数秒で再起動 (VBS shim 再 trigger 不要)
hapbeat-helper logs -f       # 反映確認
```

依存追加 (`pyproject.toml` 編集) 時は再 install:

```bash
.venv/Scripts/python.exe -m pip install -e ".[dev]"
```

`.venv` を破棄してやり直すには:

```bash
rm -rf .venv
# → 「セットアップ」セクションから再実行
```

## pipx で起きた既知の問題（参考）

### WinError 448 — 信頼されていないマウントポイント

```
OSError: [WinError 448] 信頼されていないマウントポイントが含まれているため、
パスをスキャンできません。: 'C:\\pipx\\bin\\hapbeat-helper.exe'
```

- pipx の post-install で `Path(symlink).resolve()` が Windows の `realpath`
  を呼び、symlink traversal が拒否される
- `C:\pipx\` 自体は OneDrive 外でも、Defender Controlled Folder Access や
  類似の保護機構が引っかかっているとみられる
- 回避策: `.venv` 直下インストールに切り替え

### git-bash で `Permission denied`

```bash
$ hapbeat-helper start
bash: /c/pipx/bin/hapbeat-helper: Permission denied
```

- pipx の shim が Windows symlink で MSYS layer が exec できない
- 回避策: フルパス `.venv/Scripts/hapbeat-helper.exe` で叩くか、上記
  alias を使う

### shim 探索が PATH 経由で失敗

`install-service` が内部で `shutil.which("hapbeat-helper")` を呼ぶが、
git-bash + Windows symlink の組み合わせで `which` が
`C:\pipx\bin\hapbeat-helper.exe` を見つけても返り値で詰まる、または
そもそも返さないケース。

→ 2026-05-04 の修正で `_hapbeat_helper_path()` を sys.executable
sibling 優先に変更。venv の Python から起動すれば確実に同 venv の
exe を見つけられる。

## VBS shim の動作

Windows の install-service は `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\HapbeatHelper.vbs` に shim を生成する。中身:

```vbs
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd /c """<exe>"" start >> ""<log>"" 2>&1", 0, False
```

- `<exe>` は `_hapbeat_helper_path()` の解決結果（dev では `.venv/Scripts/hapbeat-helper.exe`、production では `C:\pipx\bin\hapbeat-helper.exe`）
- ログは `%LOCALAPPDATA%\hapbeat-helper\hapbeat-helper.log`
- `cmd /c` を挟んでいるのは stdout/stderr リダイレクトのため (VBS の `Run` 単独では redirect できない)

shim の場所が **PATH 内のリソースに依存していない** ため、git-bash の symlink 問題に影響されない。これが install-service 経由が dev でも production でも安定する理由。

## マルチワークスペース (worktree) 注意

開発中に worktree を切って作業する場合、`.venv` は worktree ごとに作る必要があるが、Windows の Startup フォルダ shim はグローバル。複数 worktree で同時に install-service を叩くと最後に install したものが勝つ。

実用上は worktree 開発時は `install-service` を使わず、フォアグラウンド起動 (`hapbeat-helper start`) で確認するのが安全。

## TestPyPI publish (release flow)

`pipx upgrade hapbeat-helper` で end user が引ける状態にする手順:

```bash
cd /c/GitHub/Hapbeat/hapbeat-sdk-workspace/hapbeat-helper
# 1. version bump (pyproject.toml + src/hapbeat_helper/__init__.py)
# 2. ビルド
.venv/Scripts/python.exe -m pip install build twine     # 初回のみ
python -m build
# 3. upload
$env:TWINE_USERNAME="__token__"
$env:TWINE_PASSWORD="pypi-XXXX..."
python -m twine upload --repository testpypi dist/*
```

`~/.pypirc` に testpypi セクションを設置しておくと環境変数不要。

## 関連メモリ

- `workspace/docs/agent-memory/feedback_python_cli_dev_pipx_vs_venv.md` （次セッションで参照）
