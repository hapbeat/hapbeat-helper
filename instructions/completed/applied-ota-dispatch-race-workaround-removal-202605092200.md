---
起票: hapbeat-device-firmware セッション 2026-05-09
関連 DEC: なし
---

# 事後承認 note: OTA dispatch race workaround 削除 (v0.2.9)

## 変更ファイル

- `src/hapbeat_helper/server.py` — `time.sleep(0.1)` とその説明コメントを削除 (line 1423–1436)
- `pyproject.toml` — version `0.2.8` → `0.2.9`

## 背景

`hapbeat-device-firmware` で `tcpServerUpdate()` の JSON dispatch while ループに
binary mode 遷移後の早期 return を追加 (firmware v0.1.2)。これにより
`cmdOtaBegin` → `s_ota_active = true` の直後に helper の chunk が lwIP RX バッファ
に届いても JSON として誤食されなくなった。

helper 側の `time.sleep(0.1)` は firmware 修正が入るまでの暫定 workaround だったため
削除。firmware v0.1.2 以降のデバイスで OTA を行う場合は不要。

## 検証状況

- firmware ビルド: `necklace_v3` SUCCESS (v0.1.2)
- helper 側: 構文確認のみ。実機 OTA は次セッションで検証予定

## hapbeat-helper エージェントへのアクション

- 内容を確認したら `instructions/completed/` へ移動
- 追加対応: 実機 OTA 10 回連続テストで workaround 削除後も成功すること確認
