# CLAUDE.md — hapbeat-helper

## このリポジトリの責務

Hapbeat Studio (Web SPA) と Hapbeat デバイスの橋渡しを行う **CLI daemon**。

- ブラウザができない処理（mDNS browse / UDP broadcast / TCP raw socket）を担当
- `localhost:7703` で WebSocket を公開し、Studio から JSON コマンドを受け付ける
- pipx 配布で Mac / Windows / Linux 共通

## このリポジトリでやらないこと

- GUI を持たない（Manager の PySide6 UI は移植しない）
- ファームウェア書込み（USB Serial）は Studio が Web Serial API で直接行う
- 独自プロトコルを作らない（contracts に従う）

## 全体アーキテクチャ上の役割

```
Studio (https://devtools.hapbeat.com)
        │  ws://localhost:7703
        ▼
hapbeat-helper (this repo)
        │  UDP 7700 / TCP 7701 / mDNS
        ▼
   Hapbeat devices
```

`hapbeat-manager` (PySide6) の **非 GUI 機能** をここに移植する。Manager は
DEC-026 で deprecated。

## 技術スタック

| ライブラリ | 用途 |
|-----------|------|
| websockets | WebSocket サーバー (asyncio) |
| zeroconf | mDNS デバイス検出 |
| asyncio | ランタイム全般 |

## ディレクトリ構造

- `src/hapbeat_helper/` — 本体
  - `cli.py` — `hapbeat-helper` CLI エントリ
  - `server.py` — asyncio WebSocket サーバー
  - `device_registry.py` — デバイス state（Manager 版から Qt 除去）
  - `mdns_scanner.py` — zeroconf browse（Manager 版から Qt 除去）
  - `udp_listener.py` — UDP 7700 listener / sender
  - `tcp_client.py` — TCP 7701 同期クライアント
  - `protocol.py` — Layer 1 メッセージビルダ／パーサ
  - `pack_normalize.py` — Pack WAV を 16 kHz PCM16 に変換
- `tests/` — pytest

## やってはいけないこと

- PySide6 / PyQt 依存を持ち込まない（asyncio + callback で代替）
- 独自プロトコルを作らない
- `hapbeat-manager` を import しない（必要なら code を移植する）

## 依存

- `hapbeat-contracts`（仕様書のみ）
- 将来: `hapbeat-kit-tools`（normalize / installer の共有）

## 指示書

- 元になる指示書: `../docs/instructions-hapbeat-helper-mvp-202604251800.md`

## エージェント共通メモリ

- `../docs/agent-memory/` を共通メモリ置き場とする
- インデックス: `../docs/agent-memory/INDEX.md`
