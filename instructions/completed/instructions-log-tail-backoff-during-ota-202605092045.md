# log_tail を OTA 中は再接続させない

**作成日:** 2026-05-09
**起点:** workspace device-firmware セッション (v0.0.35)
**優先度:** 高 (OTA 失敗の主因の一つ)

## 背景

ユーザー報告 (2026-05-09 02:38):

```
log tail (192.168.0.147): [WinError 10054] 既存の接続はリモート ホストに強制的に切断されました。
log tail (192.168.0.147) auto-restart
(2 秒ごとに繰り返し)
```

device 側で OTA が走ると `tcp_server` が `s_log_stream = false` にして
log_tail コネクションを切る (これは正常動作)。helper はそれを 2 秒ごとに
再接続しに行く → device 側は accept→reject (newClient.stop()) を毎回繰り返す。

device firmware v0.0.35 で `tcpServerUpdate()` が OTA 中は
`s_server.available()` を呼ばないように短絡し、accept storm の影響は
受けにくくなった (helper 側の SYN は lwIP backlog に積もるが処理されない)。
ただし helper 側でも、OTA 中は log_tail を黙らせるのが筋が良い:

- 毎 2s の auto-restart は無駄
- 失敗ログ (10054) で UI が荒れる
- helper の再接続ループが何かのタイミングで device の OTA RX socket と
  競合する可能性をゼロにできない

## 期待動作

helper 内部で OTA 進行中フラグを持ち、OTA 開始 → 完了まで log_tail の
auto-restart を抑止する。

具体的には:

1. `deploy_ota` (or whatever the OTA orchestration entry is) が走り始めたら
   対象 IP を「OTA 中」として記録
2. log_tail thread の auto-restart ループで、対象 IP が OTA 中なら sleep して
   再接続を試みない
3. OTA 完了 (成功 / エラー / abort いずれも) で OTA 中フラグを解除し、
   log_tail を 1 回だけ再接続させる (切れていた間の接続を回復)

## 実装ヒント

- `src/hapbeat_helper/server.py` あたりで OTA タスクと log_tail タスクが
  どう分かれているか確認
- グローバルまたは DeviceState に `ota_in_progress: set[str]` (target IP の集合)
  を持たせる
- log_tail の `auto-restart` ロジック内で `if ip in ota_in_progress: skip`

## 検証

1. helper を起動、Studio から OTA を実行
2. helper のログに `log tail ... auto-restart` および `WinError 10054` が
   流れない (静か) ことを確認
3. OTA 完了後、log_tail が 1 回だけ自然に再接続することを確認

## 関連

- device firmware v0.0.35: `tcp_server.cpp` の `tcpServerUpdate()` で
  `s_ota_active` 中は `s_server.available()` を呼ばない短絡を追加
- `instructions/instructions-ota-send-timeout-202605091830.md` (既存) と
  併せて helper 側 OTA 経路を堅牢化
