# CLAUDE.md — hapbeat-helper

## このリポジトリの責務

Hapbeat Studio (Web SPA) と Hapbeat デバイスの橋渡しを行う **CLI daemon**。

- ブラウザができない処理（mDNS browse / UDP broadcast / TCP raw socket）を担当
- `localhost:7703` で WebSocket を公開し、Studio から JSON コマンドを受け付ける
- pipx 配布で Mac / Windows 共通

## このリポジトリでやらないこと

- GUI を持たない
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
  - `device_registry.py` — デバイス state
  - `mdns_scanner.py` — zeroconf browse
  - `udp_listener.py` — UDP 7700 listener / sender
  - `tcp_client.py` — TCP 7701 同期クライアント
  - `protocol.py` — Layer 1 メッセージビルダ／パーサ
  - `pack_normalize.py` — Pack WAV を 16 kHz PCM16 に変換
- `tests/` — pytest

## やってはいけないこと

- PySide6 / PyQt 依存を持ち込まない（asyncio + callback で代替）
- 独自プロトコルを作らない
- contracts に反するデータ形式を使わない

## 依存

- `hapbeat-contracts`（仕様書のみ）
- 将来: `hapbeat-kit-tools`（normalize / installer の共有）
