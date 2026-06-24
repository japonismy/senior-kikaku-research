# Additional Thumbnail OCR Retry Report 2026-06-24

Target channels:
- 人生は贈り物
- 人生の温もり
- 人生のヒカリ
- 人生は100年
- 人生は宝物

Batch status:
- All five channels are registered with `include=1` and `sync_target=senior_reading`.
- Targeted Cloud Run execution completed successfully:
  - `senior-reading-daily-thumbnail-ocr-ccdjt`
- Site data was regenerated from BigQuery and pushed in commit `d53619f`.

Final BigQuery status:

| Channel | Videos | With OCR | Missing OCR | Missing thumbnail asset |
|---|---:|---:|---:|---:|
| 人生のヒカリ | 59 | 54 | 5 | 1 |
| 人生の温もり | 63 | 47 | 16 | 16 |
| 人生は100年 | 166 | 166 | 0 | 0 |
| 人生は宝物 | 11 | 0 | 11 | 11 |
| 人生は贈り物 | 34 | 34 | 0 | 0 |

Final portal-data status:

| Channel | Videos | With OCR | Missing OCR |
|---|---:|---:|---:|
| 人生のヒカリ | 59 | 58 | 1 |
| 人生の温もり | 55 | 39 | 16 |
| 人生は100年 | 166 | 166 | 0 |
| 人生は宝物 | 11 | 0 | 11 |
| 人生は贈り物 | 34 | 34 | 0 |

Gate result:
- Never-attempted OCR count is now 0 for these five channels.
- Remaining portal-data missing OCR count is 28.
- Remaining failures are aligned with missing/unavailable thumbnail assets.
