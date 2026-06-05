# BQ Daily Metadata Batch

シニア朗読の競合動画メタデータを、ローカルSQLiteを介さずBigQueryへ直接更新するCloud Run Job。

## 方針

- 実行間隔: 1日1回
- 正本: BigQuery `rugged-destiny-408613.senior_reading_all`
- 対象: `sync_target = senior_reading`、`include = true`、韓国元動画除外、ショート除外
- 更新内容: title / published_at / view_count / like_count / comment_count / thumbnail_url / duration / fetched_at
- サムネ: GCS `gs://senior-share-staging-570862915709/senior_reading_thumbnails/{video_id}.jpg` へ保存
- 実行ログ: `youtube_metadata_update_runs`
- サムネ保存ログ: `thumbnail_assets`

## 運用

- 毎日03:00 JST: YouTubeメタデータ更新 + サムネDL
- サムネDLは、既に保存済みで `source_url` が変わっていない動画はスキップする
- Gemini OCR/構図分析は定期自動実行しない
- Gemini対象は `research_channel_scopes.scope = 'jun_kando_12'` の純感動12chに絞る

週次または手動Gemini対象の確認SQL:

```sql
SELECT
  v.video_id,
  c.channel_name,
  v.title,
  v.view_count,
  a.gcs_uri
FROM `rugged-destiny-408613.senior_reading_all.research_channel_scopes` s
JOIN `rugged-destiny-408613.senior_reading_all.analysis_competitor_db__channels` c
  USING(channel_id)
JOIN `rugged-destiny-408613.senior_reading_all.analysis_competitor_db__videos` v
  USING(channel_id)
JOIN `rugged-destiny-408613.senior_reading_all.thumbnail_assets` a
  USING(video_id)
LEFT JOIN `rugged-destiny-408613.senior_reading_all.analysis_competitor_db__thumbnail_ocr` o
  USING(video_id)
WHERE s.scope = 'jun_kando_12'
  AND s.is_kando_research_target
  AND a.error = ''
  AND (o.combined_text IS NULL OR o.combined_text = '')
ORDER BY v.view_count DESC;
```

## 環境変数

- `PROJECT_ID`: 既定 `rugged-destiny-408613`
- `BQ_DATASET`: 既定 `senior_reading_all`
- `YOUTUBE_API_KEY` または `YOUTUBE_API_KEYS`
- `DOWNLOAD_THUMBNAILS`: `1` ならサムネDLも実行
- `THUMBNAIL_BUCKET`: 既定 `senior-share-staging-570862915709`
- `THUMBNAIL_PREFIX`: 既定 `senior_reading_thumbnails`
- `LIMIT`: テスト用。`0` なら全件

## デプロイ概要

1. Secret ManagerにYouTube APIキーを保存する
2. Artifact RegistryへDocker imageをpushする
3. Cloud Run Jobを作成する
4. Cloud Schedulerで毎日1回起動する

Cloud Run Jobのサービスアカウントには、少なくとも以下を付与する。

- `roles/bigquery.dataEditor`
- `roles/bigquery.jobUser`
- `roles/storage.objectAdmin`
- Secretを読む場合は `roles/secretmanager.secretAccessor`
