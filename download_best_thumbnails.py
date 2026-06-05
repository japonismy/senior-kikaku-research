# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent
ASSET_DIR = HERE / "thumbnail_assets"
REPORT_PATH = HERE / "reports" / "thumbnail_best_assets.csv"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50, help="Number of new/missing assets to download. 0 means all.")
    ap.add_argument("--sleep", type=float, default=0.2)
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    ASSET_DIR.mkdir(exist_ok=True)
    videos = load_videos()
    rows = []
    seen_rows = read_existing_report()
    done = ok = ng = skipped_existing = 0

    for video in videos:
        if args.limit and done >= args.limit:
            break
        video_id = video["video_id"]
        out = ASSET_DIR / f"{video_id}.jpg"
        if out.exists() and not args.overwrite:
            rows.append(row(video, "existing", str(out), out.stat().st_size, ""))
            skipped_existing += 1
            continue

        result = download_best(video, out)
        rows.append(row(video, result["quality"], str(out) if out.exists() else "", result["bytes"], result["error"]))
        done += 1
        ok += 1 if out.exists() else 0
        ng += 0 if out.exists() else 1
        time.sleep(args.sleep)

    write_report(merge_report_rows(seen_rows, rows))
    print(json.dumps({"processed": done, "ok": ok, "ng": ng, "skipped_existing": skipped_existing, "report": str(REPORT_PATH)}, ensure_ascii=False))
    return 0


def load_videos() -> list[dict]:
    text = (HERE / "data" / "videos.js").read_text(encoding="utf-8")
    prefix = "window.VIDEO_DATA = "
    if not text.startswith(prefix):
        raise SystemExit("Unexpected data/videos.js format")
    return json.loads(text[len(prefix):].strip().rstrip(";"))


def download_best(video: dict, out: Path) -> dict:
    video_id = video["video_id"]
    candidates = [
        ("maxresdefault", f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"),
        ("sddefault", f"https://i.ytimg.com/vi/{video_id}/sddefault.jpg"),
        ("hqdefault", f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"),
    ]
    for url in video.get("thumbnail_fallback_urls", []) or []:
        if url and ("fallback", url) not in candidates:
            candidates.append(("fallback", url))
    if video.get("thumbnail_url"):
        candidates.append(("db_thumbnail_url", video["thumbnail_url"]))
    last_error = ""
    for quality, url in candidates:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
            if len(data) < 2048:
                last_error = f"{quality}: too small"
                continue
            out.write_bytes(data)
            return {"quality": quality, "bytes": len(data), "error": ""}
        except (urllib.error.URLError, TimeoutError) as e:
            last_error = f"{quality}: {type(e).__name__}"
    return {"quality": "", "bytes": 0, "error": last_error}


def row(video: dict, quality: str, path: str, size: int, error: str) -> dict:
    return {
        "video_id": video.get("video_id", ""),
        "channel": video.get("channel", ""),
        "title": video.get("title", ""),
        "view_count": video.get("view_count", 0),
        "quality": quality,
        "local_path": path,
        "bytes": size,
        "error": error,
    }


def write_report(rows: list[dict]) -> None:
    REPORT_PATH.parent.mkdir(exist_ok=True)
    fields = ["video_id", "channel", "title", "view_count", "quality", "local_path", "bytes", "error"]
    with REPORT_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def read_existing_report() -> dict[str, dict]:
    if not REPORT_PATH.exists():
        return {}
    with REPORT_PATH.open("r", newline="", encoding="utf-8-sig") as f:
        return {row["video_id"]: row for row in csv.DictReader(f) if row.get("video_id")}


def merge_report_rows(existing: dict[str, dict], rows: list[dict]) -> list[dict]:
    merged = dict(existing)
    for row_data in rows:
        if row_data.get("video_id"):
            merged[row_data["video_id"]] = row_data
    return list(merged.values())


if __name__ == "__main__":
    raise SystemExit(main())
