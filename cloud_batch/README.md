# BQ Daily Metadata Batch

シニア朗読の競合動画メタデータを、ローカルSQLiteを介さずBigQueryへ直接更新するCloud Run Job。

## 方針

- 実行間隔: 1日1回
- 正本: BigQuery `rugged-destiny-408613.senior_reading_all`
- 対象: `sync_target = senior_reading`、`include = true`、韓国元動画除外、ショート除外
- 更新内容: title / published_at / view_count / like_count / comment_count / thumbnail_url / duration / fetched_at
- 実行ログ: `youtube_metadata_update_runs`

## 環境変数

- `PROJECT_ID`: 既定 `rugged-destiny-408613`
- `BQ_DATASET`: 既定 `senior_reading_all`
- `YOUTUBE_API_KEY` または `YOUTUBE_API_KEYS`
- `LIMIT`: テスト用。`0` なら全件

## デプロイ概要

1. Secret ManagerにYouTube APIキーを保存する
2. Artifact RegistryへDocker imageをpushする
3. Cloud Run Jobを作成する
4. Cloud Schedulerで毎日1回起動する

Cloud Run Jobのサービスアカウントには、少なくとも以下を付与する。

- `roles/bigquery.dataEditor`
- `roles/bigquery.jobUser`
- Secretを読む場合は `roles/secretmanager.secretAccessor`
