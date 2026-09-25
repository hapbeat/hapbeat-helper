# Changelog

このファイルの形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に、
バージョニングは [Semantic Versioning](https://semver.org/lang/ja/) に従う。

## [Unreleased]

### Added

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

### Fixed

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

- pytest 100 件通過（STREAM_BEGIN address target layout を含む）。実機未検証。
- **Hapbeat 実機（duo_wl_v3 / fw 0.3.1）で確認済み**: PING が全サブネット宛てに
  ファンアウトし、実機の PONG でそのサブネットに確定、`send_raw` の `<broadcast>` は
  単一宛先のまま。稼働中のデーモンでも
  `broadcasting to 192.168.0.255 (a device answered from 192.168.0.142)` を確認した。

### Added

- 起動時に新しい版が出ているか確認し、あれば 1 行だけ知らせるようになった
  （起動ごと。閉じる操作を要求しない 1 行なので抑制はしない）。
  `hapbeat-helper version` でもいつでも確認できる。
  無効化は `--no-update-check` または `HAPBEAT_NO_UPDATE_CHECK=1`。
  取得は 3 秒でタイムアウトし、失敗しても何も出さない（オフライン運用のため）。
- BandWL v4 PWM 実験ファーム向けに `set_pwm_bias` / `pwm_tone` / `set_volume` /
  `pwm_status` / `pwm_probe` を中継し、`get_info` の `haptic_pwm` を透過するようにした。

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

[Unreleased]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.1.2...v0.1.3
