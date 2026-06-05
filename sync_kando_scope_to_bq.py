# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import bigquery


PROJECT_ID = "rugged-destiny-408613"
DATASET = "senior_reading_all"
CHANNEL_TABLE = "analysis_competitor_db__channels"
SCOPE_TABLE = "research_channel_scopes"
HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config" / "jun_kando_12_channels.json"


def main() -> int:
    os.environ.setdefault("CLOUDSDK_CORE_ACCOUNT", "japonismy@gmail.com")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    client = bigquery.Client(project=PROJECT_ID)
    ensure_table(client)
    channels = resolve_channels(client, cfg["channel_names"])
    rows = [
        {
            "scope": cfg["scope"],
            "label": cfg["label"],
            "channel_id": row["channel_id"],
            "channel_name": row["channel_name"],
            "is_kando_research_target": True,
            "source_url": cfg["source_url"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        for row in channels
    ]
    merge_rows(client, rows)
    found_names = {row["channel_name"] for row in channels}
    missing = [name for name in cfg["channel_names"] if name not in found_names]
    print(json.dumps({"scope": cfg["scope"], "matched": len(rows), "missing": missing}, ensure_ascii=False))
    return 0


def ensure_table(client: bigquery.Client) -> None:
    table = bigquery.Table(
        f"{PROJECT_ID}.{DATASET}.{SCOPE_TABLE}",
        schema=[
            bigquery.SchemaField("scope", "STRING"),
            bigquery.SchemaField("label", "STRING"),
            bigquery.SchemaField("channel_id", "STRING"),
            bigquery.SchemaField("channel_name", "STRING"),
            bigquery.SchemaField("is_kando_research_target", "BOOL"),
            bigquery.SchemaField("source_url", "STRING"),
            bigquery.SchemaField("updated_at", "TIMESTAMP"),
        ],
    )
    client.create_table(table, exists_ok=True)


def resolve_channels(client: bigquery.Client, names: list[str]) -> list[dict]:
    sql = f"""
    SELECT channel_id, channel_name
    FROM `{PROJECT_ID}.{DATASET}.{CHANNEL_TABLE}`
    WHERE channel_name IN UNNEST(@names)
    ORDER BY channel_name
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("names", "STRING", names)]
    )
    return [dict(row) for row in client.query(sql, job_config=job_config).result()]


def merge_rows(client: bigquery.Client, rows: list[dict]) -> None:
    temp_table = f"{PROJECT_ID}.{DATASET}._tmp_research_channel_scopes"
    client.load_table_from_json(
        rows,
        temp_table,
        job_config=bigquery.LoadJobConfig(
            schema=[
                bigquery.SchemaField("scope", "STRING"),
                bigquery.SchemaField("label", "STRING"),
                bigquery.SchemaField("channel_id", "STRING"),
                bigquery.SchemaField("channel_name", "STRING"),
                bigquery.SchemaField("is_kando_research_target", "BOOL"),
                bigquery.SchemaField("source_url", "STRING"),
                bigquery.SchemaField("updated_at", "TIMESTAMP"),
            ],
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        ),
    ).result()
    sql = f"""
    MERGE `{PROJECT_ID}.{DATASET}.{SCOPE_TABLE}` T
    USING `{temp_table}` S
    ON T.scope = S.scope AND T.channel_id = S.channel_id
    WHEN MATCHED THEN UPDATE SET
      label = S.label,
      channel_name = S.channel_name,
      is_kando_research_target = S.is_kando_research_target,
      source_url = S.source_url,
      updated_at = S.updated_at
    WHEN NOT MATCHED THEN INSERT (
      scope, label, channel_id, channel_name, is_kando_research_target, source_url, updated_at
    ) VALUES (
      S.scope, S.label, S.channel_id, S.channel_name, S.is_kando_research_target, S.source_url, S.updated_at
    )
    """
    client.query(sql).result()
    client.delete_table(temp_table, not_found_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
