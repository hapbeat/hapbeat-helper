# Applied: `_do_ota_to_device` のエラーメッセージに phase タグを追加

**起点 repo**: `hapbeat-studio` (onboarding 改善継続セッション、workspace)
**日付**: 2026-05-01

## 横断的な背景

ユーザー報告 (2026-04-30): Wi-Fi OTA 書き込みが失敗して `0/1 ok` だけ
出るので原因が分からない。`_send_tcp_to_many` 系には phase タグ済の
詳細メッセージが入っていたが、`_do_ota_to_device` だけ未対応で、Studio
log drawer / OTA 結果にそっけないメッセージしか出ていなかった。

## このリポジトリに入った変更

### `src/hapbeat_helper/tcp_client.py` — `TcpRawConnection.close`

`socket.shutdown(SHUT_RDWR)` → `socket.close()` の順に変更。FIN を即時
送出することで、firmware の `s_client.connected()` を早く false に
させる。これがないと、ユーザーから報告された「ボタンを押しても詰まる
／何度も押すと通る」症状の一因になる (詳細は device-firmware の
`instructions-tcp-stale-client-keepalive-202605011700.md`)。

### `src/hapbeat_helper/server.py` — `_do_ota_to_device`

エラー path のメッセージを以下のように phase タグ付き + 具体化:

| 旧                    | 新                                                         |
|----------------------|-----------------------------------------------------------|
| `connect failed`      | `phase=connect: TCP 7701 → <ip> 接続失敗 (Nms). 電源 OFF→ON …` |
| `ota_begin nack: …`   | `phase=ota_begin: nack <resp>`                            |
| `device timeout`      | `phase=verify: 30 秒応答なし。Update.end が失敗している可能性 — シリアルログ確認…` |
| `OTA error`           | `phase=verify: <firmware message>`                        |
| `io error: …`         | `phase=io: <ExceptionType>: <msg>`                        |

これで Studio 側の log drawer に `phase=connect/ota_begin/verify/io`
ラベル付きで原因が表示される (既存の `_send_tcp_to_many` の phase ラベル
規約と同じ)。

`OSError` を `connect()` の周りでも catch するようにし、`raise` する
パターン (sock オプション失敗など) の文字列化を保証。

## Studio 側の関連変更 (FYI)

- `FirmwareSubTab.tsx` に post-OTA BUILD_TAG verify を追加。OTA ok 後
  8 秒待って `get_info` を投げ、`fw` を期待値と比較。mismatch を
  「otadata 切替失敗の可能性」エラーとして表示。
- `DisplayEditor.handleDeploy` で `serial:` プレフィックスをフィルタ
  アウト (Display は LAN 専用、Serial pseudo-device は別途案内)。
- `WifiProfilesForm.tsx` の SSID 候補から localStorage 履歴を削除、
  デバイス側 scan_wifi 結果のみを表示 (Serial 接続時のみ)。

## 検証

`python -m py_compile src/hapbeat_helper/server.py` で構文 OK。
動作検証は次セッションで実機 OTA 失敗ケースを再現できれば確認したい。

## アクション

- [ ] Helper 側の動作確認 (再現 or 既存ケースでメッセージを見る)
- [ ] 完了後、本ファイルを `instructions/completed/` に移動
