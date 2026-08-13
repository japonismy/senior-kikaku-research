# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import re
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
AVAILABILITY_CHECK_TABLE = os.environ.get("AVAILABILITY_CHECK_TABLE", "video_availability_checks")
AVAILABILITY_CURRENT_TABLE = os.environ.get("AVAILABILITY_CURRENT_TABLE", "video_availability_current")
AVAILABILITY_EVENT_TABLE = os.environ.get("AVAILABILITY_EVENT_TABLE", "video_availability_events")
THUMBNAIL_ASSET_TABLE = os.environ.get("THUMBNAIL_ASSET_TABLE", "thumbnail_assets")
THUMBNAIL_BUCKET = os.environ.get("THUMBNAIL_BUCKET", "senior-share-staging-570862915709")
THUMBNAIL_PREFIX = os.environ.get("THUMBNAIL_PREFIX", "senior_reading_thumbnails")
DOWNLOAD_THUMBNAILS = os.environ.get("DOWNLOAD_THUMBNAILS", "1") != "0"
LIMIT = int(os.environ.get("LIMIT", "0"))
SLEEP_SEC = float(os.environ.get("SLEEP_SEC", "0.1"))
DISCOVER_RECENT_UPLOADS = os.environ.get("DISCOVER_RECENT_UPLOADS", "1") != "0"
DISCOVERY_UPLOADS_PER_CHANNEL = int(os.environ.get("DISCOVERY_UPLOADS_PER_CHANNEL", "20"))
TARGET_CHANNEL_IDS = [v.strip() for v in os.environ.get("TARGET_CHANNEL_IDS", "").split(",") if v.strip()]
AVAILABILITY_CONFIRM_MISSES = max(2, int(os.environ.get("AVAILABILITY_CONFIRM_MISSES", "2")))


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
    ensure_availability_tables(client)
    ensure_thumbnail_asset_table(client)
    ids = fetch_target_video_ids(client)
    discovered_ids: list[str] = []
    discovery_errors: list[str] = []
    key_index = 0
    quota_skips = 0
    if DISCOVER_RECENT_UPLOADS:
        discovered_ids, discovery_quota_skips, key_index, discovery_errors = discover_recent_upload_video_ids(client, keys)
        quota_skips += discovery_quota_skips
        ids = dedupe(discovered_ids + ids)
    if LIMIT:
        ids = ids[:LIMIT]

    updated_rows = []
    availability_checks = []
    missing = 0
    skipped_short = 0
    errors = discovery_errors[:]

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
                availability_checks.append(make_availability_check(run_id, video_id, "missing_api", fetched_at))
                continue
            availability_checks.append(make_availability_check(run_id, video_id, "public", fetched_at))
            row = make_update_row(video_id, item, fetched_at)
            duration_sec = row.get("duration_sec")
            if duration_sec is not None and duration_sec < 120:
                skipped_short += 1
                continue
            updated_rows.append(row)
        time.sleep(SLEEP_SEC)

    if updated_rows:
        merge_video_updates(client, updated_rows)
        merge_video_snapshots(client, updated_rows)

    availability_stats = {"checked": 0, "events": 0, "suspected": 0, "confirmed": 0, "restored": 0}
    if availability_checks:
        availability_stats = persist_availability_checks(client, availability_checks)

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
            "discovered_count": len(discovered_ids),
            "updated_count": len(updated_rows),
            "missing_count": missing,
            "skipped_short_count": skipped_short,
            "quota_skips": quota_skips,
            "key_used": keys[key_index][0] if key_index < len(keys) else "",
            "thumbnail_checked": thumbnail_stats["checked"],
            "thumbnail_downloaded": thumbnail_stats["downloaded"],
            "thumbnail_failed": thumbnail_stats["failed"],
            "availability_checked": availability_stats["checked"],
            "availability_events": availability_stats["events"],
            "availability_suspected": availability_stats["suspected"],
            "availability_confirmed": availability_stats["confirmed"],
            "availability_restored": availability_stats["restored"],
            "error": "\n".join(errors)[:2000],
        },
    )
    print(json.dumps({
        "run_id": run_id,
        "target_count": len(ids),
        "discovered_count": len(discovered_ids),
        "updated_count": len(updated_rows),
        "missing_count": missing,
        "skipped_short_count": skipped_short,
        "quota_skips": quota_skips,
        "key_used": keys[key_index][0] if key_index < len(keys) else "",
        "thumbnail_checked": thumbnail_stats["checked"],
        "thumbnail_downloaded": thumbnail_stats["downloaded"],
        "thumbnail_failed": thumbnail_stats["failed"],
        "availability_checked": availability_stats["checked"],
        "availability_events": availability_stats["events"],
        "availability_suspected": availability_stats["suspected"],
        "availability_confirmed": availability_stats["confirmed"],
        "availability_restored": availability_stats["restored"],
    }, ensure_ascii=False))
    return 0 if updated_rows else 1


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
    primary = os.environ.get("YOUTUBE_API_KEY_PRIMARY", "").strip()
    fallback = os.environ.get("YOUTUBE_API_KEY_FALLBACK", "").strip()
    for name, value in reversed([("primary", primary), ("fallback", fallback)]):
        if value and not any(existing == value for _, existing in keys):
            keys.insert(0, (name, value))
    return keys


def fetch_target_video_ids(client: bigquery.Client) -> list[str]:
    target_filter = "AND v.channel_id IN UNNEST(@target_channel_ids)" if TARGET_CHANNEL_IDS else ""
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
      {target_filter}
    ORDER BY COALESCE(v.view_count, 0) DESC
    """
    config = None
    if TARGET_CHANNEL_IDS:
        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("target_channel_ids", "STRING", TARGET_CHANNEL_IDS),
            ]
        )
    return [row.video_id for row in client.query(sql, job_config=config).result()]


def fetch_target_channels(client: bigquery.Client) -> list[str]:
    target_filter = "AND channel_id IN UNNEST(@target_channel_ids)" if TARGET_CHANNEL_IDS else ""
    sql = f"""
    SELECT DISTINCT channel_id
    FROM `{PROJECT_ID}.{DATASET}.{CHANNEL_TABLE}`
    WHERE channel_id IS NOT NULL
      AND channel_id != ''
      AND sync_target = 'senior_reading'
      AND COALESCE(include, 1) = 1
      AND COALESCE(source_type, '') != 'original_kr'
      {target_filter}
    ORDER BY channel_id
    """
    config = None
    if TARGET_CHANNEL_IDS:
        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("target_channel_ids", "STRING", TARGET_CHANNEL_IDS),
            ]
        )
    return [row.channel_id for row in client.query(sql, job_config=config).result()]


def discover_recent_upload_video_ids(client: bigquery.Client, keys: list[tuple[str, str]]) -> tuple[list[str], int, int, list[str]]:
    channel_ids = fetch_target_channels(client)
    upload_playlist_ids: list[str] = []
    quota_skips = 0
    key_index = 0
    errors: list[str] = []

    for batch in chunks(channel_ids, 50):
        data, key_index, skipped, error = fetch_youtube_json_with_keys(
            keys,
            key_index,
            "channels",
            {"part": "contentDetails", "id": ",".join(batch), "maxResults": "50"},
        )
        quota_skips += skipped
        if error:
            errors.append(error)
            if key_index >= len(keys):
                break
            continue
        for item in data.get("items", []):
            uploads = (
                item.get("contentDetails", {})
                .get("relatedPlaylists", {})
                .get("uploads", "")
            )
            if uploads:
                upload_playlist_ids.append(uploads)
        time.sleep(SLEEP_SEC)

    video_ids: list[str] = []
    for playlist_id in upload_playlist_ids:
        data, key_index, skipped, error = fetch_youtube_json_with_keys(
            keys,
            key_index,
            "playlistItems",
            {
                "part": "snippet",
                "playlistId": playlist_id,
                "maxResults": str(DISCOVERY_UPLOADS_PER_CHANNEL),
            },
        )
        quota_skips += skipped
        if error:
            errors.append(error)
            if key_index >= len(keys):
                break
            continue
        for item in data.get("items", []):
            video_id = (
                item.get("snippet", {})
                .get("resourceId", {})
                .get("videoId", "")
            )
            if video_id:
                video_ids.append(video_id)
        time.sleep(SLEEP_SEC)

    return dedupe(video_ids), quota_skips, key_index, errors


def fetch_youtube_json_with_keys(
    keys: list[tuple[str, str]],
    key_index: int,
    resource: str,
    params: dict[str, str],
) -> tuple[dict, int, int, str]:
    quota_skips = 0
    while key_index < len(keys):
        _, key = keys[key_index]
        try:
            return fetch_youtube_resource(key, resource, params), key_index, quota_skips, ""
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code == 403 and "quotaExceeded" in body:
                quota_skips += 1
                key_index += 1
                continue
            return {}, key_index, quota_skips, f"{resource} HTTP {e.code}: {body[:300]}"
    return {}, key_index, quota_skips, f"{resource}: all YouTube API keys appear to be quota-exceeded."


def fetch_youtube_resource(api_key: str, resource: str, params: dict[str, str]) -> dict:
    request_params = dict(params)
    request_params["key"] = api_key
    url = f"https://www.googleapis.com/youtube/v3/{resource}?" + urllib.parse.urlencode(request_params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    duration = content.get("duration", "")
    return {
        "video_id": video_id,
        "channel_id": snippet.get("channelId", ""),
        "title": snippet.get("title", ""),
        "published_at": snippet.get("publishedAt", ""),
        "view_count": int(stats.get("viewCount", 0) or 0),
        "like_count": int(stats.get("likeCount", 0) or 0),
        "comment_count": int(stats.get("commentCount", 0) or 0),
        "description": snippet.get("description", ""),
        "tags": json.dumps(snippet.get("tags", []), ensure_ascii=False),
        "thumbnail_url": best_thumbnail_url(snippet.get("thumbnails", {})),
        "duration": duration,
        "duration_sec": parse_iso8601_duration_seconds(duration),
        "fetched_at": fetched_at.strftime("%Y-%m-%d %H:%M:%S"),
    }


def make_availability_check(run_id: str, video_id: str, raw_status: str, checked_at: datetime) -> dict:
    return {
        "check_date": checked_at.date().isoformat(),
        "video_id": video_id,
        "checked_at": checked_at.isoformat(),
        "raw_status": raw_status,
        "source": "youtube_data_api_v3",
        "returned_by_api": raw_status == "public",
        "error_code": "",
        "run_id": run_id,
    }


def derive_availability_state(previous: dict | None, check: dict) -> tuple[dict, dict | None]:
    previous = previous or {}
    checked_at = check["checked_at"]
    old_status = previous.get("status") or ""
    raw_status = check["raw_status"]

    if raw_status == "public":
        new_status = "public"
        consecutive_missing = 0
        first_missing_at = None
        confirmed_unavailable_at = None
        last_seen_public_at = checked_at
    else:
        was_missing = old_status in {"suspected_unavailable", "confirmed_unavailable"}
        consecutive_missing = int(previous.get("consecutive_missing_count") or 0) + 1 if was_missing else 1
        new_status = (
            "confirmed_unavailable"
            if consecutive_missing >= AVAILABILITY_CONFIRM_MISSES
            else "suspected_unavailable"
        )
        first_missing_at = timestamp_text(previous.get("first_missing_at")) if was_missing else checked_at
        last_seen_public_at = timestamp_text(previous.get("last_seen_public_at"))
        if new_status == "confirmed_unavailable":
            confirmed_unavailable_at = timestamp_text(previous.get("confirmed_unavailable_at")) or checked_at
        else:
            confirmed_unavailable_at = None

    state = {
        "video_id": check["video_id"],
        "status": new_status,
        "previous_status": old_status,
        "last_checked_at": checked_at,
        "last_seen_public_at": last_seen_public_at,
        "first_missing_at": first_missing_at,
        "confirmed_unavailable_at": confirmed_unavailable_at,
        "consecutive_missing_count": consecutive_missing,
        "last_raw_status": raw_status,
        "last_source": check["source"],
        "last_run_id": check["run_id"],
    }

    should_emit = (bool(old_status) and old_status != new_status) or (not old_status and new_status != "public")
    if not should_emit:
        return state, None
    event = {
        "event_id": str(uuid4()),
        "event_date": checked_at[:10],
        "video_id": check["video_id"],
        "event_at": checked_at,
        "previous_status": old_status,
        "new_status": new_status,
        "raw_status": raw_status,
        "source": check["source"],
        "run_id": check["run_id"],
        "consecutive_missing_count": consecutive_missing,
        "last_seen_public_at": last_seen_public_at,
        "first_missing_at": first_missing_at,
    }
    return state, event


def persist_availability_checks(client: bigquery.Client, checks: list[dict]) -> dict[str, int]:
    previous = fetch_current_availability(client, [row["video_id"] for row in checks])
    current_rows = []
    events = []
    for check in checks:
        state, event = derive_availability_state(previous.get(check["video_id"]), check)
        current_rows.append(state)
        previous[check["video_id"]] = state
        if event:
            events.append(event)

    append_json_rows(client, AVAILABILITY_CHECK_TABLE, checks, availability_check_schema())
    merge_availability_current(client, current_rows)
    backfill_last_seen_public_at(client)
    if events:
        append_json_rows(client, AVAILABILITY_EVENT_TABLE, events, availability_event_schema())

    return {
        "checked": len(checks),
        "events": len(events),
        "suspected": sum(1 for row in events if row["new_status"] == "suspected_unavailable"),
        "confirmed": sum(1 for row in events if row["new_status"] == "confirmed_unavailable"),
        "restored": sum(1 for row in events if row["new_status"] == "public"),
    }


def backfill_last_seen_public_at(client: bigquery.Client) -> None:
    sql = f"""
    UPDATE `{PROJECT_ID}.{DATASET}.{AVAILABILITY_CURRENT_TABLE}` T
    SET last_seen_public_at = S.last_seen_public_at
    FROM (
      SELECT video_id, MAX(fetched_at) AS last_seen_public_at
      FROM `{PROJECT_ID}.{DATASET}.{SNAPSHOT_TABLE}`
      GROUP BY video_id
    ) S
    WHERE T.video_id = S.video_id
      AND T.last_seen_public_at IS NULL
    """
    client.query(sql).result()


def fetch_current_availability(client: bigquery.Client, video_ids: list[str]) -> dict[str, dict]:
    if not video_ids:
        return {}
    sql = f"""
    SELECT *
    FROM `{PROJECT_ID}.{DATASET}.{AVAILABILITY_CURRENT_TABLE}`
    WHERE video_id IN UNNEST(@video_ids)
    """
    config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("video_ids", "STRING", dedupe(video_ids))]
    )
    return {row.video_id: dict(row) for row in client.query(sql, job_config=config).result()}


def append_json_rows(client: bigquery.Client, table_name: str, rows: list[dict], schema: list[bigquery.SchemaField]) -> None:
    if not rows:
        return
    client.load_table_from_json(
        rows,
        f"{PROJECT_ID}.{DATASET}.{table_name}",
        job_config=bigquery.LoadJobConfig(
            schema=schema,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        ),
    ).result()


def merge_availability_current(client: bigquery.Client, rows: list[dict]) -> None:
    if not rows:
        return
    temp_table = f"{PROJECT_ID}.{DATASET}._tmp_video_availability_{uuid4().hex}"
    client.load_table_from_json(
        rows,
        temp_table,
        job_config=bigquery.LoadJobConfig(
            schema=availability_current_schema(),
            write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        ),
    ).result()
    sql = f"""
    MERGE `{PROJECT_ID}.{DATASET}.{AVAILABILITY_CURRENT_TABLE}` T
    USING `{temp_table}` S
    ON T.video_id = S.video_id
    WHEN MATCHED THEN UPDATE SET
      status = S.status,
      previous_status = S.previous_status,
      last_checked_at = S.last_checked_at,
      last_seen_public_at = S.last_seen_public_at,
      first_missing_at = S.first_missing_at,
      confirmed_unavailable_at = S.confirmed_unavailable_at,
      consecutive_missing_count = S.consecutive_missing_count,
      last_raw_status = S.last_raw_status,
      last_source = S.last_source,
      last_run_id = S.last_run_id
    WHEN NOT MATCHED THEN INSERT (
      video_id, status, previous_status, last_checked_at, last_seen_public_at,
      first_missing_at, confirmed_unavailable_at, consecutive_missing_count,
      last_raw_status, last_source, last_run_id
    ) VALUES (
      S.video_id, S.status, S.previous_status, S.last_checked_at, S.last_seen_public_at,
      S.first_missing_at, S.confirmed_unavailable_at, S.consecutive_missing_count,
      S.last_raw_status, S.last_source, S.last_run_id
    )
    """
    try:
        client.query(sql).result()
    finally:
        client.delete_table(temp_table, not_found_ok=True)


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
                bigquery.SchemaField("channel_id", "STRING"),
                bigquery.SchemaField("title", "STRING"),
                bigquery.SchemaField("published_at", "STRING"),
                bigquery.SchemaField("view_count", "INT64"),
                bigquery.SchemaField("like_count", "INT64"),
                bigquery.SchemaField("comment_count", "INT64"),
                bigquery.SchemaField("description", "STRING"),
                bigquery.SchemaField("tags", "STRING"),
                bigquery.SchemaField("thumbnail_url", "STRING"),
                bigquery.SchemaField("duration", "STRING"),
                bigquery.SchemaField("duration_sec", "FLOAT64"),
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
      description = S.description,
      tags = S.tags,
      thumbnail_url = COALESCE(NULLIF(S.thumbnail_url, ''), T.thumbnail_url),
      duration = S.duration,
      duration_sec = S.duration_sec,
      fetched_at = S.fetched_at
    WHEN NOT MATCHED THEN INSERT (
      video_id, channel_id, title, published_at, view_count, like_count, comment_count,
      description, tags, thumbnail_url, duration, duration_sec, fetched_at
    ) VALUES (
      S.video_id, S.channel_id, S.title, S.published_at, S.view_count, S.like_count, S.comment_count,
      S.description, S.tags, S.thumbnail_url, S.duration, S.duration_sec, S.fetched_at
    )
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
            bigquery.SchemaField("discovered_count", "INT64"),
            bigquery.SchemaField("updated_count", "INT64"),
            bigquery.SchemaField("missing_count", "INT64"),
            bigquery.SchemaField("skipped_short_count", "INT64"),
            bigquery.SchemaField("quota_skips", "INT64"),
            bigquery.SchemaField("key_used", "STRING"),
            bigquery.SchemaField("thumbnail_checked", "INT64"),
            bigquery.SchemaField("thumbnail_downloaded", "INT64"),
            bigquery.SchemaField("thumbnail_failed", "INT64"),
            bigquery.SchemaField("availability_checked", "INT64"),
            bigquery.SchemaField("availability_events", "INT64"),
            bigquery.SchemaField("availability_suspected", "INT64"),
            bigquery.SchemaField("availability_confirmed", "INT64"),
            bigquery.SchemaField("availability_restored", "INT64"),
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
            ("discovered_count", "INT64"),
            ("skipped_short_count", "INT64"),
            ("availability_checked", "INT64"),
            ("availability_events", "INT64"),
            ("availability_suspected", "INT64"),
            ("availability_confirmed", "INT64"),
            ("availability_restored", "INT64"),
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


def availability_check_schema() -> list[bigquery.SchemaField]:
    return [
        bigquery.SchemaField("check_date", "DATE"),
        bigquery.SchemaField("video_id", "STRING"),
        bigquery.SchemaField("checked_at", "TIMESTAMP"),
        bigquery.SchemaField("raw_status", "STRING"),
        bigquery.SchemaField("source", "STRING"),
        bigquery.SchemaField("returned_by_api", "BOOLEAN"),
        bigquery.SchemaField("error_code", "STRING"),
        bigquery.SchemaField("run_id", "STRING"),
    ]


def availability_current_schema() -> list[bigquery.SchemaField]:
    return [
        bigquery.SchemaField("video_id", "STRING"),
        bigquery.SchemaField("status", "STRING"),
        bigquery.SchemaField("previous_status", "STRING"),
        bigquery.SchemaField("last_checked_at", "TIMESTAMP"),
        bigquery.SchemaField("last_seen_public_at", "TIMESTAMP"),
        bigquery.SchemaField("first_missing_at", "TIMESTAMP"),
        bigquery.SchemaField("confirmed_unavailable_at", "TIMESTAMP"),
        bigquery.SchemaField("consecutive_missing_count", "INT64"),
        bigquery.SchemaField("last_raw_status", "STRING"),
        bigquery.SchemaField("last_source", "STRING"),
        bigquery.SchemaField("last_run_id", "STRING"),
    ]


def availability_event_schema() -> list[bigquery.SchemaField]:
    return [
        bigquery.SchemaField("event_id", "STRING"),
        bigquery.SchemaField("event_date", "DATE"),
        bigquery.SchemaField("video_id", "STRING"),
        bigquery.SchemaField("event_at", "TIMESTAMP"),
        bigquery.SchemaField("previous_status", "STRING"),
        bigquery.SchemaField("new_status", "STRING"),
        bigquery.SchemaField("raw_status", "STRING"),
        bigquery.SchemaField("source", "STRING"),
        bigquery.SchemaField("run_id", "STRING"),
        bigquery.SchemaField("consecutive_missing_count", "INT64"),
        bigquery.SchemaField("last_seen_public_at", "TIMESTAMP"),
        bigquery.SchemaField("first_missing_at", "TIMESTAMP"),
    ]


def ensure_availability_tables(client: bigquery.Client) -> None:
    checks = bigquery.Table(
        f"{PROJECT_ID}.{DATASET}.{AVAILABILITY_CHECK_TABLE}",
        schema=availability_check_schema(),
    )
    checks.time_partitioning = bigquery.TimePartitioning(field="check_date")
    checks.clustering_fields = ["video_id", "raw_status"]
    client.create_table(checks, exists_ok=True)

    current = bigquery.Table(
        f"{PROJECT_ID}.{DATASET}.{AVAILABILITY_CURRENT_TABLE}",
        schema=availability_current_schema(),
    )
    current.clustering_fields = ["status", "video_id"]
    client.create_table(current, exists_ok=True)

    events = bigquery.Table(
        f"{PROJECT_ID}.{DATASET}.{AVAILABILITY_EVENT_TABLE}",
        schema=availability_event_schema(),
    )
    events.time_partitioning = bigquery.TimePartitioning(field="event_date")
    events.clustering_fields = ["video_id", "new_status"]
    client.create_table(events, exists_ok=True)


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


def timestamp_text(value: object) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def parse_iso8601_duration_seconds(value: str) -> float | None:
    if not value:
        return None
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        value,
    )
    if not match:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return float(days * 86400 + hours * 3600 + minutes * 60 + seconds)


if __name__ == "__main__":
    raise SystemExit(main())
