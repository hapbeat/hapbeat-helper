# PyPI publishing メモ

hapbeat-helper を PyPI に公開する際の前提知識・運用メモ。
公開ドキュメントではない（ユーザーは pip install するだけで完結する）ので
dev-notes/ 配下に置く。

## モデル整理

### サーバー / プロジェクト / token の階層

```
┌─ PyPI (https://pypi.org) ─ サーバー（インデックス）─────────┐
│                                                            │
│   📦 hapbeat-helper        ← project (PyPI 上の登録名)      │
│      ├ v0.1.0  v0.1.1 ...  ← release (各 version)          │
│      ├ entry_points: hapbeat-helper コマンド                │
│      └ depends on: websockets, zeroconf                    │
│                                                            │
│   📦 hapbeat-bridge        ← (将来) 別 project              │
│   📦 numpy / requests / 他 50万 project ...                 │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

| 概念 | 単位 | 例 |
|---|---|---|
| サーバー | 1 個 (PyPI / TestPyPI / 社内 mirror それぞれ) | `https://pypi.org` |
| project | サーバー内に N 個 | `hapbeat-helper`, `hapbeat-bridge` |
| release | project 内に N 個 | `0.1.0`, `0.1.1`, ... |
| account | サーバーごとに 1 個 (publisher 識別) | `yus988` |
| token | account-scoped or project-scoped | `pypi-AgEI...` |

### sdk-workspace との関係

sdk-workspace は **ローカルの開発便宜** であって PyPI には何も上がらない。
各 sub-repo (`hapbeat-helper`, `hapbeat-bridge`, ...) が **完全に独立した
PyPI project** として上がる。中央 master config は無い。

```
sdk-workspace/                  ← ローカル親フォルダ (PyPI 非対応)
├── hapbeat-helper/             ← Python lib ✓ PyPI 上げる
│   └── pyproject.toml          ← name="hapbeat-helper" 完結
├── hapbeat-bridge/             ← Python lib ✓ (将来)
├── hapbeat-studio/             ← React/TS ✗ PyPI 対象外
├── hapbeat-device-firmware/    ← C++ ✗ PyPI 対象外
└── hapbeat-unity-sdk/          ← Unity Package ✗ PyPI 対象外
```

ユーザー目線:
```bash
pip install hapbeat-helper            # PyPI から helper だけ install
hapbeat-helper start                   # CLI コマンドが pip により shim 配置
```

「sdk-workspace」は意識されない。

## `~/.pypirc` の書き方

### 場所

- **Windows**: `C:\Users\<user>\.pypirc` (= git-bash の `~/.pypirc`)
- **macOS / Linux**: `~/.pypirc`
- 任意の場所に置きたい場合は `twine upload --config-file <path>` で指定

### 標準フォーマット (本番 PyPI + TestPyPI)

```ini
[distutils]
index-servers =
    pypi
    testpypi

[pypi]
username = __token__
password = pypi-AgEI...（PyPI で発行した token）

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-AgEI...（TestPyPI で発行した token）
```

### ポイント

- `index-servers` は複数行、2 行目以降は **インデント必須** (タブ or スペース)
- セクション名は任意 (`[pypi-helper]` でも `[my-private]` でも OK)
- `[pypi]` の `repository` は省略 (twine デフォルト `https://upload.pypi.org/legacy/`)
- `[testpypi]` は `repository = https://test.pypi.org/legacy/` を **必須**
- `username = __token__` は **literal の魔法文字列** (「password 欄は API token」の合図)
- `password` に token を `pypi-` プレフィックス込みで丸ごと貼る

### 複数 project / project-scoped token を分けたい場合

セクション名を別にして書く。`repository` は同じでも OK。

```ini
[distutils]
index-servers =
    pypi-helper
    pypi-bridge
    testpypi

[pypi-helper]
repository = https://upload.pypi.org/legacy/
username = __token__
password = pypi-AgEI...（hapbeat-helper 専用 project-scoped token）

[pypi-bridge]
repository = https://upload.pypi.org/legacy/
username = __token__
password = pypi-AgEI...（hapbeat-bridge 専用 project-scoped token）

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-AgEI...
```

Upload 時:
```bash
twine upload --repository pypi-helper dist/*    # ← セクション名で指定
```

## token の運用

### account-scoped vs project-scoped

| 種類 | 権限 | 主な用途 |
|---|---|---|
| **account-scoped** | あなたのアカウントの全 project に upload 可 | 初回 publish 時（project がまだ無い）/ CI で複数 project 担当 |
| **project-scoped** | 特定の 1 project のみ | 通常運用（漏洩時の被害最小化） |

### 推奨フロー

1. **初回**: account-scoped token を発行 → upload (これでプロジェクトが PyPI に出現)
2. **2 回目以降**: project-scoped token を発行 → account-scoped を **revoke**
3. 別 project を上げるときも 1 → 2 を繰り返す

### 発行 URL

- 本番: <https://pypi.org/manage/account/token/>
- TestPyPI: <https://test.pypi.org/manage/account/token/> (PyPI とは **別アカウント** が必要)

## ビルド & upload 手順

### 必要パッケージ

```bash
.venv/Scripts/python -m pip install --upgrade build twine
```

### ビルド

```bash
# 古い dist/ をクリア (mtime 順で twine が誤った wheel を上げないように)
rm -rf dist/ build/ src/*.egg-info/
.venv/Scripts/python -m build
# → dist/<name>-<ver>-py3-none-any.whl と dist/<name>-<ver>.tar.gz が生成
```

### metadata 検証

```bash
.venv/Scripts/python -m twine check dist/*
# → 全 file PASSED が出ること (README rendering check 含む)
```

### TestPyPI に upload

```bash
.venv/Scripts/python -m twine upload --repository testpypi dist/*
```

成功後の確認:
- ブラウザで <https://test.pypi.org/project/hapbeat-helper/> を開いて release が見えるか
- pipx でインストールテスト:
  ```bash
  pipx install --pip-args="--extra-index-url https://pypi.org/simple/" \
    --index-url https://test.pypi.org/simple/ hapbeat-helper==0.1.0
  hapbeat-helper version
  hapbeat-helper start
  ```
  - `--extra-index-url` は **本番 PyPI から依存 (websockets 等) を取りに行くため必須** (TestPyPI に依存パッケージのフルセットは無い)

### 本番 PyPI に upload

TestPyPI で OK が出たら:

```bash
.venv/Scripts/python -m twine upload dist/*
# (--repository 省略時のデフォルトが pypi)
```

## 失敗パターンと対処

| エラー | 原因 | 対処 |
|---|---|---|
| `403 The user '<x>' isn't allowed to upload to project 'hapbeat-helper'` | project がすでに別 account の所有 / token が project-scoped で別 project 用 | token と project の対応を再確認 |
| `400 File already exists` | 同じ version を既に上げた | version を bump (PyPI は immutable、同 ver 再 upload 不可) |
| `400 The description failed to render` | README に sphinx/MyST 構文混入で PyPI 側 RST renderer が拒否 | `twine check` で事前検出 / `pyproject.toml` で `readme` の content-type を `text/markdown` 明示 |
| `InvalidDistribution: Cannot find file (or expand pattern)` | `dist/` 空 / `build` 失敗 | `python -m build` を先に走らせる |
| TestPyPI で pipx install が依存解決失敗 | TestPyPI に websockets が無い | `--extra-index-url https://pypi.org/simple/` で本番から取らせる |

## 過去のトラブル経験

なし (初回 publish 予定 — 2026-05-10 時点)。

何か起きたらこの表に追記する。

## 参考リンク

- [Packaging User Guide — Distributing](https://packaging.python.org/en/latest/tutorials/packaging-projects/)
- [twine man page](https://twine.readthedocs.io/en/stable/)
- [PEP 621 — pyproject.toml metadata](https://peps.python.org/pep-0621/)
