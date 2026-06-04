# 事後承認 note: ノード役割 (role/transport) のリレー追加 (DEC-034)

- **編集元セッション**: hapbeat-sdk-workspace (起点 repo)、2026-06-04
- **関連 DEC**: DEC-034（ツールチェーン mode-aware 化）
- **依存仕様**: `hapbeat-contracts/specs/node-roles.md` / `serial-config.md` §4b

## 背景

Studio に MQTT / ESP-NOW / sensor / broker などノード役割を「デバイス属性」として載せる（DEC-034）。Helper は Studio ↔ デバイスの TCP リレーなので、新フィールド/新コマンドを中継できるよう拡張した。

## 入った変更（`src/hapbeat_helper/server.py`）

1. **get_info_result の mapper 拡張** — 既存（name/mac/fw/build/group/wifi_connected/board）に加えて、デバイスが返す `role` / `transport` / `transports` と役割固有フィールド（`espnow_channel` / `gain` / `input_level` / `broker_host` / `static_octet` / `mqtt_port` / `mqtt_running` / `mappings_count`）を passthrough するように追加。
2. **役割別 config コマンドのリレー追加**（`_handle_tcp_command` / `_handle_passthrough_query` 経由）:
   - `set_broker_host`（host）
   - `set_espnow_channel`（channel）
   - `set_gain`（gain）
   - `set_input_level`（level）
   - `set_broker_config`（static_octet / port）
   - `set_sensor_mapping`（mappings）
   - `get_sensor_mapping` → `sensor_mapping_result`（passthrough）

いずれも device の TCP 7701 JSON config handler へ素通し。役割に合わないコマンドは firmware が error を返し、Studio がそれを表示する。

## 横断背景

- Helper は汎用 `{type,payload}` リレーで、UDP/MQTT/ESP-NOW の搬送はデバイス/firmware 側にある。Helper は **Wi-Fi に乗るノード**（udp/mqtt 受信機・broker）への TCP 設定リレーのみ担う。
- broker は M5 組み込み採用（DEC-034）のため Helper は broker をホストしない。
- USB 専用ノード（ESP-NOW 受信機・transmitter・sensor）は Studio の Web Serial 直で設定し、Helper を経由しない（同じ JSON コマンドを serial で送る）。

## 検証状況

- `python -m py_compile src/hapbeat_helper/server.py` 通過。
- 実機（デバイス）との往復は未検証（firmware 側が role/transport を実装後に end-to-end 確認）。

## この repo セッションへのアクション

1. 変更をレビューし問題なければ本 note を `instructions/completed/` へ移動。
2. 任意: `device_list`（mDNS/PONG 由来）に role を載せたい場合、firmware が mDNS TXT / PONG で role を広告する必要がある（現状は get_info 取得後に Studio が役割を判定するため必須ではない）。将来 device list 段階で役割バッジを出すなら firmware 側 advertise + helper passthrough を検討。
3. 配布物 repo（PyPI）のため、push / version bump はユーザーの別環境検証後。

## 連動して変更された他 repo（参考）

- contracts: node-roles / mqtt-transport / espnow-stream / firmware-distribution spec + serial-config 追記（applied note 別途）。
- studio（セッション対象）: role 別 UI 一式（build 通過）。
- device-firmware / transmitter-firmware: forward instruction 起票（未実装）。
