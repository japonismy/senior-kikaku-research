import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from google.cloud import bigquery


PROJECT_ID = "rugged-destiny-408613"
DATASET = "senior_reading_all"
BASE_DIR = Path(__file__).resolve().parent
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
OUT_DIR = BASE_DIR / f"copy_fallback_search_{STAMP}"

TARGET_CHANNEL_NAMES = [
    "\u4eba\u751f\u306e\u6e29\u3082\u308a",
    "\u4eba\u751f\u306f\u4e03\u8272",
    "\u4eba\u751f\u306f\u5b9d\u7269",
    "\u4eba\u751f\u306e\u7e01\u5074",
    "\u4eba\u751f\u306e\u30d2\u30ab\u30ea",
    "\u4eba\u751f\u306e\u7cf8",
]

MAX_RESULTS = int(os.environ.get("SEARCH_RESULTS_PER_TITLE", "8"))
MIN_SCORE = float(os.environ.get("MIN_SCORE", "0.70"))
SLEEP_SEC = float(os.environ.get("SLEEP_SEC", "0.2"))


def norm_title(value: str) -> str:
    value = value or ""
    value = value.replace("...", "").replace("\u2026", "")
    value = re.sub(r"[\u3010\u3011\[\]\uff08\uff09()\u300c\u300d\u300e\u300f]", "", value)
    value = re.sub(r"[\s\u3000]+", "", value)
    value = re.sub(r"[\u301c\uff5e~!！?？,，.。:：;；\-_ー\u2192\u2190\u30fb/／|｜]", "", value)
    return value.lower().strip()


def score_title(source: str, candidate: str) -> tuple[str, float]:
    src = norm_title(source)
    hit = norm_title(candidate)
    if not src or not hit:
        return "empty", 0.0
    if src == hit:
        return "exact_normalized", 1.0
    if src in hit or hit in src:
        return "title_contains", min(len(src), len(hit)) / max(len(src), len(hit))
    return "fuzzy_title", SequenceMatcher(None, src, hit).ratio()


def find_ytdlp() -> str:
    exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if exe:
        return exe
    fallback = Path(r"C:\Users\admin\anaconda3\Scripts\yt-dlp.exe")
    if fallback.exists():
        return str(fallback)
    raise SystemExit("yt-dlp was not found")


def fetch_targets(client: bigquery.Client) -> list[dict]:
    query = f"""
    SELECT
      c.channel_name,
      v.channel_id,
      v.video_id,
      v.title,
      v.published_at,
      v.duration_sec,
      CAST(v.view_count AS STRING) AS view_count,
      COALESCE(a.gcs_uri, '') AS thumbnail_gcs_uri,
      LENGTH(COALESCE(t.transcript_text, '')) AS transcript_chars
    FROM `{PROJECT_ID}.{DATASET}.analysis_competitor_db__videos` v
    JOIN `{PROJECT_ID}.{DATASET}.analysis_competitor_db__channels` c
      ON c.channel_id = v.channel_id
    LEFT JOIN `{PROJECT_ID}.{DATASET}.thumbnail_assets` a
      ON a.video_id = v.video_id
      AND COALESCE(a.error, '') = ''
    LEFT JOIN `{PROJECT_ID}.{DATASET}.analysis_competitor_db__transcripts` t
      ON t.video_id = v.video_id
    WHERE c.channel_name IN UNNEST(@channel_names)
      AND COALESCE(a.gcs_uri, '') = ''
    ORDER BY c.channel_name, SAFE_CAST(v.published_at AS TIMESTAMP) DESC, v.video_id
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("channel_names", "STRING", TARGET_CHANNEL_NAMES)
        ]
    )
    return [dict(row) for row in client.query(query, job_config=job_config).result()]


def run_search(ytdlp: str, title: str) -> tuple[list[dict], str]:
    cmd = [
        ytdlp,
        "--flat-playlist",
        "--dump-single-json",
        "--no-warnings",
        f"ytsearch{MAX_RESULTS}:{title}",
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=75,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return [], proc.stderr.strip()[:1000]
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return [], f"json_decode_error: {exc}"
    return payload.get("entries") or [], ""


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        if not rows:
            f.write("")
            return
        fields = list(rows[0].keys())
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    client = bigquery.Client(project=PROJECT_ID)
    ytdlp = find_ytdlp()
    targets = fetch_targets(client)

    target_rows = []
    candidates = []
    logs = []
    for idx, row in enumerate(targets, 1):
        source_url = f"https://www.youtube.com/watch?v={row['video_id']}"
        target_rows.append(
            {
                "source_index": idx,
                "source_channel": row["channel_name"],
                "source_channel_id": row["channel_id"],
                "source_video_id": row["video_id"],
                "source_url": source_url,
                "source_title": row["title"],
                "published_at": row.get("published_at") or "",
                "duration_sec": row.get("duration_sec") or "",
                "view_count": row.get("view_count") or "",
                "transcript_chars": row.get("transcript_chars") or 0,
            }
        )
        entries, err = run_search(ytdlp, row["title"])
        logs.append(
            {
                "source_index": idx,
                "source_video_id": row["video_id"],
                "source_title": row["title"],
                "result_count": len(entries),
                "error": err,
            }
        )
        for rank, entry in enumerate(entries, 1):
            hit_id = entry.get("id") or ""
            hit_title = entry.get("title") or ""
            channel_id = entry.get("channel_id") or entry.get("uploader_id") or ""
            channel = entry.get("channel") or entry.get("uploader") or ""
            if not hit_id or hit_id == row["video_id"]:
                continue
            match_type, score = score_title(row["title"], hit_title)
            if score < MIN_SCORE:
                continue
            candidates.append(
                {
                    "source_index": idx,
                    "source_channel": row["channel_name"],
                    "source_video_id": row["video_id"],
                    "source_url": source_url,
                    "source_title": row["title"],
                    "source_transcript_chars": row.get("transcript_chars") or 0,
                    "rank": rank,
                    "candidate_video_id": hit_id,
                    "candidate_url": f"https://www.youtube.com/watch?v={hit_id}",
                    "candidate_title": hit_title,
                    "candidate_channel": channel,
                    "candidate_channel_id": channel_id,
                    "candidate_channel_url": f"https://www.youtube.com/channel/{channel_id}" if channel_id else "",
                    "match_type": match_type,
                    "score": round(score, 4),
                    "notes": "new_youtube_search_title_match",
                }
            )
        time.sleep(SLEEP_SEC)

    summary_map = defaultdict(lambda: {"matched_rows": 0, "exact_rows": 0, "source_videos": set()})
    channel_names = {}
    channel_urls = {}
    for item in candidates:
        key = item["candidate_channel_id"] or item["candidate_channel"]
        channel_names[key] = item["candidate_channel"]
        channel_urls[key] = item["candidate_channel_url"]
        summary_map[key]["matched_rows"] += 1
        summary_map[key]["source_videos"].add(item["source_video_id"])
        if item["match_type"] == "exact_normalized":
            summary_map[key]["exact_rows"] += 1

    summary = []
    for key, value in summary_map.items():
        summary.append(
            {
                "candidate_channel": channel_names.get(key, ""),
                "candidate_channel_id": key,
                "candidate_channel_url": channel_urls.get(key, ""),
                "matched_source_videos": len(value["source_videos"]),
                "matched_rows": value["matched_rows"],
                "exact_rows": value["exact_rows"],
            }
        )
    summary.sort(key=lambda r: (r["exact_rows"], r["matched_source_videos"], r["matched_rows"]), reverse=True)

    target_csv = OUT_DIR / "copy_fallback_source_targets.csv"
    candidates_csv = OUT_DIR / "copy_fallback_candidates.csv"
    summary_csv = OUT_DIR / "copy_fallback_candidate_channels.csv"
    log_csv = OUT_DIR / "copy_fallback_search_log.csv"
    write_csv(target_csv, target_rows)
    write_csv(candidates_csv, candidates)
    write_csv(summary_csv, summary)
    write_csv(log_csv, logs)

    report = {
        "searched_source_videos": len(targets),
        "candidate_rows": len(candidates),
        "candidate_channels": len(summary),
        "min_score": MIN_SCORE,
        "max_results_per_title": MAX_RESULTS,
        "target_csv": str(target_csv),
        "candidates_csv": str(candidates_csv),
        "summary_csv": str(summary_csv),
        "log_csv": str(log_csv),
    }
    (OUT_DIR / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
