# Changelog

このファイルの形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に、
バージョニングは [Semantic Versioning](https://semver.org/lang/ja/) に従う。

## [Unreleased]

### Added

- ストリームを別のストリームに奪われたクライアントへ `stream_displaced`
  （`{"stream_id", "targets": [ip…], "by": <新しい stream_id>, "same_client": bool}`）を送る。
  どのデバイスも持っていないストリームの DATA / END には、そのストリームにつき 1 回だけ
  `stream_ack` の `status: "no_session"` を返す。
- ストリームの開始・終了・奪取をログに出し、`health:` 行に持ち主のいない DATA / END の数
  （`stream_orphans`）を加えた。イベントループが 2 秒以上止まったら、その時点のループの
  スタックをログに出す（最短 1 分間隔）。

### Fixed

- Windows の自動起動タスクが優先度「通常未満」で動き、PC が重い時にイベントループが
  数秒〜20 秒以上止まっていた。`start` は通常未満で起動されたら自分の優先度を通常に上げ、
  `install-service` はタスクを優先度 4（通常）・異常終了時の再起動つきで登録する。
- stream-v2 のデバイスへの STREAM_END を 100 ms 間隔で計 3 回送る（1 パケットの喪失で
  デバイスが再生中のまま残っていた）。旧ファームには遅れた END を拒めないため 1 回のまま。
- STREAM_END / DATA の宛先を、payload に明示の `targets` / `ip` が無い時はそのストリームが
  持つ全デバイスにした（アドレス文字列の `target` だけの時に END が届いていなかった）。
- 待っている間にリースを失ったデバイスが、`stream_ack` の `targets` に開始済みとして
  入っていた。`deferred` に入れる。
- 探索 PING（ネットワークアダプタの列挙を含む）をイベントループの外で送る。

## [0.5.0] - 2026-10-07

### Added

- 長時間稼働の診断用に、10 分ごとの `health:` 行（接続数・ストリーム数・デバイス数・
  PONG 数・機器ごとのストリーム方式・log_tail スレッド数・スレッド数・タスク数・
  イベントループ遅延の最大値）と、イベントループが 250 ms 以上止まった時の警告
  （最短 1 分間隔）をログに出す。

- **素材台帳（`hapbeat-helper materials`）を追加した**。フリー素材サイトからダウンロードした
  音声ファイルの出典（配布サイト・元ページ URL・ライセンス）を、ファイル内容の SHA-256 で
  紐づけて記録する。`materials ingest` は Downloads（既定、`--dir` で変更）の音声と zip 内の
  音声を `~/HapbeatMaterials/store/` へコピーし（元ファイルは残す）、Windows の
  `Zone.Identifier` から元ページを読む。ライセンスは `sites.json`（サイト規則）と個別上書きから
  毎回解決する。`list` / `show` / `set-license` / `credits`（CREDITS.md 生成）/ `where` を追加。
  daemon に WS `material_lookup` / `material_register_derived` / `material_credits` を追加した
  （既存メッセージの挙動は変更なし）。config `materials_watch_downloads = true` の時だけ daemon が
  Downloads を 10 秒ごとに見て新規ファイルを自動取り込みする（既定 off）。

- **`hapbeat-helper mcp`（AI エージェント向け MCP サーバー、stdio）を追加した**（DEC-078）。
  Claude Code / Codex から Studio の AI 試行（ガイド・知識ベースの参照、試行の投稿、
  試聴、採用、知見の提案、評価待ち）をツールで操作できる。各ツールは稼働中の daemon
  経由で、エディタでフォルダを開いている Studio タブへ中継され、処理は Studio が行う。
  optional extra `hapbeat-helper[mcp]`（`mcp>=1.2`、SDK 1.x / 2.x の両方に対応）が必要。
  daemon には中継メッセージ `agent_endpoint_register` / `agent_endpoint_unregister` /
  `agent_request` / `agent_response` を追加した（既存メッセージの挙動は変更なし）。

- **Studio からのストリーム再生が stream session v2 のファームに対応した**（DEC-074/075）。
  PING を 16 バイト（client incarnation 付き）にし、応答の HBS2 tail で機器ごとに判定する。
  v2 ファームには boot id / lease ticket / generation 付きのパケットを待ちなしで送り、
  v2 に対応していない旧ファームには従来の v1 形式と 300 ms の END→BEGIN 待ちで送る
  （旧ファームの利用者に影響しない）。未判定の機器は開始時に PING して最大 0.4 秒待ち、
  応答が無ければ送らない（`stream_ack` の `deferred`）。別アプリに受信許可を取られた
  機器は、ユーザーが再生を始めた時だけ新しい incarnation で取り直す。

- **`hapbeat-helper ota <target> <bin>`** を追加した。dev ビルドのファームを Studio を
  開かずに CLI から流せる。`<target>` は IP またはデバイス名（名前の解決には稼働中の
  helper が要る）。helper が動いていれば WS 経由（per-IP ロック・OTA 排他と協調）、
  動いていなければデバイスへ直接ストリームする。送信前に app image を検証し、
  merged image (`firmware_full_serial.bin`) は弾く。終了コードは 0 成功 / 1 OTA 失敗 /
  2 引数・宛先エラー。
- **PLAY (0x01) に `pan` を追加した**（DEC-055、-1.0 左 / 0.0 中央 / +1.0 右）。
  `preview_event` の payload に任意フィールド `pan`（既定 0.0）を受け、送信時は
  `[-1, 1]` にクランプして常に付与する。STOP / STOP_ALL は変更なし。
- BandWL v4 PWM 実験ファーム向けに `set_pwm_bias` / `pwm_tone` / `set_volume` /
  `pwm_status` / `pwm_probe` を中継し、`get_info` の `haptic_pwm` を透過するようにした。

### Fixed

- **旧ファームの機器でループ再生すると、2 周目以降が半分ほど欠ける問題**を修正した
  （この版の stream v2 対応で入った退行）。旧ファーム向けの 300 ms の END→BEGIN 待ちを
  WS の受信処理ごと止めて実装していたため、待ちの間は同じ接続のメッセージ（v2 機器宛ての
  DATA も含む）がすべて止まり、明けた瞬間に溜まった DATA を一斉に送っていた。旧ファーム
  （0.4.0）はこれを取りこぼし、END 直後に再開した 2 本目は 32 パケット中 16〜17 しか
  届かなかった。待ちの間はその旧ファーム機器宛てのパケットだけを溜め、BEGIN 後に到着間隔
  どおり送る（その機器は最大 0.3 秒遅れて鳴る）。v2 機器と他の機器は待たない。
- **ログ購読（`subscribe_logs`）の解除直後に再購読すると、止められない log_tail スレッドが
  残る問題**を修正した。止めた側のスレッドが終了時に、後から作られたスレッドの登録まで
  消していた。残ったスレッドは Studio を再読み込みしても止まらず、デバイスの単一 TCP 枠を
  後続の log_tail や Studio のコマンドと奪い合い続け、helper を再起動するまで消えなかった。
  終了時は自分の登録だけを消す。再接続待ち（1〜4 秒）も解除で即座に終わる。
- PONG の `volume_wiper` の次のバイトを `volume_steps` として読んでいたのを削除した。
  そのバイトを送るファームは無く、v2 ファームでは lease tail の先頭 `H`(72) を
  段数と誤読していた。段数は従来どおり `get_info` から取る。
- `python -m hapbeat_helper` が終了コードを捨てていたのを修正した（常に 0 を返していた）。
- Windows の日本語コンソール (cp932) で、`—` を含むメッセージを出力するとコマンドごと
  `UnicodeEncodeError` で落ちていたのを修正した。表示できない文字だけを置換する。

- **マルチホーム PC で、mDNS が使えないときにデバイスを発見できない問題**を修正した。
  ブロードキャスト PING の宛先が `255.255.255.255`（limited broadcast）だったが、これは
  **インターフェイスメトリックが最小の 1 本からしか送出されない**。Hyper-V / WSL2 /
  Docker を入れると作られる仮想アダプタは **LAN ケーブルを繋いでいなくても常時
  Connected** で、Windows の既定で Wi-Fi より優先されることがある。この場合パケットは
  Hapbeat のいるネットワークに届かない。
  helper はふだん mDNS で見つけてユニキャストするため表面化しにくいが、
  **ブロードキャスト PING はまさに mDNS が使えない場合のフォールバック**なので、
  同じ穴が空いていた。
  - PING を**ローカルの各サブネット宛て**へ送るようにした。宛先は各 NIC の実プレフィックス
    から算出する（/16 は `x.y.255.255`、/25 は `x.y.z.127` になるため `.255` 決め打ちには
    できない）。同一サブネットに NIC が 2 枚ある場合は宛先で重複排除する。
  - **PONG が返ったサブネットに確定**し、以後のブロードキャストをそこへ向ける。
    設定項目は増やしていない。
  - **確定は外れる**（contracts message-format.md §2.1）。ブロードキャスト PING の送信時
    （1 秒に 1 回まで）にインターフェイスを列挙し直し、確定したサブネットが無くなった場合、
    または device TTL（レジストリが offline と判定する 8 秒）を超えてそのサブネットから PONG が
    無い場合に確定を外して全サブネットへ探索し直す。以前は確定が helper の再起動まで続いた
    ため、PC の Wi-Fi を別のネットワークへ切り替えると古いサブネットへ PING を送り続け、
    helper を再起動するまで Studio に新しいネットワークの機器が出なかった。
  - `255.255.255.255` は catch-all として常に候補に残す。SoftAP 構成、インターフェイスを
    列挙できないホスト、単一 NIC 環境は従来どおり動く。
  - **PLAY / STOP のブロードキャストは従来どおり単一宛先**（`send_raw`）。ファームウェア
    v0.3.0 未満は seq 重複排除を持たないため、複数経路へ送ると触覚が 2 回鳴る。
    探索系と再生系で送信経路を分けている。

### Changed

- Browser SDK の endpoint-scoped multi-stream 用に、`stream_begin` の
  `payload.target`（完全 device address）を UDP `STREAM_BEGIN` payload へ
  透過するようにした。BEGIN/DATA/END の `payload.ip` と組み合わせ、複数
  endpoint の同時 session を broadcast せず分離する。
- `ifaddr` を依存関係に明示した。ローカル NIC のネットマスク取得に直接使うため
  （`zeroconf` の依存として以前から入っていたが、それに依存し続ける前提は置けない）。

### 検証

- pytest 196 件通過。
- Hapbeat 実機（duo_wl_v3 fw 0.5.0d10 = stream v2、duo_wl_v3 fw 0.4.0 = 旧ファーム）で、
  単独・同時・同一ストリームへの両方宛て・END 直後の再開のいずれも、送信した 32 パケットが
  両機器に届くこと（`get_stream_debug` の packets / session_v2）を確認した。v2 機器へは
  60 秒ごとの試聴を約 3.5 時間続け、欠落・拒否・アンダーラン 0。`hapbeat-helper ota` で
  fw 0.5.0d10 を書き込めることを確認した。
- duo_wl_v3 / fw 0.3.1 で、PING が全サブネット宛てにファンアウトし、実機の PONG でその
  サブネットに確定、`send_raw` の `<broadcast>` は単一宛先のままであることを確認した。

## [0.4.0] - 2026-08-03

### Added

- 起動時に新しい版が出ているか確認し、あれば 1 行だけ知らせるようになった
  （起動ごと。閉じる操作を要求しない 1 行なので抑制はしない）。
  `hapbeat-helper version` でもいつでも確認できる。
  無効化は `--no-update-check` または `HAPBEAT_NO_UPDATE_CHECK=1`。
  取得は 3 秒でタイムアウトし、失敗しても何も出さない（オフライン運用のため）。

### Changed

- デバイス未選択時の PLAY / STOP も unicast に揃えた。

## [0.3.1] - 2026-08-01

### Changed

- Kit 転送チャンクの送信タイムアウトを 10 秒 → 3 秒に短縮（実測ベース）。

## [0.3.0] - 2026-07-22

### Added

- DuoWL v4 の音声 DSP 系 WS コマンドをデバイスへ中継（`set_*` の persist も透過）。
- DEC-041 のオーディオ設定 3 コマンド + `set_stream_buffer` + `get_info` の audio 透過。
- `set_input_mode`（DuoWL v4 のライン入力 / 出力切替）の中継。

## [0.2.0] - 2026-07-02

### Added

- `preview_event` / `stop` を IP 指定で unicast できるようにした（選択デバイスのみ再生）。

### Fixed

- Windows の ICMP reset (10054) で UDP 受信スレッドが死に、デバイスを全ロストする問題を根治。

## [0.1.4] - 2026-06-17

### Fixed

- 長時間稼働で徐々に遅くなる問題を根治（`_pending_pings` のリーク、高頻度 read poll での
  stuck-slot recovery スキップ）。
- Ctrl+C でのシャットダウンがハングする問題、終了時の ProactorEventLoop ノイズを解消。
- コマンド実行中の `log_tail` 再接続を抑止（TCP スロットの ping-pong を根治）。
- offline 判定の閾値を 5 秒 → 8 秒に変更（デバイス一覧の点滅を解消）。
- クライアントが処理中に切断した場合の `ConnectionClosed` を捕捉。

### Added

- OTA をバックグラウンドタスク化し、接続をブロックしないようにした。
  同一 IP への並行 OTA は fail-fast で弾く。
- editable / ソース実行時は git から版を算出（`dN` 追従）。

## [0.1.3] - 2026-05-26

### Changed

- Kit manifest schema 2.0.0 (DEC-031) 追従。
- `__version__` をパッケージメタデータから取得（ハードコードのフォールバックを撤廃）。
- `get_info` の build 転送、kit manifest のファイル名規約に追従。

## 0.1.2 以前

[GitHub Releases](https://github.com/Hapbeat/hapbeat-helper/releases) を参照。

[Unreleased]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.1.2...v0.1.3
