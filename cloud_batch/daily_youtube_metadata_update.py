# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from uuid import uuid4

from google.cloud import bigquery


PROJECT_ID = os.environ.get("PROJECT_ID", "rugged-destiny-408613")
DATASET = os.environ.get("BQ_DATASET", "senior_reading_all")
VIDEO_TABLE = os.environ.get("VIDEO_TABLE", "analysis_competitor_db__videos")
CHANNEL_TABLE = os.environ.get("CHANNEL_TABLE", "analysis_competitor_db__channels")
RUN_LOG_TABLE = os.environ.get("RUN_LOG_TABLE", "youtube_metadata_update_runs")
LIMIT = int(os.environ.get("LIMIT", "0"))
SLEEP_SEC = float(os.environ.get("SLEEP_SEC", "0.1"))


def main() -> int:
    started_at = utc_now()
    run_id = str(uuid4())
    client = bigquery.Client(project=PROJECT_ID)
    keys = load_api_keys()
    if not keys:
        raise SystemExit("YOUTUBE_API_KEY or YOUTUBE_API_KEYS is required.")

    ensure_run_log_table(client)
    ids = fetch_target_video_ids(client)
    if LIMIT:
        ids = ids[:LIMIT]

    updated_rows = []
    missing = 0
    quota_skips = 0
    errors = []
    key_index = 0

    for batch in chunks(ids, 50):
        data = None
        while key_index < len(keys):
            key_name, key = keys[key_index]
            try:
                data = fetch_youtube_videos(key, batch)
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code == 403 and "quotaExceeded" in body:
                    quota_skips += 1
                    key_index += 1
                    continue
                errors.append(f"HTTP {e.code}: {body[:300]}")
                raise
        if data is None:
            errors.append("All YouTube API keys appear to be quota-exceeded.")
            break

        items = {item["id"]: item for item in data.get("items", [])}
        fetched_at = utc_now()
        for video_id in batch:
            item = items.get(video_id)
            if not item:
                missing += 1
                continue
            updated_rows.append(make_update_row(video_id, item, fetched_at))
        time.sleep(SLEEP_SEC)

    if updated_rows:
        merge_video_updates(client, updated_rows)

    finished_at = utc_now()
    log_run(
        client,
        {
            "run_id": run_id,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "target_count": len(ids),
            "updated_count": len(updated_rows),
            "missing_count": missing,
            "quota_skips": quota_skips,
            "key_used": keys[key_index][0] if key_index < len(keys) else "",
            "error": "\n".join(errors)[:2000],
        },
    )
    print(json.dumps({
        "run_id": run_id,
        "target_count": len(ids),
        "updated_count": len(updated_rows),
        "missing_count": missing,
        "quota_skips": quota_skips,
        "key_used": keys[key_index][0] if key_index < len(keys) else "",
    }, ensure_ascii=False))
    return 0 if not errors else 1


def load_api_keys() -> list[tuple[str, str]]:
    raw_multi = os.environ.get("YOUTUBE_API_KEYS", "").strip()
    keys: list[tuple[str, str]] = []
    if raw_multi:
        try:
            parsed = json.loads(raw_multi)
            if isinstance(parsed, dict):
                keys.extend((str(k), str(v)) for k, v in parsed.items() if v)
            elif isinstance(parsed, list):
                keys.extend((f"key{i + 1}", str(v)) for i, v in enumerate(parsed) if v)
        except json.JSONDecodeError:
            keys.extend((f"key{i + 1}", v.strip()) for i, v in enumerate(raw_multi.split(",")) if v.strip())
    single = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if single and not any(v == single for _, v in keys):
        keys.insert(0, ("default", single))
    return keys


def fetch_target_video_ids(client: bigquery.Client) -> list[str]:
    sql = f"""
    SELECT v.video_id
    FROM `{PROJECT_ID}.{DATASET}.{VIDEO_TABLE}` v
    JOIN `{PROJECT_ID}.{DATASET}.{CHANNEL_TABLE}` c
      ON c.channel_id = v.channel_id
    WHERE v.thumbnail_url IS NOT NULL
      AND v.thumbnail_url != ''
      AND c.sync_target = 'senior_reading'
      AND COALESCE(c.include, 1) = 1
      AND COALESCE(c.source_type, '') != 'original_kr'
      AND (v.duration_sec IS NULL OR v.duration_sec >= 120)
    ORDER BY COALESCE(v.view_count, 0) DESC
    """
    return [row.video_id for row in client.query(sql).result()]


def fetch_youtube_videos(api_key: str, ids: list[str]) -> dict:
    params = {
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(ids),
        "key": api_key,
        "maxResults": "50",
    }
    url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def make_update_row(video_id: str, item: dict, fetched_at: datetime) -> dict:
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    content = item.get("contentDetails", {})
    return {
        "video_id": video_id,
        "title": snippet.get("title", ""),
        "published_at": snippet.get("publishedAt", ""),
        "view_count": int(stats.get("viewCount", 0) or 0),
        "like_count": int(stats.get("likeCount", 0) or 0),
        "comment_count": int(stats.get("commentCount", 0) or 0),
        "thumbnail_url": best_thumbnail_url(snippet.get("thumbnails", {})),
        "duration": content.get("duration", ""),
        "fetched_at": fetched_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def best_thumbnail_url(thumbs: dict) -> str:
    for key in ["maxres", "standard", "high", "medium", "default"]:
        if key in thumbs and thumbs[key].get("url"):
            return thumbs[key]["url"]
    return ""


def merge_video_updates(client: bigquery.Client, rows: list[dict]) -> None:
    temp_table = f"{PROJECT_ID}.{DATASET}._tmp_video_metadata_{uuid4().hex}"
    job = client.load_table_from_json(
        rows,
        temp_table,
        job_config=bigquery.LoadJobConfig(
            schema=[
                bigquery.SchemaField("video_id", "STRING"),
                bigquery.SchemaField("title", "STRING"),
                bigquery.SchemaField("published_at", "STRING"),
                bigquery.SchemaField("view_count", "INT64"),
                bigquery.SchemaField("like_count", "INT64"),
                bigquery.SchemaField("comment_count", "INT64"),
                bigquery.SchemaField("thumbnail_url", "STRING"),
                bigquery.SchemaField("duration", "STRING"),
                bigquery.SchemaField("fetched_at", "STRING"),
            ],
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        ),
    )
    job.result()
    sql = f"""
    MERGE `{PROJECT_ID}.{DATASET}.{VIDEO_TABLE}` T
    USING `{temp_table}` S
    ON T.video_id = S.video_id
    WHEN MATCHED THEN UPDATE SET
      title = S.title,
      published_at = S.published_at,
      view_count = S.view_count,
      like_count = S.like_count,
      comment_count = S.comment_count,
      thumbnail_url = COALESCE(NULLIF(S.thumbnail_url, ''), T.thumbnail_url),
      duration = S.duration,
      fetched_at = S.fetched_at
    """
    client.query(sql).result()
    client.delete_table(temp_table, not_found_ok=True)


def ensure_run_log_table(client: bigquery.Client) -> None:
    table_id = f"{PROJECT_ID}.{DATASET}.{RUN_LOG_TABLE}"
    table = bigquery.Table(
        table_id,
        schema=[
            bigquery.SchemaField("run_id", "STRING"),
            bigquery.SchemaField("started_at", "TIMESTAMP"),
            bigquery.SchemaField("finished_at", "TIMESTAMP"),
            bigquery.SchemaField("target_count", "INT64"),
            bigquery.SchemaField("updated_count", "INT64"),
            bigquery.SchemaField("missing_count", "INT64"),
            bigquery.SchemaField("quota_skips", "INT64"),
            bigquery.SchemaField("key_used", "STRING"),
            bigquery.SchemaField("error", "STRING"),
        ],
    )
    client.create_table(table, exists_ok=True)


def log_run(client: bigquery.Client, row: dict) -> None:
    table_id = f"{PROJECT_ID}.{DATASET}.{RUN_LOG_TABLE}"
    errors = client.insert_rows_json(table_id, [row])
    if errors:
        raise RuntimeError(f"Failed to insert run log: {errors}")


def chunks(values: list[str], size: int):
    for i in range(0, len(values), size):
        yield values[i:i + size]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())
