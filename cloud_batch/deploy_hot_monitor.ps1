param(
  [string]$ProjectId = "rugged-destiny-408613",
  [string]$Region = "asia-northeast1",
  [string]$SourceJobName = "senior-reading-daily-metadata",
  [string]$JobName = "senior-reading-hot-availability",
  [string]$SchedulerName = "senior-reading-hot-availability-schedule",
  [string]$ServiceAccountName = "senior-reading-batch",
  [string]$SecretName = "senior-reading-youtube-api-key",
  [string]$TargetChannelIds = "UCg3LNJsGAVWqv5nlOddBnHA",
  [string]$Schedule = "15 */3 * * *",
  [string]$TimeZone = "Asia/Tokyo"
)

$ErrorActionPreference = "Stop"
$serviceAccountEmail = "$ServiceAccountName@$ProjectId.iam.gserviceaccount.com"

$image = gcloud run jobs describe $SourceJobName `
  --project $ProjectId `
  --region $Region `
  --format "value(spec.template.spec.template.spec.containers[0].image)"
if (-not $image) {
  throw "Source Cloud Run Job image was not found: $SourceJobName"
}

gcloud run jobs deploy $JobName `
  --project $ProjectId `
  --region $Region `
  --image $image `
  --service-account $serviceAccountEmail `
  --set-env-vars "PROJECT_ID=$ProjectId,BQ_DATASET=senior_reading_all,LIMIT=0,SLEEP_SEC=0.1,DOWNLOAD_THUMBNAILS=1,DISCOVER_RECENT_UPLOADS=1,DISCOVERY_UPLOADS_PER_CHANNEL=20,TARGET_CHANNEL_IDS=$TargetChannelIds,AVAILABILITY_CONFIRM_MISSES=2,THUMBNAIL_BUCKET=senior-share-staging-570862915709,THUMBNAIL_PREFIX=senior_reading_thumbnails" `
  --set-secrets "YOUTUBE_API_KEY_PRIMARY=${SecretName}:latest,YOUTUBE_API_KEY_FALLBACK=naresome-youtube-api-key:latest" `
  --max-retries 1 `
  --tasks 1 `
  --quiet | Out-Null

$runUri = "https://$Region-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$ProjectId/jobs/$JobName`:run"
$existing = gcloud scheduler jobs list `
  --project $ProjectId `
  --location $Region `
  --filter "name:$SchedulerName" `
  --format "value(name)"
if ($existing) {
  gcloud scheduler jobs update http $SchedulerName `
    --project $ProjectId `
    --location $Region `
    --schedule $Schedule `
    --time-zone $TimeZone `
    --uri $runUri `
    --http-method POST `
    --oauth-service-account-email $serviceAccountEmail `
    --quiet | Out-Null
} else {
  gcloud scheduler jobs create http $SchedulerName `
    --project $ProjectId `
    --location $Region `
    --schedule $Schedule `
    --time-zone $TimeZone `
    --uri $runUri `
    --http-method POST `
    --oauth-service-account-email $serviceAccountEmail `
    --quiet | Out-Null
}

@{
  project = $ProjectId
  job = $JobName
  scheduler = $SchedulerName
  target_channel_ids = $TargetChannelIds
  schedule = $Schedule
  image = $image
} | ConvertTo-Json -Compress
