# -*- coding: utf-8 -*-
"""Recover failed high-value thumbnail OCR from historical Vault images."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from google.cloud import bigquery, storage

from manus_pipeline import DATASET, PROJECT_ID


VAULT = Path(r"E:\Data\ObsidianVault")
OCR_MODULE_PATH = VAULT / "04_Tools" / "channels" / "senior_reading" / "cloud_batch" / "daily_senior_thumbnail_ocr.py"


def load_ocr_module():
    spec = importlib.util.spec_from_file_location("daily_senior_thumbnail_ocr", OCR_MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"OCR moduleを読み込めません: {OCR_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch_targets(client: bigquery.Client, min_views: int, limit: int) -> list[dict[str, Any]]:
    sql = f"""
    SELECT v.video_id, v.channel_id, v.title, SAFE_CAST(v.view_count AS INT64) AS view_count,
           o.error
    FROM `{PROJECT_ID}.{DATASET}.analysis_competitor_db__videos` v
    JOIN `{PROJECT_ID}.{DATASET}.analysis_competitor_db__channels` c USING(channel_id)
    JOIN `{PROJECT_ID}.{DATASET}.thumbnail_ocr_gemini` o USING(video_id)
    WHERE COALESCE(c.include, 0)=1
      AND COALESCE(c.country, '')!='KR'
      AND COALESCE(c.source_type, '')!='original_kr'
      AND COALESCE(c.sync_target, '') IN ('senior_reading', 'roudoku_longform_jp')
      AND SAFE_CAST(v.view_count AS INT64)>=@min_views
      AND COALESCE(o.combined_text, '')=''
      AND COALESCE(o.error, '')!=''
    ORDER BY SAFE_CAST(v.view_count AS INT64) DESC
    LIMIT {int(limit)}
    """
    config = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("min_views", "INT64", min_views)]
    )
    return [dict(row) for row in client.query(sql, job_config=config).result()]


def discover_images(root: Path, video_ids: set[str]) -> dict[str, Path]:
    result = subprocess.run(
        ["rg", "--files", str(root)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )
    candidates: dict[str, list[Path]] = {video_id: [] for video_id in video_ids}
    image_suffixes = {".jpg", ".jpeg", ".png", ".webp"}
    for raw in result.stdout.splitlines():
        path = Path(raw)
        if path.suffix.lower() not in image_suffixes:
            continue
        for video_id in video_ids:
            if video_id in path.stem:
                candidates[video_id].append(path)
    selected = {}
    for video_id, paths in candidates.items():
        valid = [path for path in paths if path.exists() and path.stat().st_size > 1024]
        if valid:
            selected[video_id] = max(valid, key=lambda path: path.stat().st_size)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-views", type=int, default=100_000)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--search-root", default=str(VAULT / "02_Channels"))
    args = parser.parse_args()

    ocr = load_ocr_module()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEYが設定されていません")
    from google import genai
    from google.genai import types

    bq = bigquery.Client(project=PROJECT_ID)
    targets = fetch_targets(bq, args.min_views, args.limit)
    by_id = {target["video_id"]: target for target in targets}
    found = discover_images(Path(args.search_root), set(by_id))
    genai_client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=ocr.THUMBNAIL_OCR_TIMEOUT_MS),
    )
    storage_client = storage.Client(project=PROJECT_ID)
    ocr_rows = []
    asset_rows = []
    failures = []
    empty = 0
    completed = 0
    for video_id, target in by_id.items():
        path = found.get(video_id)
        if path is None:
            failures.append({"video_id": video_id, "error": "vault_image_not_found"})
            continue
        try:
            data = path.read_bytes()
            image_mime = ocr.mime_type(str(path), "")
            value, _ = ocr.analyze_thumbnail(genai_client, data, image_mime)
            row = ocr.ocr_bq_row(video_id, value)
            provenance = f"vault_recovery:{path}"
            row["notes"] = f"{row.get('notes') or ''} / {provenance}".strip(" / ")
            empty += int(not row["combined_text"])
            completed += 1
            ocr_rows.append(row)
            asset_rows.append(ocr.upload_asset(storage_client, target, data, image_mime, str(path)))
            if len(ocr_rows) >= 10:
                ocr.flush_rows(bq, asset_rows, ocr_rows)
                asset_rows, ocr_rows = [], []
        except Exception as exc:
            failures.append({"video_id": video_id, "error": f"{type(exc).__name__}: {exc}"[:500]})
    ocr.flush_rows(bq, asset_rows, ocr_rows)
    print(
        json.dumps(
            {
                "targets": len(targets),
                "vault_images_found": len(found),
                "completed": completed,
                "empty": empty,
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
