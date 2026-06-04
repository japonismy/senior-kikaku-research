# 企画リサーチ

Created: 2026-06-05

既存の `サムネ参考_純感動12ch` は変更せず、横断検索用に新設した静的HTMLポータル。

## 初期検索対象

- サムネ文字
- タイトル
- タグ

文字起こし/台本は検索対象にしない。詳細画面の冒頭ダイジェストとダウンロード用に使う。

## 絞り込み・並び替え

- チャンネル
- 視聴回数
- 公開日以降
- サムネ文字未整備のみ
- 視聴回数順
- 公開日順
- 関連度順

## データ元

- `analysis/competitor_db.sqlite`
- `videos`
- `channels`
- `transcripts`
- `thumbnail_ocr`
- `thumbnail_axis_tags`

## 対象条件

- `channels.sync_target = senior_reading`
- `channels.include = 1`
- `channels.source_type != original_kr`
- `videos.duration_sec >= 120` または長さ不明

`genre_tag` の `短尺_...` はチャンネル分類として使われているため、ショート除外条件には使わない。

## 生成物

- `index.html`
- `data/videos.js`
- `data/transcripts_light.js`
- `reports/thumbnail_text_missing.csv`
- `reports/build_summary.json`
- `thumbnail_text_overrides.csv`

## 詳細画面

- 冒頭ダイジェスト表示
- CSV DL
- サムネDL(maxresdefault優先、存在しない場合はフォールバック)
- YouTubeリンク
- 公開日
- 更新日
- サムネ分析

DLファイル名:

```text
{video_id}_{タイトル先頭20文字}.csv
{video_id}_{タイトル先頭20文字}_thumbnail.jpg
```

## 保守バッチ

上位100件の高解像度サムネ取得、Geminiサムネ分析、データ再生成:

```powershell
python run_maintenance_batch.py --limit 100
```

YouTubeメタデータ更新も含める場合:

```powershell
python run_maintenance_batch.py --limit 100 --update-youtube
```

GitHub Pagesへ反映する場合:

```powershell
python run_maintenance_batch.py --limit 100 --deploy
```

既存データだけ再生成・pushする場合:

```powershell
python deploy_pages.py
```

## サムネ文字の補正

現時点の `thumbnail_ocr` はほぼ未整備のため、サムネ文字検索は器だけ先に作り、欠損一覧を補正対象として出力する。

`thumbnail_text_overrides.csv` に以下の形式で追記してから `generate_portal_data.py` を再実行する。

```csv
video_id,thumbnail_text,note
XXXXXXXXXXX,双子の少女を住み込みで雇った結果,manual
```
