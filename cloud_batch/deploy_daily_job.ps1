param(
  [string]$ProjectId = "rugged-destiny-408613",
  [string]$Region = "asia-northeast1",
  [string]$JobName = "senior-reading-daily-metadata",
  [string]$SchedulerName = "senior-reading-daily-metadata-schedule",
  [string]$ServiceAccountName = "senior-reading-batch",
  [string]$SecretName = "senior-reading-youtube-api-key",
  [string]$Schedule = "0 3 * * *",
  [string]$TimeZone = "Asia/Tokyo",
  [string]$YoutubeApiKey = ""
)

$ErrorActionPreference = "Stop"

$serviceAccountEmail = "$ServiceAccountName@$ProjectId.iam.gserviceaccount.com"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

gcloud config set project $ProjectId | Out-Null

gcloud services enable `
  run.googleapis.com `
  cloudbuild.googleapis.com `
  artifactregistry.googleapis.com `
  cloudscheduler.googleapis.com `
  secretmanager.googleapis.com `
  bigquery.googleapis.com `
  --project $ProjectId | Out-Null

$existingSa = gcloud iam service-accounts list `
  --project $ProjectId `
  --filter "email=$serviceAccountEmail" `
  --format "value(email)"
if (-not $existingSa) {
  gcloud iam service-accounts create $ServiceAccountName `
    --project $ProjectId `
    --display-name "Senior Reading Batch" | Out-Null
}

foreach ($role in @("roles/bigquery.dataEditor", "roles/bigquery.jobUser", "roles/secretmanager.secretAccessor", "roles/run.developer", "roles/storage.objectAdmin")) {
  gcloud projects add-iam-policy-binding $ProjectId `
    --member "serviceAccount:$serviceAccountEmail" `
    --role $role `
    --quiet | Out-Null
}

$existingSecret = gcloud secrets list `
  --project $ProjectId `
  --filter "name:$SecretName" `
  --format "value(name)"
if (-not $existingSecret) {
  gcloud secrets create $SecretName `
    --project $ProjectId `
    --replication-policy automatic | Out-Null
}
if ($YoutubeApiKey) {
  $tmp = New-TemporaryFile
  try {
    Set-Content -LiteralPath $tmp -Value $YoutubeApiKey -NoNewline -Encoding ascii
    gcloud secrets versions add $SecretName `
      --project $ProjectId `
      --data-file $tmp | Out-Null
  } finally {
    Remove-Item -LiteralPath $tmp -Force
  }
}

Push-Location $scriptDir
try {
  gcloud run jobs deploy $JobName `
    --project $ProjectId `
    --region $Region `
    --source "." `
    --service-account $serviceAccountEmail `
    --set-env-vars "PROJECT_ID=$ProjectId,BQ_DATASET=senior_reading_all,LIMIT=0,SLEEP_SEC=0.1,DOWNLOAD_THUMBNAILS=1,DISCOVER_RECENT_UPLOADS=1,DISCOVERY_UPLOADS_PER_CHANNEL=20,THUMBNAIL_BUCKET=senior-share-staging-570862915709,THUMBNAIL_PREFIX=senior_reading_thumbnails" `
    --set-secrets "YOUTUBE_API_KEY=${SecretName}:latest" `
    --max-retries 1 `
    --tasks 1 | Out-Null
} finally {
  Pop-Location
}

$runUri = "https://$Region-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$ProjectId/jobs/$JobName`:run"
$existingScheduler = gcloud scheduler jobs list `
  --project $ProjectId `
  --location $Region `
  --filter "name:$SchedulerName" `
  --format "value(name)"
if ($existingScheduler) {
  gcloud scheduler jobs update http $SchedulerName `
    --project $ProjectId `
    --location $Region `
    --schedule $Schedule `
    --time-zone $TimeZone `
    --uri $runUri `
    --http-method POST `
    --oauth-service-account-email $serviceAccountEmail | Out-Null
} else {
  gcloud scheduler jobs create http $SchedulerName `
    --project $ProjectId `
    --location $Region `
    --schedule $Schedule `
    --time-zone $TimeZone `
    --uri $runUri `
    --http-method POST `
    --oauth-service-account-email $serviceAccountEmail | Out-Null
}

Write-Output (@{
  project = $ProjectId
  region = $Region
  job = $JobName
  scheduler = $SchedulerName
  schedule = $Schedule
  time_zone = $TimeZone
  service_account = $serviceAccountEmail
} | ConvertTo-Json -Compress)
