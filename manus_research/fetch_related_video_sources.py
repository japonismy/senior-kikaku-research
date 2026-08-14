# -*- coding: utf-8 -*-
"""Fetch per-target RELATED_VIDEO source edges from YouTube Analytics."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import bigquery


PROJECT_ID = "rugged-destiny-408613"
DATASET = "senior_reading_all"
VAULT = Path(r"E:\Data\ObsidianVault")
OAUTH_TOOL = VAULT / "04_Tools" / "youtube_studio_oauth"
CALIBRATION_FILE = (
    VAULT / "02_Channels" / "シニア朗読" / "analysis" / "20260813_一次調査"
    / "manus_calibration_v1" / "calibration_25_blind.jsonl"
)
DEFAULT_OUTPUT = (
    VAULT / "02_Channels" / "シニア朗読" / "analysis" / "20260813_一次調査"
    / "manus_calibration_v1" / "related_video_20260814"
)


def load_inputs() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in CALIBRATION_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def report_rows(analytics, channel_id: str, video_id: str, start: str, end: str, max_results: int) -> list[list[Any]]:
    response = analytics.reports().query(
        ids=f"channel=={channel_id}",
        startDate=start,
        endDate=end,
        metrics="views,estimatedMinutesWatched",
        dimensions="insightTrafficSourceDetail",
        filters=f"video=={video_id};insightTrafficSourceType==RELATED_VIDEO",
        sort="-views",
        maxResults=max_results,
    ).execute()
    return response.get("rows", [])


def report_total(analytics, channel_id: str, video_id: str, start: str, end: str) -> tuple[int, float]:
    response = analytics.reports().query(
        ids=f"channel=={channel_id}",
        startDate=start,
        endDate=end,
        metrics="views,estimatedMinutesWatched",
        dimensions="insightTrafficSourceType",
        filters=f"video=={video_id}",
        sort="-views",
    ).execute()
    for source_type, views, minutes in response.get("rows", []):
        if source_type == "RELATED_VIDEO":
            return int(views), float(minutes)
    return 0, 0.0


def fetch_metadata(youtube, video_ids: list[str]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for index in range(0, len(video_ids), 50):
        batch = video_ids[index:index + 50]
        response = youtube.videos().list(
            part="snippet,status,contentDetails",
            id=",".join(batch),
            maxResults=len(batch),
        ).execute()
        for item in response.get("items", []):
            snippet = item.get("snippet") or {}
            status = item.get("status") or {}
            details = item.get("contentDetails") or {}
            output[item["id"]] = {
                "source_title": snippet.get("title") or "",
                "source_channel_id": snippet.get("channelId") or "",
                "source_channel_title": snippet.get("channelTitle") or "",
                "source_published_at": snippet.get("publishedAt") or "",
                "source_privacy_status": status.get("privacyStatus") or "",
                "source_duration": details.get("duration") or "",
            }
    return output


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def save_bigquery(rows: list[dict[str, Any]]) -> dict[str, str]:
    client = bigquery.Client(project=PROJECT_ID)
    current_id = f"{PROJECT_ID}.{DATASET}.youtube_related_video_edges_current_v1"
    history_id = f"{PROJECT_ID}.{DATASET}.youtube_related_video_edges_history"
    client.load_table_from_json(
        rows,
        current_id,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE", autodetect=True),
    ).result()
    client.load_table_from_json(
        rows,
        history_id,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_APPEND", autodetect=True),
    ).result()
    return {"current": current_id, "history": history_id}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-04-01")
    parser.add_argument("--end", default="2026-08-14")
    parser.add_argument("--max-results", type=int, default=25)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()
    if not 1 <= args.max_results <= 25:
        raise ValueError("--max-results must be between 1 and 25 for insightTrafficSourceDetail")

    sys.path.insert(0, str(OAUTH_TOOL))
    from oauth_common import (  # noqa: PLC0415
        build_services,
        load_credentials,
        refresh_credentials_if_needed,
        resolve_token_ref,
    )

    ref = resolve_token_ref("jinsei_recipe", "studio_readonly_monetary")
    credentials = load_credentials(ref.token_file)
    credentials, refresh_status = refresh_credentials_if_needed(credentials, ref.token_file)
    if not credentials or not getattr(credentials, "valid", False):
        raise RuntimeError("YouTube Studio credentials are invalid")
    youtube, analytics = build_services(credentials)
    channel_id = ref.channel["channel_id"]

    inputs = load_inputs()
    if len(inputs) != 25 or len({row["video_id"] for row in inputs}) != 25:
        raise ValueError("expected 25 unique calibration videos")

    fetched_at = datetime.now(timezone.utc).isoformat()
    snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_edges: list[dict[str, Any]] = []
    target_totals: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    for item in inputs:
        video_id = item["video_id"]
        try:
            total_views, total_minutes = report_total(analytics, channel_id, video_id, args.start, args.end)
            details = report_rows(analytics, channel_id, video_id, args.start, args.end, args.max_results)
            target_totals[video_id] = {
                "target_related_views": total_views,
                "target_related_minutes": round(total_minutes, 4),
                "detail_views": sum(int(row[1]) for row in details),
                "detail_source_count": len(details),
            }
            for source_video_id, views, minutes in details:
                raw_edges.append({
                    "snapshot_id": snapshot_id,
                    "fetched_at": fetched_at,
                    "period_start": args.start,
                    "period_end": args.end,
                    "calibration_id": item["calibration_id"],
                    "target_video_id": video_id,
                    "target_title": item["title"],
                    "source_video_id": str(source_video_id),
                    "related_views": int(views),
                    "related_minutes": round(float(minutes), 4),
                })
        except Exception as exc:  # continue other targets and preserve the error manifest
            errors.append({"target_video_id": video_id, "error": f"{type(exc).__name__}: {str(exc)[:1000]}"})

    source_ids = sorted({row["source_video_id"] for row in raw_edges if row["source_video_id"]})
    metadata = fetch_metadata(youtube, source_ids)
    input_ids = {row["video_id"] for row in inputs}
    for edge in raw_edges:
        meta = metadata.get(edge["source_video_id"], {})
        totals = target_totals[edge["target_video_id"]]
        edge.update(meta)
        edge["source_metadata_available"] = bool(meta)
        edge["source_availability"] = "available" if meta else "unavailable_or_hidden"
        edge["source_is_self"] = bool(
            meta.get("source_channel_id") == channel_id or edge["source_video_id"] in input_ids
        )
        edge["target_related_views"] = totals["target_related_views"]
        edge["detail_views"] = totals["detail_views"]
        edge["detail_coverage"] = round(totals["detail_views"] / totals["target_related_views"], 6) if totals["target_related_views"] else None
        edge["source_share_of_target_related"] = round(edge["related_views"] / totals["target_related_views"], 6) if totals["target_related_views"] else None

    if not raw_edges:
        raise RuntimeError(f"no related-video edges returned; errors={errors}")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "calibration_25_related_video_edges_20260814.csv"
    write_csv(csv_path, raw_edges)
    table_ids = save_bigquery(raw_edges)

    summary = {
        "snapshot_id": snapshot_id,
        "fetched_at": fetched_at,
        "period_start": args.start,
        "period_end": args.end,
        "credential_refresh_status": refresh_status,
        "target_count": len(inputs),
        "targets_with_edges": len(target_totals),
        "edge_count": len(raw_edges),
        "unique_source_count": len(source_ids),
        "available_source_metadata": sum(1 for source_id in source_ids if source_id in metadata),
        "unavailable_source_metadata": sum(1 for source_id in source_ids if source_id not in metadata),
        "self_edge_count": sum(bool(row["source_is_self"]) for row in raw_edges),
        "external_edge_count": sum(not bool(row["source_is_self"]) for row in raw_edges),
        "total_related_views": sum(item["target_related_views"] for item in target_totals.values()),
        "total_detail_views": sum(item["detail_views"] for item in target_totals.values()),
        "weighted_detail_coverage": round(
            sum(item["detail_views"] for item in target_totals.values())
            / sum(item["target_related_views"] for item in target_totals.values()), 6
        ) if sum(item["target_related_views"] for item in target_totals.values()) else None,
        "target_totals": target_totals,
        "errors": errors,
        "csv": str(csv_path),
        "bigquery": table_ids,
    }
    summary_path = output_dir / "calibration_25_related_video_fetch_summary_20260814.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
