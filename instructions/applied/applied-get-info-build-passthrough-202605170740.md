# [applied] get_info_result の `build` 転送 + Kit manifest filename 規約追従

**起点セッション:** hapbeat-studio (workspace worktree `recursing-curie-cd0c3f`)
**日付:** 2026-05-17
**関連指示書:**
- `hapbeat-studio/instructions/completed/instructions-show-build-commit-sha-202605101500.md`
- `hapbeat-studio/instructions/completed/instructions-kitname-manifest-rename-202605161800.md`
**関連 commit (firmware):** `hapbeat-device-firmware` 1be8711 (FIRMWARE_VERSION 自動生成 + `cmdGetInfo.build`)

## 当 repo の変更

### 1. `src/hapbeat_helper/server.py` — `get_info` allowlist に `build` 追加

`get_info` ハンドラの response allowlist に `"build": r.get("build")` を追加。
firmware が `cmdGetInfo` で送る commit short SHA を Studio まで素通しする。

### 2. `src/hapbeat_helper/server.py` — Kit manifest 探索 + wire rename

- `_looks_like_manifest(filename)` / `_find_kit_manifest(pack_dir)` ヘルパー追加
- `_deploy_kit_to_device`:
  - manifest 探索を `<pack_dir.name>-manifest.json` 優先 → `*manifest*.json` fallback に変更
  - TCP 転送時に manifest の wire path を `manifest.json` に rewrite (firmware は LittleFS から `manifest.json` を literal で読むため)
  - 同じ kit ルートに 2 つ以上 manifest 候補があれば片方だけを転送 (両方積むと device 側が ambiguous になる)

### 3. `src/hapbeat_helper/pack_normalize.py` — 同じ探索ロジックに追従

- `_find_manifest(pack_dir)` ヘルパー追加
- `normalize_pack` も新規約のファイル名を見つけるように変更

## 横断的な背景

### build フィールド

device firmware が `cmdGetInfo` 応答に commit short SHA (`r["build"] = BUILD_COMMIT_SHA`, tcp_server.cpp:132) を載せるようになった。helper は明示的な field allowlist で `get_info_result` を整形するため、`build` も列挙しないと Studio まで届かない。
Studio 側 (`hapbeat-studio/src/components/devices/DeviceDetail.tsx`) では fw 表示の隣に `(<sha>)` を併記して、`0.1.2d3` のような dev build を別 commit で識別できるようにする UX 改修を入れた。

### Manifest filename 規約

Unity SDK ですでに採用された Kit manifest 命名規約 `<kit-name>-manifest.json` を Studio が出力側で追従するための変更が走っている (起点指示書: `instructions-kitname-manifest-rename-202605161800.md`)。
device firmware は LittleFS から `manifest.json` を literal で読む (kit_loader.cpp:289 / kit_installer.cpp:540 ほか) ため、helper は wire 上で manifest の名前を `manifest.json` に rewrite して転送する。
on-disk filename の規約変更だけが SDK / OS Explorer 側の視認性向上のための変更。

## 検証状況

- 構文: `python -c "from hapbeat_helper.server import _find_kit_manifest, _looks_like_manifest"` OK
- ユニットテスト: `pytest tests/` 7 件 pass
- 手動 smoke: preferred / legacy / missing の各ケースで `_find_kit_manifest` が正しい結果を返すことを確認
- ランタイム検証 (実機 + Studio): 未実施 — ユーザー側で完了確認後 `completed/` へ
- 互換性:
  - 古い firmware (`build` 未送信): `r.get("build")` が `None` → Studio 側はオプショナル参照なので問題なし
  - 旧名 `manifest.json` のままの kit: `_find_kit_manifest` の fallback で発見、wire rename 後は問題なし
  - 新名 `<kit>-manifest.json`: wire 上で `manifest.json` に rewrite するため firmware 側は影響なし

## helper エージェントへのアクション

- ランタイム検証 (実機 + Studio で `0.1.x (xxxxxxx)` 表示 + Kit deploy 動作) を済ませてから `completed/` に移動
- pack_normalize.py に既存の `clips` キー (DEC-027 で `install_clips` に変更済) を見ている dead code が残っている (manifest filename とは別問題)。気になるなら別途整理 instruction を起こすこと推奨
