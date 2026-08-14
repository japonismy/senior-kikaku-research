# -*- coding: utf-8 -*-
"""Consolidate legacy thumbnail-text overrides into the BigQuery OCR table."""
from __future__ import annotations

import csv
import uuid
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import bigquery

from manus_pipeline import DATASET, PROJECT_ID


DEFAULT_SOURCE = Path(__file__).resolve().parents[1] / "thumbnail_text_overrides.csv"


def main() -> int:
    rows = []
    with DEFAULT_SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            video_id = str(row.get("video_id") or "").strip()
            text = str(row.get("thumbnail_text") or "").strip()
            if video_id and text:
                rows.append(
                    {
                        "video_id": video_id,
                        "combined_text": text,
                        "notes": f"legacy_override:{str(row.get('note') or '').strip()}",
                    }
                )
    client = bigquery.Client(project=PROJECT_ID)
    temporary = f"{PROJECT_ID}.{DATASET}._tmp_thumbnail_text_overrides_{uuid.uuid4().hex}"
    schema = [
        bigquery.SchemaField("video_id", "STRING"),
        bigquery.SchemaField("combined_text", "STRING"),
        bigquery.SchemaField("notes", "STRING"),
    ]
    client.load_table_from_json(rows, temporary, job_config=bigquery.LoadJobConfig(schema=schema)).result()
    analyzed_at = datetime.now(timezone.utc).isoformat()
    config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("analyzed_at", "STRING", analyzed_at)]
    )
    try:
        result = client.query(
            f"""
            MERGE `{PROJECT_ID}.{DATASET}.thumbnail_ocr_gemini` T
            USING `{temporary}` S ON T.video_id=S.video_id
            WHEN MATCHED AND COALESCE(T.combined_text, '') = '' THEN UPDATE SET
              combined_text=S.combined_text,
              narration_text=S.combined_text,
              notes=S.notes,
              error='',
              analyzed_at=@analyzed_at
            WHEN NOT MATCHED THEN INSERT
              (video_id, combined_text, emphasis_text, narration_text, dialogue_text,
               top_upper_text, top_lower_text, center_text, bottom_upper_text,
               bottom_lower_text, raw_json, notes, error, analyzed_at)
            VALUES
              (S.video_id, S.combined_text, '', S.combined_text, '', '', '', '', '', '',
               '', S.notes, '', @analyzed_at)
            """,
            job_config=config,
        ).result()
    finally:
        client.delete_table(temporary, not_found_ok=True)
    print({"source_rows": len(rows), "affected_rows": result.num_dml_affected_rows})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
