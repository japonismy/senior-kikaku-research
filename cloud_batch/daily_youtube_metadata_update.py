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
from google.cloud import storage


PROJECT_ID = os.environ.get("PROJECT_ID", "rugged-destiny-408613")
DATASET = os.environ.get("BQ_DATASET", "senior_reading_all")
VIDEO_TABLE = os.environ.get("VIDEO_TABLE", "analysis_competitor_db__videos")
CHANNEL_TABLE = os.environ.get("CHANNEL_TABLE", "analysis_competitor_db__channels")
RUN_LOG_TABLE = os.environ.get("RUN_LOG_TABLE", "youtube_metadata_update_runs")
SNAPSHOT_TABLE = os.environ.get("SNAPSHOT_TABLE", "video_snapshots")
THUMBNAIL_ASSET_TABLE = os.environ.get("THUMBNAIL_ASSET_TABLE", "thumbnail_assets")
THUMBNAIL_BUCKET = os.environ.get("THUMBNAIL_BUCKET", "senior-share-staging-570862915709")
THUMBNAIL_PREFIX = os.environ.get("THUMBNAIL_PREFIX", "senior_reading_thumbnails")
DOWNLOAD_THUMBNAILS = os.environ.get("DOWNLOAD_THUMBNAILS", "1") != "0"
LIMIT = int(os.environ.get("LIMIT", "0"))
SLEEP_SEC = float(os.environ.get("SLEEP_SEC", "0.1"))


def main() -> int:
    started_at = utc_now()
    run_id = str(uuid4())
    client = bigquery.Client(project=PROJECT_ID)
    storage_client = storage.Client(project=PROJECT_ID)
    keys = load_api_keys()
    if not keys:
        raise SystemExit("YOUTUBE_API_KEY or YOUTUBE_API_KEYS is required.")

    ensure_run_log_table(client)
    ensure_snapshot_table(client)
    ensure_thumbnail_asset_table(client)
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
        merge_video_snapshots(client, updated_rows)

    thumbnail_stats = {"checked": 0, "downloaded": 0, "skipped": 0, "failed": 0}
    if DOWNLOAD_THUMBNAILS and updated_rows:
        thumbnail_stats = sync_thumbnail_assets(client, storage_client, updated_rows)

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
            "thumbnail_checked": thumbnail_stats["checked"],
            "thumbnail_downloaded": thumbnail_stats["downloaded"],
            "thumbnail_failed": thumbnail_stats["failed"],
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
        "thumbnail_checked": thumbnail_stats["checked"],
        "thumbnail_downloaded": thumbnail_stats["downloaded"],
        "thumbnail_failed": thumbnail_stats["failed"],
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


def merge_video_snapshots(client: bigquery.Client, rows: list[dict]) -> None:
    snapshot_rows = []
    for row in rows:
        snapshot_date = row["fetched_at"][:10]
        views = row.get("view_count") or 0
        likes = row.get("like_count") or 0
        comments = row.get("comment_count") or 0
        snapshot_rows.append({
            "snapshot_date": snapshot_date,
            "video_id": row["video_id"],
            "view_count": views,
            "like_count": likes,
            "comment_count": comments,
            "like_rate": likes / views if views else 0.0,
            "comment_rate": comments / views if views else 0.0,
            "fetched_at": row["fetched_at"],
        })
    temp_table = f"{PROJECT_ID}.{DATASET}._tmp_video_snapshots_{uuid4().hex}"
    client.load_table_from_json(
        snapshot_rows,
        temp_table,
        job_config=bigquery.LoadJobConfig(
            schema=[
                bigquery.SchemaField("snapshot_date", "DATE"),
                bigquery.SchemaField("video_id", "STRING"),
                bigquery.SchemaField("view_count", "INT64"),
                bigquery.SchemaField("like_count", "INT64"),
                bigquery.SchemaField("comment_count", "INT64"),
                bigquery.SchemaField("like_rate", "FLOAT64"),
                bigquery.SchemaField("comment_rate", "FLOAT64"),
                bigquery.SchemaField("fetched_at", "TIMESTAMP"),
            ],
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        ),
    ).result()
    sql = f"""
    MERGE `{PROJECT_ID}.{DATASET}.{SNAPSHOT_TABLE}` T
    USING `{temp_table}` S
    ON T.snapshot_date = S.snapshot_date
      AND T.video_id = S.video_id
    WHEN MATCHED THEN UPDATE SET
      view_count = S.view_count,
      like_count = S.like_count,
      comment_count = S.comment_count,
      like_rate = S.like_rate,
      comment_rate = S.comment_rate,
      fetched_at = S.fetched_at
    WHEN NOT MATCHED THEN INSERT (
      snapshot_date, video_id, view_count, like_count, comment_count, like_rate, comment_rate, fetched_at
    ) VALUES (
      S.snapshot_date, S.video_id, S.view_count, S.like_count, S.comment_count, S.like_rate, S.comment_rate, S.fetched_at
    )
    """
    client.query(sql).result()
    client.delete_table(temp_table, not_found_ok=True)


def sync_thumbnail_assets(client: bigquery.Client, storage_client: storage.Client, rows: list[dict]) -> dict[str, int]:
    existing = fetch_existing_thumbnail_assets(client, [row["video_id"] for row in rows])
    bucket = storage_client.bucket(THUMBNAIL_BUCKET)
    asset_rows = []
    stats = {"checked": 0, "downloaded": 0, "skipped": 0, "failed": 0}
    for row in rows:
        video_id = row["video_id"]
        source_url = row.get("thumbnail_url", "")
        if not source_url:
            continue
        stats["checked"] += 1
        current = existing.get(video_id)
        if current and current.get("source_url") == source_url and current.get("gcs_uri"):
            stats["skipped"] += 1
            continue
        asset_rows.append(download_thumbnail_asset(bucket, video_id, source_url))
        if asset_rows[-1].get("error"):
            stats["failed"] += 1
        else:
            stats["downloaded"] += 1
    if asset_rows:
        merge_thumbnail_assets(client, asset_rows)
    return stats


def fetch_existing_thumbnail_assets(client: bigquery.Client, video_ids: list[str]) -> dict[str, dict]:
    if not video_ids:
        return {}
    table_id = f"{PROJECT_ID}.{DATASET}.{THUMBNAIL_ASSET_TABLE}"
    sql = f"""
    SELECT video_id, source_url, gcs_uri
    FROM `{table_id}`
    WHERE video_id IN UNNEST(@video_ids)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("video_ids", "STRING", video_ids)]
    )
    try:
        return {row.video_id: dict(row) for row in client.query(sql, job_config=job_config).result()}
    except Exception:
        return {}


def download_thumbnail_asset(bucket: storage.Bucket, video_id: str, source_url: str) -> dict:
    fetched_at = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    quality = thumbnail_quality(source_url)
    object_name = f"{THUMBNAIL_PREFIX}/{video_id}.jpg"
    gcs_uri = f"gs://{THUMBNAIL_BUCKET}/{object_name}"
    try:
        req = urllib.request.Request(source_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "image/jpeg")
        blob = bucket.blob(object_name)
        blob.upload_from_string(data, content_type=content_type)
        return {
            "video_id": video_id,
            "gcs_uri": gcs_uri,
            "source_url": source_url,
            "quality": quality,
            "bytes": len(data),
            "content_type": content_type,
            "fetched_at": fetched_at,
            "error": "",
        }
    except Exception as e:
        return {
            "video_id": video_id,
            "gcs_uri": "",
            "source_url": source_url,
            "quality": quality,
            "bytes": 0,
            "content_type": "",
            "fetched_at": fetched_at,
            "error": f"{type(e).__name__}: {str(e)[:300]}",
        }


def thumbnail_quality(url: str) -> str:
    for value in ["maxresdefault", "sddefault", "hqdefault", "mqdefault", "default"]:
        if value in url:
            return value
    return ""


def merge_thumbnail_assets(client: bigquery.Client, rows: list[dict]) -> None:
    temp_table = f"{PROJECT_ID}.{DATASET}._tmp_thumbnail_assets_{uuid4().hex}"
    schema = [
        bigquery.SchemaField("video_id", "STRING"),
        bigquery.SchemaField("gcs_uri", "STRING"),
        bigquery.SchemaField("source_url", "STRING"),
        bigquery.SchemaField("quality", "STRING"),
        bigquery.SchemaField("bytes", "INT64"),
        bigquery.SchemaField("content_type", "STRING"),
        bigquery.SchemaField("fetched_at", "STRING"),
        bigquery.SchemaField("error", "STRING"),
    ]
    client.load_table_from_json(
        rows,
        temp_table,
        job_config=bigquery.LoadJobConfig(schema=schema, write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE),
    ).result()
    sql = f"""
    MERGE `{PROJECT_ID}.{DATASET}.{THUMBNAIL_ASSET_TABLE}` T
    USING `{temp_table}` S
    ON T.video_id = S.video_id
    WHEN MATCHED THEN UPDATE SET
      gcs_uri = S.gcs_uri,
      source_url = S.source_url,
      quality = S.quality,
      bytes = S.bytes,
      content_type = S.content_type,
      fetched_at = S.fetched_at,
      error = S.error
    WHEN NOT MATCHED THEN INSERT (
      video_id, gcs_uri, source_url, quality, bytes, content_type, fetched_at, error
    ) VALUES (
      S.video_id, S.gcs_uri, S.source_url, S.quality, S.bytes, S.content_type, S.fetched_at, S.error
    )
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
            bigquery.SchemaField("thumbnail_checked", "INT64"),
            bigquery.SchemaField("thumbnail_downloaded", "INT64"),
            bigquery.SchemaField("thumbnail_failed", "INT64"),
            bigquery.SchemaField("error", "STRING"),
        ],
    )
    client.create_table(table, exists_ok=True)
    ensure_columns(
        client,
        RUN_LOG_TABLE,
        [
            ("thumbnail_checked", "INT64"),
            ("thumbnail_downloaded", "INT64"),
            ("thumbnail_failed", "INT64"),
        ],
    )


def ensure_thumbnail_asset_table(client: bigquery.Client) -> None:
    table = bigquery.Table(
        f"{PROJECT_ID}.{DATASET}.{THUMBNAIL_ASSET_TABLE}",
        schema=[
            bigquery.SchemaField("video_id", "STRING"),
            bigquery.SchemaField("gcs_uri", "STRING"),
            bigquery.SchemaField("source_url", "STRING"),
            bigquery.SchemaField("quality", "STRING"),
            bigquery.SchemaField("bytes", "INT64"),
            bigquery.SchemaField("content_type", "STRING"),
            bigquery.SchemaField("fetched_at", "STRING"),
            bigquery.SchemaField("error", "STRING"),
        ],
    )
    client.create_table(table, exists_ok=True)


def ensure_snapshot_table(client: bigquery.Client) -> None:
    table = bigquery.Table(
        f"{PROJECT_ID}.{DATASET}.{SNAPSHOT_TABLE}",
        schema=[
            bigquery.SchemaField("snapshot_date", "DATE"),
            bigquery.SchemaField("video_id", "STRING"),
            bigquery.SchemaField("view_count", "INT64"),
            bigquery.SchemaField("like_count", "INT64"),
            bigquery.SchemaField("comment_count", "INT64"),
            bigquery.SchemaField("like_rate", "FLOAT64"),
            bigquery.SchemaField("comment_rate", "FLOAT64"),
            bigquery.SchemaField("fetched_at", "TIMESTAMP"),
        ],
    )
    client.create_table(table, exists_ok=True)
    ensure_columns(
        client,
        SNAPSHOT_TABLE,
        [
            ("like_rate", "FLOAT64"),
            ("comment_rate", "FLOAT64"),
            ("fetched_at", "TIMESTAMP"),
        ],
    )


def ensure_columns(client: bigquery.Client, table_name: str, columns: list[tuple[str, str]]) -> None:
    table = client.get_table(f"{PROJECT_ID}.{DATASET}.{table_name}")
    existing = {field.name for field in table.schema}
    missing = [bigquery.SchemaField(name, type_) for name, type_ in columns if name not in existing]
    if not missing:
        return
    table.schema = list(table.schema) + missing
    client.update_table(table, ["schema"])


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
