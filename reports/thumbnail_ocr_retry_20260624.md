# Thumbnail OCR Retry Report 2026-06-24

Target channels:
- 人生は七色
- 人生の糸
- 人生を紡ぐ
- 人生の輝き
- 人生逆転ドラマch
- 人生逆転ストーリー
- 感動スカっと人生録
- 人生の縁側
- 夜明け座

Notes:
- `夜明け座` was not found in the current portal/BigQuery data.
- `人生の糸` and `人生は七色` are both scheduled batch targets: `include=1`, `sync_target=senior_reading`.
- `感動スカっと人生録` is the stored channel name. The user spelling `感動スカッと人生録` did not match exactly.
- The targeted OCR job was updated to support `TARGET_CHANNEL_IDS`, `FLUSH_EVERY`, and `RETRY_FAILED_OCR`.
- The metadata job was updated to support `TARGET_CHANNEL_IDS`.

Execution summary:
- Targeted OCR executions completed:
  - `senior-reading-daily-thumbnail-ocr-hrxvt`: 300 targets, ok 160, fail 140.
  - `senior-reading-daily-thumbnail-ocr-tfqsx`: 300 targets.
  - `senior-reading-daily-thumbnail-ocr-nbcg9`: remaining never-attempted targets.
- The initial 1000-target execution was canceled after timeout behavior was observed.
- Site data was regenerated from BigQuery and pushed in commit `6588112`.

Final BigQuery status for the 8 found channels:

| Channel | Videos | With OCR | Missing OCR | Missing thumbnail asset |
|---|---:|---:|---:|---:|
| 人生の糸 | 14 | 13 | 1 | 1 |
| 人生の縁側 | 14 | 10 | 4 | 4 |
| 人生の輝き | 266 | 32 | 234 | 234 |
| 人生は七色 | 138 | 126 | 12 | 12 |
| 人生を紡ぐ | 10 | 10 | 0 | 0 |
| 人生逆転ストーリー | 167 | 9 | 158 | 158 |
| 人生逆転ドラマch | 101 | 101 | 0 | 0 |
| 感動スカっと人生録 | 163 | 13 | 150 | 150 |

Final totals:
- Videos in found target channels: 873
- With OCR: 314
- Missing OCR: 559
- Never attempted: 0
- Failed empty OCR rows: 559
- Missing thumbnail asset: 559

Gate result:
- All videos that could be attempted from available thumbnail assets/URLs were attempted.
- Remaining failures are aligned with missing thumbnail assets.
- Sample check showed YouTube thumbnail URLs returning HTTP 404, so the remaining issue is source thumbnail unavailability rather than OCR execution not running.
