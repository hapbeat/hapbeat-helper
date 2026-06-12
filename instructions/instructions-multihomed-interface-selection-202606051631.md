# 指示書: 送信インターフェイス選択 / subnet-directed broadcast（Helper）

- **起点**: hapbeat-sdk-workspace セッション（2026-06-05）
- **マスター指示書**: `../../docs/instructions-multihomed-interface-selection-202606051631.md`（workspace ルート）
- **前提**: contracts の仕様確定（`hapbeat-contracts/instructions/instructions-multihomed-broadcast-spec-202606051631.md`）後に着手するのが望ましい。

## 背景（要約）

マルチホーム PC（Wi-Fi=Hapbeat / 有線=別ネットワーク）で、UDP の limited broadcast
`255.255.255.255` が優先 NIC（多くは有線）からしか出ず、Hapbeat に届かない。
Studio のオンライン判定（PING/PONG）が落ちる。詳細はマスター参照。

## このリポジトリでやること

### 1. UDP 送信に NIC 選択 / subnet-directed broadcast を追加

- 対象: `src/hapbeat_helper/udp_listener.py`
  - `start()`: 必要なら送信ソケットを選択ローカル IP に bind（`sock.bind((local_ip, port))`）、
    または送信時に宛先を subnet-directed broadcast に切替えられるようにする。
  - `send_broadcast_ping()` / `send_raw(..., "<broadcast>")`（L124, L138 付近）:
    宛先 `255.255.255.255` を、選択インターフェイスのサブネット宛 directed broadcast
    （例 `192.168.10.255`）に差し替え可能にする。未選択時は従来どおり `255.255.255.255`。
- インターフェイス列挙: ローカル NIC の (IP, netmask) を取得して directed broadcast を計算。
  zeroconf 経由で得られる情報、または標準ライブラリ（`socket` + 補助）で実装。
  外部依存を増やす場合は `pyproject.toml` と相談（CLAUDE.md: 依存は最小に）。

### 2. WS API でインターフェイス列挙・選択を公開

- 対象: `src/hapbeat_helper/server.py`
  - 新規 WS コマンド例: `list_interfaces`（候補 NIC を Studio へ返す）/
    `set_send_interface`（選択を保持）。
  - 既存の `rescan`（L312 付近）・broadcast 送信（L640, L663 付近）が選択 NIC を使うよう配線。
  - 選択は daemon 内に保持（再起動間の永続化は任意、まずはメモリ保持で可）。

## 注意（スコープ）

- Helper の対応で直るのは **Studio の online/offline 表示と Studio 発の送信**まで。
  コンテンツアプリの触覚は SDK が直接 broadcast するため、別途 SDK 側対応が必要
  （`hapbeat-unity-sdk` ほか）。本書は Helper 分のみ。

## 検証

- マルチホーム PC でメトリック調整なしに Studio が online を表示できること。
- 単一 NIC 環境で回帰がないこと（既定 `255.255.255.255` 動作維持）。
- `pytest` が通ること。

## 完了

- 動作確認は **別環境でのユーザー検証後に push**（配布物 repo ポリシー）。
- 本書を `instructions/completed/` へ移動。
