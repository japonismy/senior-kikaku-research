# -*- coding: utf-8 -*-
"""Join calibrated story structures to YouTube Analytics and reach metrics."""
from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import bigquery


PROJECT_ID = "rugged-destiny-408613"
DATASET = "senior_reading_all"
VAULT = Path(r"E:\Data\ObsidianVault")
ANALYTICS_DIR = VAULT / "02_Channels" / "シニア朗読" / "analysis" / "studio_analytics" / "20260814"
REACH_DIR = (
    VAULT / "02_Channels" / "シニア朗読" / "analysis" / "studio_analytics"
    / "20260812_refresh" / "20260812_073051" / "reach_reports"
)
OUTPUT_DIR = (
    VAULT / "02_Channels" / "シニア朗読" / "analysis" / "20260813_一次調査"
    / "manus_calibration_v1" / "performance_20260814"
)


def number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def median(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [number(row.get(key)) for row in rows if row.get(key) not in (None, "")]
    return round(statistics.median(values), 4) if values else None


def rank(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(indexed):
        end = index + 1
        while end < len(indexed) and indexed[end][1] == indexed[index][1]:
            end += 1
        average_rank = (index + 1 + end) / 2
        for position in range(index, end):
            ranks[indexed[position][0]] = average_rank
        index = end
    return ranks


def spearman(rows: list[dict[str, Any]], x_key: str, y_key: str, log_y: bool = False) -> float | None:
    pairs = []
    for row in rows:
        if row.get(x_key) in (None, "") or row.get(y_key) in (None, ""):
            continue
        x_value = number(row[x_key])
        y_value = number(row[y_key])
        if log_y:
            y_value = math.log1p(max(y_value, 0))
        pairs.append((x_value, y_value))
    if len(pairs) < 4:
        return None
    x_ranks = rank([pair[0] for pair in pairs])
    y_ranks = rank([pair[1] for pair in pairs])
    x_mean = statistics.mean(x_ranks)
    y_mean = statistics.mean(y_ranks)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_ranks, y_ranks))
    denominator = math.sqrt(
        sum((x - x_mean) ** 2 for x in x_ranks) * sum((y - y_mean) ** 2 for y in y_ranks)
    )
    return round(numerator / denominator, 4) if denominator else None


def read_analytics() -> dict[str, dict[str, Any]]:
    path = ANALYTICS_DIR / "video_breakdown.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return {row["video"]: row for row in csv.DictReader(handle)}


def read_reach() -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    totals: dict[str, dict[str, float]] = defaultdict(lambda: {"impressions": 0.0, "clicks": 0.0})
    dates: set[str] = set()
    files = sorted(REACH_DIR.glob("channel_reach_basic_a1__*.csv"))
    for path in files:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                video_id = row.get("video_id") or ""
                impressions = number(row.get("video_thumbnail_impressions"))
                ctr = number(row.get("video_thumbnail_impressions_ctr"))
                totals[video_id]["impressions"] += impressions
                totals[video_id]["clicks"] += impressions * ctr
                if row.get("date"):
                    dates.add(row["date"])
    normalized = {}
    for video_id, values in totals.items():
        impressions = values["impressions"]
        normalized[video_id] = {
            "reach_impressions": round(impressions),
            "reach_ctr": round(values["clicks"] / impressions, 6) if impressions else None,
        }
    coverage = {
        "file_count": len(files),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
        "video_count": len(normalized),
    }
    return normalized, coverage


def load_calibration(client: bigquery.Client) -> list[dict[str, Any]]:
    query = f"""
    SELECT calibration_id, video_id, title,
      prior_primary_structure primary_structure,
      prior_director_card director_card,
      prior_food_role food_role,
      result_json
    FROM `{PROJECT_ID}.{DATASET}.manus_calibration_review_v1`
    WHERE manus_primary_structure IS NOT NULL
    ORDER BY calibration_id
    """
    return [dict(row) for row in client.query(query).result()]


def group_summary(rows: list[dict[str, Any]], dimension: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(dimension) or "unknown")].append(row)
    output = []
    for value, members in sorted(groups.items()):
        ctr_rows = [row for row in members if row.get("reach_ctr") not in (None, "")]
        output.append({
            "value": value,
            "n": len(members),
            "median_views": median(members, "views"),
            "median_views_per_day": median(members, "views_per_day"),
            "median_average_view_percentage": median(members, "averageViewPercentage"),
            "median_reach_ctr": median(ctr_rows, "reach_ctr"),
            "median_subscribers_per_1000_views": median(members, "subscribers_per_1000_views"),
        })
    return output


def main() -> int:
    client = bigquery.Client(project=PROJECT_ID)
    analytics = read_analytics()
    reach, reach_coverage = read_reach()
    rows = load_calibration(client)
    joined: list[dict[str, Any]] = []
    as_of = datetime(2026, 8, 14, tzinfo=timezone.utc)
    for source in rows:
        result = json.loads(source.pop("result_json") or "{}")
        video_id = source["video_id"]
        performance = analytics.get(video_id, {})
        published_at = performance.get("publishedAt") or ""
        age_days = None
        if published_at:
            published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            age_days = max((as_of - published).total_seconds() / 86400, 1)
        views = number(performance.get("views")) if performance else None
        joined_row = {
            **source,
            "emotional_contract": result.get("emotional_contract"),
            "rescue_direction": result.get("rescue_direction"),
            "midpoint_mechanism": result.get("midpoint_mechanism"),
            "climax_resolution": result.get("climax_resolution"),
            "ending_reward": result.get("ending_reward"),
            "hook_strength": result.get("hook_strength"),
            "midpoint_strength": result.get("midpoint_strength"),
            "ending_satisfaction": result.get("ending_satisfaction"),
            "exchangeability_risk": result.get("exchangeability_risk"),
            "published_at": published_at or None,
            "age_days": round(age_days, 2) if age_days else None,
            "views": round(views) if views is not None else None,
            "views_per_day": round(views / age_days, 2) if views is not None and age_days else None,
            "averageViewDuration": round(number(performance.get("averageViewDuration")), 2) if performance else None,
            "averageViewPercentage": round(number(performance.get("averageViewPercentage")), 4) if performance else None,
            "subscribersGained": round(number(performance.get("subscribersGained"))) if performance else None,
            "likes": round(number(performance.get("likes"))) if performance else None,
            "comments": round(number(performance.get("comments"))) if performance else None,
            "shares": round(number(performance.get("shares"))) if performance else None,
            "subscribers_per_1000_views": round(number(performance.get("subscribersGained")) * 1000 / views, 4) if views else None,
            "likes_per_1000_views": round(number(performance.get("likes")) * 1000 / views, 4) if views else None,
            **reach.get(video_id, {"reach_impressions": None, "reach_ctr": None}),
        }
        joined.append(joined_row)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_DIR / "calibration_25_performance_join_20260814.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(joined[0].keys()))
        writer.writeheader()
        writer.writerows(joined)

    focus = {structure: [row for row in joined if row["primary_structure"] == structure] for structure in ("P03B", "P06B")}
    summary: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "analytics_period": {"start": "2026-04-01", "end": "2026-08-14"},
        "reach_coverage": reach_coverage,
        "calibration_count": len(joined),
        "analytics_matched": sum(row["views"] is not None for row in joined),
        "reach_matched": sum(row["reach_ctr"] is not None for row in joined),
        "structures": {},
    }
    for structure, members in focus.items():
        summary["structures"][structure] = {
            "n": len(members),
            "median_views": median(members, "views"),
            "median_views_per_day": median(members, "views_per_day"),
            "median_average_view_percentage": median(members, "averageViewPercentage"),
            "median_reach_ctr": median([row for row in members if row.get("reach_ctr") is not None], "reach_ctr"),
            "median_subscribers_per_1000_views": median(members, "subscribers_per_1000_views"),
            "top_views": [
                {"video_id": row["video_id"], "title": row["title"], "views": row["views"],
                 "avp": row["averageViewPercentage"], "ctr": row["reach_ctr"],
                 "midpoint": row["midpoint_mechanism"], "climax": row["climax_resolution"],
                 "ending": row["ending_reward"], "exchangeability_risk": row["exchangeability_risk"]}
                for row in sorted(members, key=lambda item: number(item.get("views")), reverse=True)[:5]
            ],
            "dimensions": {
                dimension: group_summary(members, dimension)
                for dimension in ("midpoint_mechanism", "climax_resolution", "ending_reward", "food_role")
            },
            "correlations": {
                "exchangeability_vs_log_views": spearman(members, "exchangeability_risk", "views", log_y=True),
                "exchangeability_vs_avp": spearman(members, "exchangeability_risk", "averageViewPercentage"),
                "midpoint_strength_vs_log_views": spearman(members, "midpoint_strength", "views", log_y=True),
                "ending_satisfaction_vs_avp": spearman(members, "ending_satisfaction", "averageViewPercentage"),
                "reach_ctr_vs_log_views": spearman(
                    [row for row in members if row.get("reach_ctr") is not None], "reach_ctr", "views", log_y=True
                ),
            },
        }
    summary_path = OUTPUT_DIR / "calibration_25_performance_summary_20260814.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    bq_rows = []
    for row in joined:
        bq_rows.append({key: value for key, value in row.items()})
    table_id = f"{PROJECT_ID}.{DATASET}.manus_calibration_performance_v1"
    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE", autodetect=True)
    client.load_table_from_json(bq_rows, table_id, job_config=job_config).result()
    print(json.dumps({
        "rows": len(joined), "analytics_matched": summary["analytics_matched"],
        "reach_matched": summary["reach_matched"], "csv": str(csv_path),
        "summary": str(summary_path), "bigquery_table": table_id,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
