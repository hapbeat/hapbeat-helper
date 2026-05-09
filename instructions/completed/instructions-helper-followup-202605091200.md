# Instructions: hapbeat-helper の次回修正 (内容未確定)

**発行日:** 2026-05-09
**起票:** workspace session 2026-05-09 (Studio + Helper Onboarding 大規模整理)
**優先度:** 即着手

## 背景

ユーザより「次回は helper の修正を行います」と申し送り。具体的な修正項目は次セッションでユーザから指示を受ける。

本セッションで helper には既に以下を入れた (commit bca9147):
- OTA 中の log_tail auto-restart 抑止 (`_ota_in_progress` set + supervisor 待機ループ)
- `TcpRawConnection.send_raw` に timeout オプション追加、OTA で 10s 指定

このため次回の修正対象は **上記以外の helper 既知課題** または **新規ユーザ要望** が主たる候補。

## タスク

1. begin-session で helper を作業対象に指定
2. 既存 instructions を確認:
   - `hapbeat-helper/instructions/*.md` (新規バックログ)
   - `hapbeat-helper/instructions/applied/*.md` (事後承認待ち)
3. ユーザ指示を待ってから着手

## 完了条件

- [ ] ユーザ指示を反映した修正を実装
- [ ] typecheck / build / 動作確認 (Studio 5173 dev server から helper の挙動を確認)
- [ ] 本ファイルを `instructions/completed/` に移動

## 依存関係

- **Required**: なし
- **Downstream**: 必要に応じて hapbeat-studio 側に追従修正
