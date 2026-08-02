# Changelog

このファイルの形式は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/) に、
バージョニングは [Semantic Versioning](https://semver.org/lang/ja/) に従う。

## [Unreleased]

### Added

- 起動時に新しい版が出ているか確認し、あれば 1 行だけ知らせるようになった。
  同じ版について 2 回目は表示しない（版を固定して使っている間、毎回同じ行を
  見せないため）。`hapbeat-helper version` はいつでも最新版を表示する。
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

[Unreleased]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.1.4...v0.2.0
[0.1.4]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/Hapbeat/hapbeat-helper/compare/v0.1.2...v0.1.3
