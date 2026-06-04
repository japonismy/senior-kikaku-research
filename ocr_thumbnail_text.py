# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
VAULT_ROOT = Path(r"c:\Data\ObsidianVault")
TOOLS_DIR = VAULT_ROOT / "04_Tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from core.config import Config  # noqa: E402


ASSET_DIR = HERE / "thumbnail_assets"
WORK_DIR = HERE / "ocr_work"
OVERRIDE_PATH = HERE / "thumbnail_text_overrides.csv"
REPORT_PATH = HERE / "reports" / "thumbnail_ocr_batch.csv"

PROMPT = """You are reading Japanese text embedded in a YouTube thumbnail.
Return only JSON.

Schema:
{
  "thumbnail_text": "all visible Japanese thumbnail text, preserving the main phrases",
  "confidence": 0.0,
  "notes": "short note if unreadable"
}

Rules:
- Do not include the YouTube video title unless it is visibly written on the thumbnail.
- If no readable text exists, use an empty string.
- Keep only text visible in the image.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--min-views", type=int, default=0)
    ap.add_argument("--engine", choices=["tesseract", "gemini"], default="tesseract")
    args = ap.parse_args()

    WORK_DIR.mkdir(exist_ok=True)
    client = model_id = None
    if args.engine == "gemini":
        client, model_id = load_client()
    overrides = read_overrides()
    videos = load_videos()
    targets = [
        v for v in videos
        if not overrides.get(v["video_id"])
        and not (v.get("thumbnail_text") or "").strip()
        and int(v.get("view_count") or 0) >= args.min_views
        and (ASSET_DIR / f"{v['video_id']}.jpg").exists()
    ]

    rows = []
    processed = ok = ng = 0
    for video in targets[: args.limit]:
        video_id = video["video_id"]
        image_path = ASSET_DIR / f"{video_id}.jpg"
        try:
            data = ocr_image(client, model_id, image_path) if args.engine == "gemini" else ocr_image_tesseract(image_path)
            text = clean_text(data.get("thumbnail_text", ""))
            if text:
                overrides[video_id] = {"thumbnail_text": text, "note": "gemini_ocr"}
                ok += 1
            else:
                ng += 1
            rows.append(report_row(video, text, data.get("confidence", ""), data.get("notes", ""), ""))
        except Exception as e:
            ng += 1
            rows.append(report_row(video, "", "", "", f"{type(e).__name__}: {str(e)[:120]}"))
        processed += 1
        time.sleep(args.sleep)

    write_overrides(overrides)
    write_report(rows)
    print(json.dumps({"processed": processed, "ok_text": ok, "empty_or_ng": ng, "report": str(REPORT_PATH)}, ensure_ascii=False))
    return 0


def load_client():
    from google import genai
    cfg = Config()
    key = cfg.gemini_api_key
    if not key:
        raise SystemExit("Gemini API key is not configured.")
    return genai.Client(api_key=key), cfg.get_model("text_llm") if hasattr(cfg, "get_model") else "gemini-2.0-flash"


def ocr_image(client, model_id: str, image_path: Path) -> dict:
    from google.genai import types
    image_part = types.Part.from_bytes(data=image_path.read_bytes(), mime_type="image/jpeg")
    resp = client.models.generate_content(
        model=model_id,
        contents=[PROMPT, image_part],
        config=types.GenerateContentConfig(temperature=0.0, response_mime_type="application/json"),
    )
    return parse_json(resp.text or "")


def ocr_image_tesseract(image_path: Path) -> dict:
    pre = WORK_DIR / f"{image_path.stem}_pre.png"
    magick = [
        "magick",
        str(image_path),
        "-resize",
        "2400x1350",
        "-colorspace",
        "Gray",
        "-sharpen",
        "0x1",
        "-contrast-stretch",
        "2%x2%",
        str(pre),
    ]
    subprocess.run(magick, cwd=HERE, check=True, capture_output=True)
    cmd = ["tesseract", str(pre), "stdout", "-l", "jpn+eng", "--psm", "6"]
    proc = subprocess.run(cmd, cwd=HERE, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if proc.returncode:
        raise RuntimeError((proc.stderr or proc.stdout).strip()[:300])
    text = clean_ocr_text(proc.stdout)
    return {"thumbnail_text": text, "confidence": "", "notes": "tesseract_local"}


def parse_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?\s*", "", text.strip())
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start >= 0 and end >= start:
        t = t[start:end + 1]
    return json.loads(t)


def load_videos() -> list[dict]:
    text = (HERE / "data" / "videos.js").read_text(encoding="utf-8")
    return json.loads(text.removeprefix("window.VIDEO_DATA = ").strip().rstrip(";"))


def read_overrides() -> dict[str, dict[str, str]]:
    if not OVERRIDE_PATH.exists():
        return {}
    out = {}
    with OVERRIDE_PATH.open("r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            vid = clean_text(row.get("video_id", ""))
            text = clean_text(row.get("thumbnail_text", ""))
            if vid and text:
                out[vid] = {"thumbnail_text": text, "note": row.get("note", "")}
    return out


def write_overrides(rows: dict[str, dict[str, str]]) -> None:
    with OVERRIDE_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["video_id", "thumbnail_text", "note"])
        writer.writeheader()
        for vid in sorted(rows):
            writer.writerow({"video_id": vid, **rows[vid]})


def write_report(rows: list[dict]) -> None:
    fields = ["video_id", "channel", "title", "view_count", "thumbnail_text", "confidence", "notes", "error"]
    with REPORT_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def report_row(video: dict, text: str, confidence: object, notes: str, error: str) -> dict:
    return {
        "video_id": video.get("video_id", ""),
        "channel": video.get("channel", ""),
        "title": video.get("title", ""),
        "view_count": video.get("view_count", 0),
        "thumbnail_text": text,
        "confidence": confidence,
        "notes": notes,
        "error": error,
    }


def clean_text(value: str) -> str:
    return " ".join(str(value or "").replace("\r", "\n").split())


def clean_ocr_text(value: str) -> str:
    lines = []
    for line in str(value or "").replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", "", line)
        line = line.strip(" _-—|[]{}()（）.,，。:：;；")
        if len(line) >= 2:
            lines.append(line)
    return " ".join(lines)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    raise SystemExit(main())
