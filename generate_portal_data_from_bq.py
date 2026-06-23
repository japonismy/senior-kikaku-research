# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import base64
import csv
import json
import shutil
import subprocess
from pathlib import Path

try:
    from google.cloud import bigquery
except Exception:  # pragma: no cover - CLI fallback for minimal environments.
    bigquery = None


HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
REPORT_DIR = HERE / "reports"
OVERRIDE_PATH = HERE / "thumbnail_text_overrides.csv"
ANALYSIS_OVERRIDE_PATH = HERE / "thumbnail_analysis_overrides.jsonl"

PROJECT_ID = "rugged-destiny-408613"
DATASET = "senior_reading_all"
TRANSCRIPT_DIGEST_CHARS = 1600


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-id", default=PROJECT_ID)
    ap.add_argument("--dataset", default=DATASET)
    ap.add_argument("--limit", type=int, default=0, help="0 means no limit")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)

    client = bigquery.Client(project=args.project_id) if bigquery else None
    videos = fetch_videos(client, args.project_id, args.dataset, args.limit)
    transcript_rows = fetch_transcripts(client, args.project_id, args.dataset, [v["video_id"] for v in videos])

    thumbnail_overrides = read_thumbnail_overrides(OVERRIDE_PATH)
    analysis_overrides = read_analysis_overrides(ANALYSIS_OVERRIDE_PATH)
    missing = []

    for item in videos:
        vid = item["video_id"]
        if thumbnail_overrides.get(vid):
            item["thumbnail_text"] = thumbnail_overrides[vid]
        if analysis_overrides.get(vid):
            override = analysis_overrides[vid]
            item["thumbnail_text"] = item["thumbnail_text"] or compact_text(override.get("thumbnail_text", ""))
            item["thumbnail_analysis"] = {
                "main_subject": compact_text(override.get("main_subject", "")),
                "people": compact_text(override.get("people", "")),
                "setting": compact_text(override.get("setting", "")),
                "composition": compact_text(override.get("composition", "")),
                "emotion_appeal": compact_text(override.get("emotion_appeal", "")),
                "story_hook": compact_text(override.get("story_hook", "")),
            }
            item["tags"] = unique(
                item["tags"]
                + normalize_tags(override.get("pattern_tags", []))
                + normalize_tags(override.get("visual_tags", []))
                + normalize_tags(override.get("search_tags", []))
            )
        if not item["thumbnail_text"]:
            missing.append(
                {
                    "video_id": vid,
                    "channel": item["channel"],
                    "title": item["title"],
                    "view_count": item["view_count"],
                    "published_at": item["published_at"],
                    "fetched_at": item["fetched_at"],
                    "thumbnail_url": item["thumbnail_url"],
                }
            )

    selected = {v["video_id"] for v in videos}
    transcripts = [make_transcript_record(r) for r in transcript_rows if r["video_id"] in selected]

    write_js(DATA_DIR / "videos.js", "VIDEO_DATA", videos)
    write_js(DATA_DIR / "transcripts_light.js", "TRANSCRIPT_DATA", transcripts)
    write_missing_csv(REPORT_DIR / "thumbnail_text_missing.csv", missing)

    summary = {
        "source": "bigquery",
        "project_id": args.project_id,
        "dataset": args.dataset,
        "videos": len(videos),
        "videos_with_thumbnail_text": sum(1 for v in videos if v["thumbnail_text"]),
        "videos_missing_thumbnail_text": len(missing),
        "videos_with_gcs_thumbnail": sum(1 for v in videos if v.get("thumbnail_gcs_uri")),
        "transcripts_light": len(transcripts),
        "transcript_digest_chars_per_video": TRANSCRIPT_DIGEST_CHARS,
    }
    (REPORT_DIR / "build_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def fetch_videos(client, project_id: str, dataset: str, limit: int) -> list[dict]:
    limit_sql = "LIMIT @limit" if limit and client else f"LIMIT {int(limit)}" if limit else ""
    query = f"""
    SELECT
      v.video_id,
      v.channel_id,
      COALESCE(c.channel_name, c.channel_name_ja, c.handle, v.channel_id) AS channel,
      COALESCE(v.title, '') AS title,
      COALESCE(v.published_at, '') AS published_at,
      v.duration_sec,
      COALESCE(SAFE_CAST(v.view_count AS INT64), 0) AS view_count,
      COALESCE(SAFE_CAST(v.like_count AS INT64), 0) AS like_count,
      COALESCE(SAFE_CAST(v.comment_count AS INT64), 0) AS comment_count,
      COALESCE(v.thumbnail_url, '') AS thumbnail_url,
      COALESCE(v.tags, '') AS tags,
      COALESCE(v.fetched_at, '') AS fetched_at,
      COALESCE(a.gcs_uri, '') AS thumbnail_gcs_uri,
      COALESCE(o.combined_text, '') AS combined_text,
      COALESCE(o.emphasis_text, '') AS emphasis_text,
      COALESCE(o.narration_text, '') AS narration_text,
      COALESCE(o.dialogue_text, '') AS dialogue_text,
      COALESCE(o.top_upper_text, '') AS top_upper_text,
      COALESCE(o.top_lower_text, '') AS top_lower_text,
      COALESCE(o.center_text, '') AS center_text,
      COALESCE(o.bottom_upper_text, '') AS bottom_upper_text,
      COALESCE(o.bottom_lower_text, '') AS bottom_lower_text,
      TO_BASE64(CAST(COALESCE(o.raw_json, '') AS BYTES)) AS raw_json_b64,
      COALESCE(o.notes, '') AS ocr_notes,
      COALESCE(o.error, '') AS ocr_error,
      COALESCE(o.analyzed_at, '') AS ocr_analyzed_at
    FROM `{project_id}.{dataset}.analysis_competitor_db__videos` v
    JOIN `{project_id}.{dataset}.analysis_competitor_db__channels` c
      ON c.channel_id = v.channel_id
    LEFT JOIN `{project_id}.{dataset}.thumbnail_ocr_gemini` o
      ON o.video_id = v.video_id
    LEFT JOIN `{project_id}.{dataset}.thumbnail_assets` a
      ON a.video_id = v.video_id
      AND COALESCE(a.error, '') = ''
    WHERE COALESCE(v.thumbnail_url, '') != ''
      AND c.sync_target = 'senior_reading'
      AND COALESCE(c.include, 1) = 1
      AND COALESCE(c.source_type, '') != 'original_kr'
      AND (v.duration_sec IS NULL OR v.duration_sec >= 120)
    ORDER BY COALESCE(SAFE_CAST(v.view_count AS INT64), 0) DESC
    {limit_sql}
    """
    if client:
        job_config = None
        if limit:
            job_config = bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("limit", "INT64", limit)]
            )
        rows = [dict(row) for row in client.query(query, job_config=job_config).result()]
    else:
        rows = run_bq_query(project_id, query)
    return [make_video_record(row) for row in rows]


def fetch_transcripts(client, project_id: str, dataset: str, video_ids: list[str]) -> list[dict]:
    if not video_ids:
        return []
    selected = set(video_ids)
    if client:
        query = f"""
        SELECT video_id, transcript_text, language, source, fetched_at
        FROM `{project_id}.{dataset}.analysis_competitor_db__transcripts`
        WHERE video_id IN UNNEST(@video_ids)
          AND COALESCE(transcript_text, '') != ''
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[bigquery.ArrayQueryParameter("video_ids", "STRING", video_ids)]
        )
        return [dict(row) for row in client.query(query, job_config=job_config).result()]
    query = f"""
    SELECT video_id, transcript_text, language, source, fetched_at
    FROM `{project_id}.{dataset}.analysis_competitor_db__transcripts`
    WHERE COALESCE(transcript_text, '') != ''
    """
    return [row for row in run_bq_query(project_id, query) if row.get("video_id") in selected]


def run_bq_query(project_id: str, query: str) -> list[dict]:
    bq = shutil.which("bq") or shutil.which("bq.cmd") or shutil.which("bq.exe")
    if not bq:
        raise SystemExit("bq CLI was not found on PATH.")
    proc = subprocess.run(
        [
            bq,
            "--project_id",
            project_id,
            "query",
            "--use_legacy_sql=false",
            "--format=prettyjson",
        ],
        cwd=HERE,
        input=query,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if proc.returncode:
        raise SystemExit((proc.stderr or proc.stdout).strip())
    text = (proc.stdout or "").strip()
    return json.loads(text) if text else []


def make_video_record(row: dict) -> dict:
    vid = row["video_id"]
    raw_json = parse_json_object(decode_b64(row.get("raw_json_b64", "")))
    composition = raw_json.get("composition_analysis") if isinstance(raw_json, dict) else {}
    thumbnail_text = clean_join(
        [
            row.get("combined_text"),
            row.get("emphasis_text"),
            row.get("narration_text"),
            row.get("dialogue_text"),
            row.get("top_upper_text"),
            row.get("top_lower_text"),
            row.get("center_text"),
            row.get("bottom_upper_text"),
            row.get("bottom_lower_text"),
        ]
    )
    return {
        "video_id": vid,
        "channel_id": row.get("channel_id") or "",
        "channel": row.get("channel") or "",
        "title": row.get("title") or "",
        "published_at": row.get("published_at") or "",
        "duration_sec": row.get("duration_sec"),
        "view_count": row.get("view_count") or 0,
        "like_count": row.get("like_count") or 0,
        "comment_count": row.get("comment_count") or 0,
        "thumbnail_url": row.get("thumbnail_url") or "",
        "thumbnail_max_url": f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
        "thumbnail_fallback_urls": [
            f"https://i.ytimg.com/vi/{vid}/sddefault.jpg",
            f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            row.get("thumbnail_url") or "",
        ],
        "thumbnail_gcs_uri": row.get("thumbnail_gcs_uri") or "",
        "youtube_url": f"https://www.youtube.com/watch?v={vid}",
        "fetched_at": row.get("fetched_at") or "",
        "thumbnail_text": thumbnail_text,
        "thumbnail_ocr": {
            "analyzed_at": row.get("ocr_analyzed_at") or "",
            "notes": row.get("ocr_notes") or "",
            "error": row.get("ocr_error") or "",
        },
        "thumbnail_analysis": {
            "main_subject": compact_text(composition.get("main_subject", "")) if isinstance(composition, dict) else "",
            "people": str(composition.get("character_count", "")) if isinstance(composition, dict) else "",
            "setting": compact_text(composition.get("layout", "")) if isinstance(composition, dict) else "",
            "composition": compact_text(composition.get("speech_bubbles", "")) if isinstance(composition, dict) else "",
            "emotion_appeal": compact_text(composition.get("emotion", "")) if isinstance(composition, dict) else "",
            "story_hook": compact_text(composition.get("hook", "")) if isinstance(composition, dict) else "",
        },
        "tags": unique(parse_tags(row.get("tags"))),
    }


def make_transcript_record(row: dict) -> dict:
    text = compact_text(row.get("transcript_text", ""))
    return {
        "video_id": row.get("video_id", ""),
        "digest": text[:TRANSCRIPT_DIGEST_CHARS],
        "chars": len(text),
        "language": row.get("language") or "",
        "source": row.get("source") or "",
        "fetched_at": row.get("fetched_at") or "",
    }


def clean_join(values: list[object]) -> str:
    seen = []
    for value in values:
        text = compact_text(value or "")
        if text and text not in seen:
            seen.append(text)
    return " ".join(seen)


def compact_text(text: object) -> str:
    return " ".join(str(text or "").replace("\r", "\n").split())


def parse_json_object(value: object) -> dict:
    text = compact_text(value)
    if not text:
        return {}


def decode_b64(value: object) -> str:
    text = compact_text(value)
    if not text:
        return ""
    try:
        return base64.b64decode(text).decode("utf-8", "replace")
    except Exception:
        return ""
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def parse_tags(value: object) -> list[str]:
    text = compact_text(value)
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [compact_text(x) for x in parsed if compact_text(x)]
    except Exception:
        pass
    return [compact_text(x) for x in text.replace("・", ",").split(",") if compact_text(x)]


def normalize_tags(value: object) -> list[str]:
    if isinstance(value, list):
        return [compact_text(x) for x in value if compact_text(x)]
    return parse_tags(value)


def unique(values: list[str]) -> list[str]:
    out = []
    seen = set()
    for value in values:
        v = compact_text(value)
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def write_js(path: Path, name: str, data: object) -> None:
    path.write_text(
        f"window.{name} = "
        + json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )


def write_missing_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["video_id", "channel", "title", "view_count", "published_at", "fetched_at", "thumbnail_url"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_thumbnail_overrides(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            video_id = compact_text(row.get("video_id", ""))
            text = compact_text(row.get("thumbnail_text", ""))
            if video_id and text:
                out[video_id] = text
    return out


def read_analysis_overrides(path: Path) -> dict[str, dict]:
    out = {}
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            video_id = compact_text(row.get("video_id", ""))
            if video_id:
                out[video_id] = row
    return out


if __name__ == "__main__":
    raise SystemExit(main())
