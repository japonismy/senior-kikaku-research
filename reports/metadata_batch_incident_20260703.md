# Senior Reading Metadata Batch Incident 2026-07-03

## Summary

The GitHub Pages portal continued to deploy from BigQuery, but the upstream Cloud Run metadata batch stopped ingesting new senior-reading competitor videos after the 2026-06-24 redeploy.

Impact:
- `senior-reading-daily-metadata` returned `target_count=0` on scheduled runs from 2026-06-24 18:00 UTC / 2026-06-25 03:00 JST onward.
- Portal data stayed current as a deployment artifact, but the BigQuery source table had no videos newer than 2026-06-23 before the fix.
- `人生は七色` was stale in BigQuery until the fix; after rerun, it advanced to `2026-07-01T21:00:28Z`.

## Root Cause

The batch supported an optional `TARGET_CHANNEL_IDS` environment variable for targeted reruns.

Before the fix, the SQL always included this condition:

```sql
AND (
  ARRAY_LENGTH(@target_channel_ids) = 0
  OR channel_id IN UNNEST(@target_channel_ids)
)
```

The code passed `TARGET_CHANNEL_IDS=[]` when the env var was not set. In actual BigQuery execution, this empty array parameter did not behave as the intended "all channels" condition for the scheduled job and resulted in zero target rows.

This was a conditional bug:
- When `TARGET_CHANNEL_IDS` was set, targeted manual runs worked.
- When `TARGET_CHANNEL_IDS` was missing, scheduled full runs returned zero targets.

## Why It Started Midway

The bug surfaced after the 2026-06-24 redeploy.

Observed timeline:
- 2026-06-23 scheduled runs were healthy, with about `target_count=3221`.
- 2026-06-24 redeploy created image digest `sha256:7b1029c60b7cca11efcd317bd32ea12881c14ad921fe55d70eaa84d7fe5485f4`.
- A manual execution after redeploy had `TARGET_CHANNEL_IDS` set to 8 channels and produced nonzero targets.
- The scheduled execution on 2026-06-24 18:00 UTC did not include `TARGET_CHANNEL_IDS` and returned `target_count=0`.
- Scheduled executions continued to return `target_count=0` until the 2026-07-03 fix.

This explains why the same program did not fail from day one: the failure mode only appears on full scheduled runs where `TARGET_CHANNEL_IDS` is absent.

## Fix

File:
- `cloud_batch/daily_youtube_metadata_update.py`

Change:
- Do not include the `channel_id IN UNNEST(@target_channel_ids)` SQL filter unless `TARGET_CHANNEL_IDS` is non-empty.
- Do not pass the array query parameter when it is not used.

Verification before deploy:
- Local function check with no `TARGET_CHANNEL_IDS` returned:
  - `video_ids=3378`
  - `channels=43`

Deployment:
- `cloud_batch/deploy_daily_job.ps1`
- Cloud Run job: `senior-reading-daily-metadata`
- Region: `asia-northeast1`

Post-fix execution:
- Execution `senior-reading-daily-metadata-fp7bq`: success
  - `target_count=3452`
  - `discovered_count=667`
  - `updated_count=2021`
  - `missing_count=1400`
- Execution `senior-reading-daily-metadata-v7k7m`: success
  - `target_count=3465`
  - `discovered_count=680`
  - `updated_count=2034`
  - `missing_count=1400`

Portal redeploy:
- `deploy_from_bq.ps1`
- Commit: `a6f443e`
- Portal data after rebuild:
  - `videos=3434`
  - `videos_with_gcs_thumbnail=2094`
  - `transcripts_light=2510`

## Channel Notes

`人生は七色`:
- Channel ID: `UCd8RNaNMmt32HpGAR_POuVQ`
- BigQuery latest after fix: `2026-07-01T21:00:28Z`
- Videos since 2026-06-24 after fix: 6

`人生の糸`:
- Existing registered channel: `UCHFtC-6PV6j7WDcKMsRdTYg`
- BigQuery/latest YouTube check showed latest confirmed upload: `2026-06-19T06:00:21Z`
- Search also found sibling channel `人生の糸～感動する人生の物語～` / `UCB6o9joc57Po5cFy3nlP9oQ`.
- That sibling channel was added as a sync target on 2026-07-03, but sampled videos checked by `yt-dlp` were March 2026 uploads, not current daily uploads.
- If another daily-updating "人生の糸" channel exists, its exact channel URL or channel ID must be added to `analysis_competitor_db__channels`.

## Current Documentation Gap

Existing docs mention the metadata batch and the 2026-06-24 targeted OCR/metadata work, but they did not clearly document:
- That `TARGET_CHANNEL_IDS` is optional and must mean "all channels" when absent.
- That scheduled and manual executions can differ by environment variables.
- The expected sanity thresholds for scheduled runs.
- The immediate checks for `target_count=0`.

Also, `cloud_batch/README.md` currently displays mojibake in this environment, so it should not be treated as the only operational runbook until it is repaired.

## Prevention

Recommended checks:
- Alert if `target_count=0` on `senior-reading-daily-metadata`.
- Alert if max `published_at` in `analysis_competitor_db__videos` is older than 2 days for active channels.
- After every redeploy, run one full execution with no `TARGET_CHANNEL_IDS` and confirm `target_count > 0`.
- Keep targeted rerun env vars out of the persistent scheduled job unless intentionally needed.
- Add a small unit-style test around SQL construction:
  - no `TARGET_CHANNEL_IDS` -> no channel filter
  - non-empty `TARGET_CHANNEL_IDS` -> channel filter included

