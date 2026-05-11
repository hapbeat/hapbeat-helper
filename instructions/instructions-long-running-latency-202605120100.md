# Instructions: 長時間稼働後の Studio → Helper → Hapbeat 経路の遅延を調査

**発行日:** 2026-05-12
**起票:** devtools-site getting-started 更新セッション
**優先度:** 高（ユーザー体験の根幹を毀損するため。Studio がリリース前に直したい）

## 症状

Helper を **数時間〜半日以上** 起動し続けると、Studio → Helper → Hapbeat の再生経路で **秒単位の遅延** が発生する。

- 再現条件: Helper を長時間 (時間オーダー) 起動しっぱなしにする
- 観測: Studio で波形を再生してから振動するまで 0.5〜数秒遅れる、または振動が来ないことがある
- 復旧: Helper を再起動すると即座に正常 (体感ゼロ遅延) に戻る
- Hapbeat デバイス自体は別アプリ (Unity SDK 等) からの UDP 直接送信で正常に動作するので、デバイス側ではなく **Helper の経路** の問題

## 想定される原因 (要調査)

1. **mDNS / UDP socket リーク** — long-running で OS リソースが詰まり、send 経路に遅延
2. **イベントループ詰まり** — asyncio task の累積 / supervisor の slow drain
3. **TCP 接続ハンドル** — `_send_tcp_to_many` の per-IP lock が古いハンドルで stuck
4. **ガベージコレクション** — 長時間動作で割り当て増加、stop-the-world で時々詰まる
5. **WS 経由 broadcast の buffer 滞留**

## デバッグ着手点

- Helper 起動直後と数時間後で `_send_tcp_to_many` / UDP send の wall-time を比較 (簡易 instrument を仕込む)
- Helper のメモリ使用量と open file descriptor 数を `psutil` で経時記録
- `asyncio.all_tasks()` の数の経時推移
- 既存の per-IP `asyncio.Lock` が解放されない経路がないか確認

## 暫定対応 (ドキュメント側、本セッションで完了済み)

- `hapbeat-devtools-site/docs/start-here/getting-started.md` の Step 4 に `:::caution` ブロックを追加。
  「正常な再生は即時。遅延が出たら Helper を再起動。これは Helper の既知の問題で修正予定」と明記済み。
- Studio または Helper 側で `helper_uptime > N 時間` を検出して UI に注意喚起を出す手も検討余地あり (本 instruction の scope 外)。

## 完了条件

- 原因の特定 (どのリソースが drain しているか)
- 修正 (定期的な再初期化 / リソース解放 / GC 強制発火など根治策)
- 24 時間連続稼働でも初回と同等のレイテンシで動くことを実機検証
- getting-started の `:::caution` を「修正済み」表現に書き換える (or 削除)

## 参考

- Helper 主要送信経路: `_send_tcp_to_many` / UDP broadcast / WS broadcast handlers
- 過去の安定化 (v0.2.1〜v0.2.3): per-IP asyncio.Lock 化 / WS-close 時 log_tail cleanup / supervisor。今回の症状はそれらでカバーしきれていない領域
