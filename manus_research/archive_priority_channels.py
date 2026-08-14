# -*- coding: utf-8 -*-
"""Archive priority YouTube channels and detect availability transitions."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from google.api_core.exceptions import NotFound
from google.cloud import bigquery


PROJECT_ID = "rugged-destiny-408613"
DATASET = "senior_reading_all"
SOURCE_VIDEOS = f"{PROJECT_ID}.{DATASET}.analysis_competitor_db__videos"
SOURCE_CHANNELS = f"{PROJECT_ID}.{DATASET}.analysis_competitor_db__channels"
SOURCE_TRANSCRIPTS = f"{PROJECT_ID}.{DATASET}.analysis_competitor_db__transcripts"
HISTORY_TABLE = f"{PROJECT_ID}.{DATASET}.priority_channel_video_snapshots_history"
CURRENT_TABLE = f"{PROJECT_ID}.{DATASET}.priority_channel_videos_current_v1"
EVENTS_TABLE = f"{PROJECT_ID}.{DATASET}.priority_channel_availability_events"
LATEST_VIEW = f"{PROJECT_ID}.{DATASET}.priority_channel_videos_latest_v1"
CHANGES_VIEW = f"{PROJECT_ID}.{DATASET}.priority_channel_availability_changes_v1"

VAULT = Path(r"E:\Data\ObsidianVault")
TOOLS = VAULT / "04_Tools"
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "priority_archive_channels.json"
DEFAULT_ARCHIVE_ROOT = (
    VAULT / "02_Channels" / "シニア朗読" / "競合リサーチ" / "アーカイブ" / "重点チャンネル"
)
DEFAULT_REPORT_ROOT = VAULT / "02_Channels" / "シニア朗読" / "analysis" / "archive_monitor"
RECOVERY_THUMBNAIL_ROOTS = [
    VAULT / "02_Channels" / "シニア朗読" / "analysis" / "thumbnails" / "人生は贈り物",
]


SNAPSHOT_SCHEMA = [
    bigquery.SchemaField("snapshot_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("snapshot_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("snapshot_date", "DATE", mode="REQUIRED"),
    bigquery.SchemaField("channel_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("channel_name", "STRING"),
    bigquery.SchemaField("priority", "STRING"),
    bigquery.SchemaField("video_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("title", "STRING"),
    bigquery.SchemaField("published_at", "STRING"),
    bigquery.SchemaField("duration_sec", "INTEGER"),
    bigquery.SchemaField("view_count", "INTEGER"),
    bigquery.SchemaField("like_count", "INTEGER"),
    bigquery.SchemaField("comment_count", "INTEGER"),
    bigquery.SchemaField("description", "STRING"),
    bigquery.SchemaField("tags_json", "STRING"),
    bigquery.SchemaField("thumbnail_url", "STRING"),
    bigquery.SchemaField("privacy_status", "STRING"),
    bigquery.SchemaField("availability", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("in_public_uploads", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("metadata_available", "BOOLEAN", mode="REQUIRED"),
    bigquery.SchemaField("source_fetched_at", "STRING"),
    bigquery.SchemaField("transcript_chars", "INTEGER"),
    bigquery.SchemaField("archive_dir", "STRING"),
    bigquery.SchemaField("thumbnail_path", "STRING"),
    bigquery.SchemaField("transcript_path", "STRING"),
    bigquery.SchemaField("metadata_fingerprint", "STRING"),
]

EVENT_SCHEMA = [
    bigquery.SchemaField("event_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("detected_at", "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("snapshot_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("previous_snapshot_id", "STRING"),
    bigquery.SchemaField("channel_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("channel_name", "STRING"),
    bigquery.SchemaField("video_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("title", "STRING"),
    bigquery.SchemaField("event_type", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("previous_availability", "STRING"),
    bigquery.SchemaField("current_availability", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("archive_dir", "STRING"),
]


def chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def api_get(path: str, params: dict[str, str], api_key: str) -> dict[str, Any]:
    values = dict(params)
    values["key"] = api_key
    url = f"https://www.googleapis.com/youtube/v3/{path}?{urllib.parse.urlencode(values)}"
    request = urllib.request.Request(url, headers={"User-Agent": "senior-research-archive/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"YouTube API {path} failed: HTTP {exc.code}: {body[:800]}") from exc


def fetch_channel(channel_id: str, api_key: str) -> dict[str, Any]:
    response = api_get(
        "channels", {"part": "snippet,contentDetails,statistics", "id": channel_id}, api_key
    )
    items = response.get("items") or []
    if not items:
        raise RuntimeError(
            f"YouTube API returned no channel for {channel_id}; availability was not changed"
        )
    return items[0]


def fetch_public_upload_ids(playlist_id: str, api_key: str, limit: int) -> list[str]:
    output: list[str] = []
    token = ""
    while len(output) < limit:
        params = {
            "part": "contentDetails",
            "playlistId": playlist_id,
            "maxResults": str(min(50, limit - len(output))),
        }
        if token:
            params["pageToken"] = token
        response = api_get("playlistItems", params, api_key)
        output.extend(
            item.get("contentDetails", {}).get("videoId", "")
            for item in response.get("items", [])
        )
        output = [value for value in output if value]
        token = response.get("nextPageToken") or ""
        if not token:
            break
    return list(dict.fromkeys(output))


def parse_duration(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not match:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def best_thumbnail(snippet: dict[str, Any]) -> str:
    thumbs = snippet.get("thumbnails") or {}
    for name in ("maxres", "standard", "high", "medium", "default"):
        if (thumbs.get(name) or {}).get("url"):
            return str(thumbs[name]["url"])
    return ""


def fetch_video_details(video_ids: list[str], api_key: str) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for batch in chunks(video_ids, 50):
        response = api_get(
            "videos",
            {
                "part": "snippet,statistics,contentDetails,status",
                "id": ",".join(batch),
                "maxResults": str(len(batch)),
            },
            api_key,
        )
        for item in response.get("items", []):
            snippet = item.get("snippet") or {}
            statistics = item.get("statistics") or {}
            content = item.get("contentDetails") or {}
            status = item.get("status") or {}
            output[item["id"]] = {
                "video_id": item["id"],
                "channel_id": snippet.get("channelId"),
                "title": snippet.get("title"),
                "published_at": snippet.get("publishedAt"),
                "duration_sec": parse_duration(content.get("duration")),
                "view_count": int(statistics["viewCount"]) if "viewCount" in statistics else None,
                "like_count": int(statistics["likeCount"]) if "likeCount" in statistics else None,
                "comment_count": int(statistics["commentCount"]) if "commentCount" in statistics else None,
                "description": snippet.get("description"),
                "tags_json": json.dumps(snippet.get("tags") or [], ensure_ascii=False),
                "thumbnail_url": best_thumbnail(snippet),
                "privacy_status": status.get("privacyStatus"),
            }
    return output


def load_config(path: Path) -> list[dict[str, str]]:
    channels = json.loads(path.read_text(encoding="utf-8")).get("channels") or []
    ids = [item.get("channel_id") for item in channels]
    if not ids or any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("priority archive config needs unique channel_id values")
    return channels


def query_source_rows(client: bigquery.Client, channel_ids: list[str]) -> dict[str, dict[str, Any]]:
    query = f"""
    SELECT v.video_id, v.channel_id, c.channel_name, v.title, v.published_at,
           CAST(v.duration_sec AS INT64) duration_sec,
           CAST(v.view_count AS INT64) view_count,
           CAST(v.like_count AS INT64) like_count,
           CAST(v.comment_count AS INT64) comment_count,
           v.description, v.tags AS tags_json, v.thumbnail_url,
           v.fetched_at AS source_fetched_at,
           t.transcript_text, t.fetched_at AS transcript_fetched_at
    FROM `{SOURCE_VIDEOS}` v
    LEFT JOIN `{SOURCE_CHANNELS}` c USING(channel_id)
    LEFT JOIN `{SOURCE_TRANSCRIPTS}` t USING(video_id)
    WHERE v.channel_id IN UNNEST(@channel_ids)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("channel_ids", "STRING", channel_ids)]
    )
    return {row.video_id: dict(row) for row in client.query(query, job_config=job_config).result()}


def ensure_table(client: bigquery.Client, table_id: str, schema: list[bigquery.SchemaField]) -> None:
    try:
        client.get_table(table_id)
    except NotFound:
        client.create_table(bigquery.Table(table_id, schema=schema))


def previous_rows(
    client: bigquery.Client, channel_ids: list[str]
) -> tuple[dict[tuple[str, str], dict[str, Any]], bool]:
    ensure_table(client, HISTORY_TABLE, SNAPSHOT_SCHEMA)
    query = f"""
    SELECT * FROM `{HISTORY_TABLE}`
    WHERE channel_id IN UNNEST(@channel_ids)
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY channel_id, video_id ORDER BY snapshot_at DESC
    ) = 1
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("channel_ids", "STRING", channel_ids)]
    )
    rows = [dict(row) for row in client.query(query, job_config=job_config).result()]
    return {(row["channel_id"], row["video_id"]): row for row in rows}, bool(rows)


def download_thumbnail(video_id: str, preferred_url: str, output: Path) -> tuple[str, str]:
    if output.exists() and output.stat().st_size > 1024:
        return "existing", str(output)
    for recovery_root in RECOVERY_THUMBNAIL_ROOTS:
        if not recovery_root.is_dir():
            continue
        candidates = sorted(
            path
            for path in recovery_root.rglob(f"*{video_id}*")
            if path.is_file()
            and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            and path.stat().st_size > 1024
        )
        if candidates:
            shutil.copy2(candidates[0], output)
            return f"recovered_from_vault:{candidates[0]}", str(output)
    urls = [preferred_url] if preferred_url else []
    urls.extend(
        f"https://i.ytimg.com/vi/{video_id}/{name}"
        for name in ("maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg", "0.jpg")
    )
    errors: list[str] = []
    for url in dict.fromkeys(value for value in urls if value):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
            if len(data) < 1024:
                errors.append(f"too_small:{url}")
                continue
            temporary = output.with_suffix(output.suffix + ".tmp")
            temporary.write_bytes(data)
            temporary.replace(output)
            return "downloaded", str(output)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}:{url}")
    return "failed:" + ";".join(errors[:4]), ""


def fingerprint(row: dict[str, Any]) -> str:
    stable = {
        key: row.get(key)
        for key in (
            "title", "published_at", "duration_sec", "description", "tags_json",
            "thumbnail_url", "privacy_status", "availability",
        )
    }
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


def archive_row(
    row: dict[str, Any], transcript: str, archive_root: Path, snapshot_stamp: str
) -> dict[str, Any]:
    video_dir = archive_root / row["channel_id"] / row["video_id"]
    video_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_status, thumbnail_path = download_thumbnail(
        row["video_id"], row.get("thumbnail_url") or "", video_dir / "thumbnail.jpg"
    )
    transcript_path = video_dir / "transcript.txt"
    if transcript and (not transcript_path.exists() or transcript_path.stat().st_size < 1000):
        transcript_path.write_text(transcript, encoding="utf-8")
    row["archive_dir"] = str(video_dir)
    row["thumbnail_path"] = thumbnail_path
    row["transcript_path"] = str(transcript_path) if transcript_path.exists() else ""
    row["transcript_chars"] = len(transcript or "")
    row["metadata_fingerprint"] = fingerprint(row)

    latest_path = video_dir / "metadata_latest.json"
    previous_fingerprint = ""
    if latest_path.exists():
        try:
            previous_fingerprint = json.loads(latest_path.read_text(encoding="utf-8")).get(
                "metadata_fingerprint", ""
            )
        except (OSError, ValueError):
            previous_fingerprint = ""
    write_json(latest_path, row)
    if row["metadata_fingerprint"] != previous_fingerprint:
        write_json(video_dir / "metadata_snapshots" / f"{snapshot_stamp}.json", row)
    row["thumbnail_archive_status"] = thumbnail_status
    return row


def build_events(
    rows: list[dict[str, Any]],
    previous: dict[tuple[str, str], dict[str, Any]],
    has_history: bool,
    event_video_ids: set[str],
    detected_at: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in rows:
        old = previous.get((row["channel_id"], row["video_id"]))
        old_availability = old.get("availability") if old else None
        current = row["availability"]
        if current != "public" and row["video_id"] not in event_video_ids and (
            not has_history or old_availability == current
        ):
            event_type = "baseline_unavailable_detected"
        elif not has_history:
            continue
        elif old is None and current == "public":
            event_type = "new_public_video"
        elif old_availability == current:
            continue
        elif old_availability == "public" and current != "public":
            event_type = "public_became_unavailable"
        elif old_availability != "public" and current == "public":
            event_type = "became_public"
        else:
            event_type = "availability_changed"
        raw_id = f"{row['snapshot_id']}:{row['channel_id']}:{row['video_id']}:{event_type}"
        events.append(
            {
                "event_id": hashlib.sha256(raw_id.encode("utf-8")).hexdigest(),
                "detected_at": detected_at,
                "snapshot_id": row["snapshot_id"],
                "previous_snapshot_id": old.get("snapshot_id") if old else None,
                "channel_id": row["channel_id"],
                "channel_name": row.get("channel_name"),
                "video_id": row["video_id"],
                "title": row.get("title"),
                "event_type": event_type,
                "previous_availability": old_availability,
                "current_availability": current,
                "archive_dir": row.get("archive_dir"),
            }
        )
    return events


def existing_event_video_ids(client: bigquery.Client, channel_ids: list[str]) -> set[str]:
    ensure_table(client, EVENTS_TABLE, EVENT_SCHEMA)
    query = f"""
    SELECT DISTINCT video_id FROM `{EVENTS_TABLE}`
    WHERE channel_id IN UNNEST(@channel_ids)
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ArrayQueryParameter("channel_ids", "STRING", channel_ids)]
    )
    return {row.video_id for row in client.query(query, job_config=job_config).result()}


def save_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [field.name for field in SNAPSHOT_SCHEMA]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def save_bigquery(
    client: bigquery.Client, rows: list[dict[str, Any]], events: list[dict[str, Any]]
) -> None:
    ensure_table(client, HISTORY_TABLE, SNAPSHOT_SCHEMA)
    ensure_table(client, EVENTS_TABLE, EVENT_SCHEMA)
    clean_rows = [{field.name: row.get(field.name) for field in SNAPSHOT_SCHEMA} for row in rows]
    client.load_table_from_json(
        clean_rows,
        CURRENT_TABLE,
        job_config=bigquery.LoadJobConfig(schema=SNAPSHOT_SCHEMA, write_disposition="WRITE_TRUNCATE"),
    ).result()
    client.load_table_from_json(
        clean_rows,
        HISTORY_TABLE,
        job_config=bigquery.LoadJobConfig(schema=SNAPSHOT_SCHEMA, write_disposition="WRITE_APPEND"),
    ).result()
    if events:
        client.load_table_from_json(
            events,
            EVENTS_TABLE,
            job_config=bigquery.LoadJobConfig(schema=EVENT_SCHEMA, write_disposition="WRITE_APPEND"),
        ).result()
    client.query(
        f"""
        CREATE OR REPLACE VIEW `{LATEST_VIEW}` AS
        SELECT * FROM `{HISTORY_TABLE}`
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY channel_id, video_id ORDER BY snapshot_at DESC
        ) = 1
        """
    ).result()
    client.query(
        f"CREATE OR REPLACE VIEW `{CHANGES_VIEW}` AS SELECT * FROM `{EVENTS_TABLE}`"
    ).result()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--archive-root", default=str(DEFAULT_ARCHIVE_ROOT))
    parser.add_argument("--report-root", default=str(DEFAULT_REPORT_ROOT))
    parser.add_argument("--api-key-name", default="clinelabo")
    parser.add_argument("--max-public-videos", type=int, default=200)
    args = parser.parse_args()

    if str(TOOLS) not in sys.path:
        sys.path.insert(0, str(TOOLS))
    from core.config import Config  # noqa: PLC0415

    config = Config(VAULT)
    api_key = config.get_youtube_api_key(args.api_key_name)
    if not api_key and args.api_key_name == "default":
        api_key = config.youtube_api_key
    if not api_key:
        raise RuntimeError(f"YouTube API key '{args.api_key_name}' is not configured")

    channels = load_config(Path(args.config))
    channel_ids = [item["channel_id"] for item in channels]
    archive_root = Path(args.archive_root)
    report_root = Path(args.report_root)
    archive_root.mkdir(parents=True, exist_ok=True)
    report_root.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    snapshot_id = now.strftime("%Y%m%dT%H%M%SZ")
    snapshot_at = now.isoformat()
    snapshot_date = now.date().isoformat()
    client = bigquery.Client(project=PROJECT_ID)
    source = query_source_rows(client, channel_ids)
    previous, has_history = previous_rows(client, channel_ids)
    event_video_ids = existing_event_video_ids(client, channel_ids)

    rows: list[dict[str, Any]] = []
    channel_reports: list[dict[str, Any]] = []
    for channel_config in channels:
        channel_id = channel_config["channel_id"]
        channel_item = fetch_channel(channel_id, api_key)
        snippet = channel_item.get("snippet") or {}
        uploads = channel_item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
        if not uploads:
            raise RuntimeError(f"uploads playlist not found for {channel_id}; no statuses were changed")
        public_ids = fetch_public_upload_ids(uploads, api_key, args.max_public_videos)
        known_ids = sorted(
            set(public_ids)
            | {video_id for video_id, item in source.items() if item.get("channel_id") == channel_id}
            | {video_id for old_channel, video_id in previous if old_channel == channel_id}
        )
        details = fetch_video_details(known_ids, api_key)
        public_set = set(public_ids)
        transcript_recovery = {
            item["video_id"]: Path(item["path"])
            for item in channel_config.get("transcript_recovery_files", [])
            if item.get("video_id") and item.get("path")
        }
        channel_rows: list[dict[str, Any]] = []
        for video_id in known_ids:
            stored = source.get(video_id) or {}
            old = previous.get((channel_id, video_id), {})
            live = details.get(video_id) or {}
            metadata_available = bool(live)
            privacy = live.get("privacy_status") or ""
            if video_id in public_set and metadata_available:
                availability = "public"
            elif metadata_available:
                availability = privacy or "available_not_in_public_uploads"
            else:
                availability = "unavailable_or_private"
            transcript = stored.get("transcript_text") or ""
            recovery_path = transcript_recovery.get(video_id)
            if not transcript and recovery_path and recovery_path.is_file():
                transcript = recovery_path.read_text(encoding="utf-8")
            row = {
                "snapshot_id": snapshot_id,
                "snapshot_at": snapshot_at,
                "snapshot_date": snapshot_date,
                "channel_id": channel_id,
                "channel_name": snippet.get("title") or channel_config.get("channel_name") or stored.get("channel_name"),
                "priority": channel_config.get("priority") or "hot",
                "video_id": video_id,
                "title": live.get("title") or stored.get("title") or old.get("title"),
                "published_at": live.get("published_at") or stored.get("published_at") or old.get("published_at"),
                "duration_sec": live.get("duration_sec") if live.get("duration_sec") is not None else stored.get("duration_sec"),
                "view_count": live.get("view_count") if live.get("view_count") is not None else stored.get("view_count"),
                "like_count": live.get("like_count") if live.get("like_count") is not None else stored.get("like_count"),
                "comment_count": live.get("comment_count") if live.get("comment_count") is not None else stored.get("comment_count"),
                "description": live.get("description") or stored.get("description") or old.get("description"),
                "tags_json": live.get("tags_json") or stored.get("tags_json") or old.get("tags_json"),
                "thumbnail_url": live.get("thumbnail_url") or stored.get("thumbnail_url") or old.get("thumbnail_url"),
                "privacy_status": privacy,
                "availability": availability,
                "in_public_uploads": video_id in public_set,
                "metadata_available": metadata_available,
                "source_fetched_at": stored.get("source_fetched_at"),
            }
            archive_row(row, transcript, archive_root, snapshot_id)
            channel_rows.append(row)
        rows.extend(channel_rows)
        channel_reports.append(
            {
                "channel_id": channel_id,
                "channel_name": snippet.get("title"),
                "known_videos": len(known_ids),
                "public_videos": sum(row["availability"] == "public" for row in channel_rows),
                "unavailable_or_private": sum(
                    row["availability"] == "unavailable_or_private" for row in channel_rows
                ),
                "transcripts_archived": sum(bool(row["transcript_path"]) for row in channel_rows),
                "thumbnails_archived": sum(bool(row["thumbnail_path"]) for row in channel_rows),
            }
        )

    rows.sort(
        key=lambda item: (item["channel_id"], item.get("published_at") or "", item["video_id"]),
        reverse=True,
    )
    events = build_events(rows, previous, has_history, event_video_ids, snapshot_at)
    save_bigquery(client, rows, events)
    save_csv(archive_root / "重点チャンネル_最新状態.csv", rows)
    write_json(archive_root / "重点チャンネル_最新状態.json", rows)
    write_json(archive_root / "snapshots" / f"{snapshot_id}.json", rows)
    report = {
        "snapshot_id": snapshot_id,
        "snapshot_at": snapshot_at,
        "config": str(Path(args.config)),
        "archive_root": str(archive_root),
        "source_video_count": len(source),
        "previous_history_found": has_history,
        "channel_reports": channel_reports,
        "event_count": len(events),
        "events": events,
        "bigquery": {
            "current": CURRENT_TABLE,
            "history": HISTORY_TABLE,
            "events": EVENTS_TABLE,
            "latest_view": LATEST_VIEW,
            "changes_view": CHANGES_VIEW,
        },
    }
    report_path = report_root / f"priority_archive_{snapshot_id}.json"
    write_json(report_path, report)
    print(
        json.dumps(
            {**report, "report_path": str(report_path)}, ensure_ascii=False, indent=2, default=str
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
