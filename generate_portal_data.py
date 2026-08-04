# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path


HERE = Path(__file__).resolve().parent
ANALYSIS_DIR = HERE.parents[1] / "analysis"
DB_PATH = ANALYSIS_DIR / "competitor_db.sqlite"
DATA_DIR = HERE / "data"
REPORT_DIR = HERE / "reports"
OVERRIDE_PATH = HERE / "thumbnail_text_overrides.csv"
ANALYSIS_OVERRIDE_PATH = HERE / "thumbnail_analysis_overrides.jsonl"

TRANSCRIPT_DIGEST_CHARS = 1600


def main() -> int:
    DATA_DIR.mkdir(exist_ok=True)
    REPORT_DIR.mkdir(exist_ok=True)

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row

    channel_meta = {
        r["channel_id"]: {
            "name": r["channel_name"] or r["channel_name_ja"] or r["handle"] or r["channel_id"],
            "group": r["channel_group"] or "",
            "group_role": r["group_role"] or "",
            "group_parent_channel_id": r["group_parent_channel_id"] or "",
            "genre_tag": r["genre_tag"] or "",
        }
        for r in con.execute(
            """
            SELECT channel_id, channel_name, channel_name_ja, handle,
                   channel_group, group_role, group_parent_channel_id, genre_tag
            FROM channels
            """
        )
    }
    thumbnail_text = {
        r["video_id"]: clean_join(
            [
                r["combined_text"],
                r["emphasis_text"],
                r["narration_text"],
                r["dialogue_text"],
                r["top_upper_text"],
                r["top_lower_text"],
                r["center_text"],
                r["bottom_upper_text"],
                r["bottom_lower_text"],
            ]
        )
        for r in con.execute(
            """
            SELECT video_id, combined_text, emphasis_text, narration_text,
                   dialogue_text, top_upper_text, top_lower_text, center_text,
                   bottom_upper_text, bottom_lower_text
            FROM thumbnail_ocr
            """
        )
    }
    thumbnail_text.update(read_thumbnail_overrides(OVERRIDE_PATH))
    analysis_map = read_analysis_overrides(ANALYSIS_OVERRIDE_PATH)
    tag_map: dict[str, list[str]] = {}
    for r in con.execute(
        """
        SELECT video_id, axis, code
        FROM thumbnail_axis_tags
        WHERE code IS NOT NULL AND code != ''
        """
    ):
        tag = f"{r['axis']}:{r['code']}" if r["axis"] else r["code"]
        tag_map.setdefault(r["video_id"], []).append(tag)

    videos = []
    missing = []
    rows = con.execute(
        """
        SELECT v.video_id, v.channel_id, v.title, v.published_at, v.duration_sec,
               v.view_count, v.like_count, v.comment_count, v.thumbnail_url,
               v.tags, v.fetched_at
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
    ).fetchall()

    for r in rows:
        vid = r["video_id"]
        meta = channel_meta.get(
            r["channel_id"],
            {
                "name": r["channel_id"] or "",
                "group": "",
                "group_role": "",
                "group_parent_channel_id": "",
                "genre_tag": "",
            },
        )
        raw_tags = parse_tags(r["tags"])
        analysis_tags = unique(tag_map.get(vid, []))
        thumb_analysis = analysis_map.get(vid, {})
        generated_tags = []
        for key in ["pattern_tags", "visual_tags", "search_tags"]:
            generated_tags.extend(thumb_analysis.get(key, []))
        item = {
            "video_id": vid,
            "channel_id": r["channel_id"],
            "channel": meta["name"],
            "channel_group": meta["group"],
            "group_role": meta["group_role"],
            "group_parent_channel_id": meta["group_parent_channel_id"],
            "genre_tag": meta["genre_tag"],
            "title": r["title"] or "",
            "published_at": r["published_at"] or "",
            "duration_sec": r["duration_sec"],
            "view_count": r["view_count"] or 0,
            "like_count": r["like_count"] or 0,
            "comment_count": r["comment_count"] or 0,
            "thumbnail_url": r["thumbnail_url"] or "",
            "thumbnail_max_url": f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
            "thumbnail_fallback_urls": [
                f"https://i.ytimg.com/vi/{vid}/sddefault.jpg",
                f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                r["thumbnail_url"] or "",
            ],
            "youtube_url": f"https://www.youtube.com/watch?v={vid}",
            "fetched_at": r["fetched_at"] or "",
            "thumbnail_text": thumbnail_text.get(vid, "") or thumb_analysis.get("thumbnail_text", ""),
            "thumbnail_analysis": {
                "main_subject": thumb_analysis.get("main_subject", ""),
                "people": thumb_analysis.get("people", ""),
                "setting": thumb_analysis.get("setting", ""),
                "composition": thumb_analysis.get("composition", ""),
                "emotion_appeal": thumb_analysis.get("emotion_appeal", ""),
                "story_hook": thumb_analysis.get("story_hook", ""),
            },
            "tags": unique(raw_tags + analysis_tags + generated_tags),
        }
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
        videos.append(item)

    selected_video_ids = {v["video_id"] for v in videos}
    transcript_rows = con.execute(
        """
        SELECT video_id, transcript_text, language, source, fetched_at
        FROM transcripts
        WHERE transcript_text IS NOT NULL AND transcript_text != ''
        """
    ).fetchall()
    transcripts = [
        make_transcript_record(r)
        for r in transcript_rows
        if r["video_id"] in selected_video_ids
    ]

    write_js(DATA_DIR / "videos.js", "VIDEO_DATA", videos)
    write_js(DATA_DIR / "transcripts_light.js", "TRANSCRIPT_DATA", transcripts)
    write_missing_csv(REPORT_DIR / "thumbnail_text_missing.csv", missing)

    summary = {
        "videos": len(videos),
        "videos_with_thumbnail_text": sum(1 for v in videos if v["thumbnail_text"]),
        "videos_missing_thumbnail_text": len(missing),
        "transcripts_light": len(transcripts),
        "transcript_digest_chars_per_video": TRANSCRIPT_DIGEST_CHARS,
    }
    (REPORT_DIR / "build_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def make_transcript_record(r: sqlite3.Row) -> dict[str, object]:
    text = compact_text(r["transcript_text"])
    return {
        "video_id": r["video_id"],
        "digest": text[:TRANSCRIPT_DIGEST_CHARS],
        "chars": len(text),
        "language": r["language"] or "",
        "source": r["source"] or "",
        "fetched_at": r["fetched_at"] or "",
    }


def clean_join(values: list[str | None]) -> str:
    seen = []
    for value in values:
        text = compact_text(value or "")
        if text and text not in seen:
            seen.append(text)
    return " ".join(seen)


def compact_text(text: str) -> str:
    return " ".join(str(text).replace("\r", "\n").split())


def parse_tags(value: str | None) -> list[str]:
    if not value:
        return []
    text = value.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(x) for x in parsed if str(x).strip()]
    except Exception:
        pass
    return [x.strip() for x in text.replace("，", ",").split(",") if x.strip()]


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
        fields = [
            "video_id",
            "channel",
            "title",
            "view_count",
            "published_at",
            "fetched_at",
            "thumbnail_url",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_thumbnail_overrides(path: Path) -> dict[str, str]:
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["video_id", "thumbnail_text", "note"])
            writer.writeheader()
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
