# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


HERE = Path(__file__).resolve().parent
VAULT_ROOT = Path(r"c:\Data\ObsidianVault")
TOOLS_DIR = VAULT_ROOT / "04_Tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from core.config import Config  # noqa: E402


DB_PATH = VAULT_ROOT / "02_Channels" / "シニア朗読" / "analysis" / "competitor_db.sqlite"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cfg = Config()
    keys = list((cfg.youtube_api_keys or {}).items())
    if cfg.youtube_api_key and not any(k == "default" for k, _ in keys):
        keys.insert(0, ("default", cfg.youtube_api_key))
    keys = [(name, key) for name, key in keys if key]
    if not keys:
        raise SystemExit("YOUTUBE_API_KEY is not configured.")

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    ids = [r["video_id"] for r in con.execute(target_sql()).fetchall()]
    if args.limit:
        ids = ids[: args.limit]

    updated = missing = quota_skips = 0
    key_index = 0
    for batch in chunks(ids, 50):
        data = None
        while key_index < len(keys):
            key_name, key = keys[key_index]
            try:
                data = fetch_videos(key, batch)
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code == 403 and "quotaExceeded" in body:
                    quota_skips += 1
                    key_index += 1
                    continue
                raise
        if data is None:
            raise SystemExit("All YouTube API keys appear to be quota-exceeded.")
        items = {item["id"]: item for item in data.get("items", [])}
        for vid in batch:
            item = items.get(vid)
            if not item:
                missing += 1
                continue
            update_row(con, vid, item)
            updated += 1
        con.commit()
    print(json.dumps({"target": len(ids), "updated": updated, "missing": missing, "quota_skips": quota_skips, "key_used": keys[key_index][0] if key_index < len(keys) else ""}, ensure_ascii=False))
    return 0


def target_sql() -> str:
    return """
    SELECT v.video_id
    FROM videos v
    JOIN channels c ON c.channel_id = v.channel_id
    WHERE v.thumbnail_url IS NOT NULL
      AND v.thumbnail_url != ''
      AND c.sync_target = 'senior_reading'
      AND COALESCE(c.include, 1) = 1
      AND COALESCE(c.source_type, '') != 'original_kr'
      AND (v.duration_sec IS NULL OR v.duration_sec >= 120)
    ORDER BY COALESCE(v.view_count, 0) DESC
    """


def fetch_videos(key: str, ids: list[str]) -> dict:
    params = {
        "part": "snippet,statistics,contentDetails",
        "id": ",".join(ids),
        "key": key,
        "maxResults": "50",
    }
    url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def update_row(con: sqlite3.Connection, vid: str, item: dict) -> None:
    snippet = item.get("snippet", {})
    stats = item.get("statistics", {})
    content = item.get("contentDetails", {})
    thumbs = snippet.get("thumbnails", {})
    thumbnail_url = best_thumbnail_url(thumbs)
    con.execute(
        """
        UPDATE videos
        SET title = ?,
            published_at = ?,
            view_count = ?,
            like_count = ?,
            comment_count = ?,
            thumbnail_url = COALESCE(?, thumbnail_url),
            duration = ?,
            fetched_at = ?
        WHERE video_id = ?
        """,
        (
            snippet.get("title", ""),
            snippet.get("publishedAt", ""),
            int(stats.get("viewCount", 0) or 0),
            int(stats.get("likeCount", 0) or 0),
            int(stats.get("commentCount", 0) or 0),
            thumbnail_url,
            content.get("duration", ""),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            vid,
        ),
    )


def best_thumbnail_url(thumbs: dict) -> str:
    for key in ["maxres", "standard", "high", "medium", "default"]:
        if key in thumbs and thumbs[key].get("url"):
            return thumbs[key]["url"]
    return ""


def chunks(values: list[str], size: int):
    for i in range(0, len(values), size):
        yield values[i:i + size]


if __name__ == "__main__":
    raise SystemExit(main())
