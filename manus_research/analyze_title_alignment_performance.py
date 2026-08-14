# -*- coding: utf-8 -*-
"""Join Manus title and title/thumbnail alignment labels to performance."""
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
OUTPUT_DIR = (
    VAULT / "02_Channels" / "シニア朗読" / "analysis" / "20260813_一次調査"
    / "manus_calibration_v1" / "title_alignment_20260814"
)
TITLE_CATEGORIES = (
    "title_protagonist_frame",
    "title_hardship_signal",
    "title_reversal_device",
    "title_promise_type",
    "title_outcome_tease",
    "title_numeric_hook",
    "title_opening_device",
)
TITLE_SCORES = (
    "title_specificity_score",
    "title_stakes_clarity",
    "title_reversal_clarity",
    "title_curiosity_strength",
    "title_emotional_intensity",
    "title_formulaic_risk",
)
ALIGNMENT_CATEGORIES = (
    "alignment_type",
    "alignment_shared_promise",
    "alignment_recommended_direction",
)


def number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def median(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [number(row[key]) for row in rows if row.get(key) not in (None, "")]
    return round(statistics.median(values), 4) if values else None


def rank(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    result = [0.0] * len(values)
    index = 0
    while index < len(indexed):
        end = index + 1
        while end < len(indexed) and indexed[end][1] == indexed[index][1]:
            end += 1
        average = (index + 1 + end) / 2
        for position in range(index, end):
            result[indexed[position][0]] = average
        index = end
    return result


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
    xs = rank([pair[0] for pair in pairs])
    ys = rank([pair[1] for pair in pairs])
    x_mean = statistics.mean(xs)
    y_mean = statistics.mean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    denominator = math.sqrt(sum((x - x_mean) ** 2 for x in xs) * sum((y - y_mean) ** 2 for y in ys))
    return round(numerator / denominator, 4) if denominator else None


def load_rows(client: bigquery.Client) -> list[dict[str, Any]]:
    query = f"""
    SELECT
      p.* EXCEPT(title),
      t.title AS title_text,
      t.protagonist_frame AS title_protagonist_frame,
      t.hardship_signal AS title_hardship_signal,
      t.reversal_device AS title_reversal_device,
      t.promise_type AS title_promise_type,
      t.outcome_tease AS title_outcome_tease,
      t.numeric_hook AS title_numeric_hook,
      t.opening_device AS title_opening_device,
      t.specificity_score AS title_specificity_score,
      t.stakes_clarity AS title_stakes_clarity,
      t.reversal_clarity AS title_reversal_clarity,
      t.curiosity_strength AS title_curiosity_strength,
      t.emotional_intensity AS title_emotional_intensity,
      t.formulaic_risk AS title_formulaic_risk,
      t.confidence AS title_confidence,
      t.needs_review AS title_needs_review,
      a.alignment_type,
      a.shared_promise AS alignment_shared_promise,
      a.title_lead AS alignment_title_lead,
      a.thumbnail_lead AS alignment_thumbnail_lead,
      a.missing_visual_element AS alignment_missing_visual_element,
      a.recommended_direction AS alignment_recommended_direction,
      a.rationale AS alignment_rationale,
      a.confidence AS alignment_confidence,
      a.needs_review AS alignment_needs_review
    FROM `{PROJECT_ID}.{DATASET}.manus_calibration_thumbnail_performance_v1` p
    JOIN `{PROJECT_ID}.{DATASET}.manus_title_calibration_v1` t USING (video_id)
    JOIN `{PROJECT_ID}.{DATASET}.manus_title_thumbnail_alignment_v1` a USING (video_id)
    ORDER BY p.calibration_id
    """
    rows = []
    for source in client.query(query).result():
        row = dict(source)
        for key, value in list(row.items()):
            if isinstance(value, datetime):
                row[key] = value.isoformat()
        rows.append(row)
    return rows


def category_summary(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "unknown")].append(row)
    return sorted([
        {
            "value": value,
            "n": len(members),
            "median_views": median(members, "views"),
            "median_views_per_day": median(members, "views_per_day"),
            "median_avp": median(members, "averageViewPercentage"),
            "median_ctr": median(members, "reach_ctr"),
            "median_subscribers_per_1000_views": median(members, "subscribers_per_1000_views"),
        }
        for value, members in groups.items()
    ], key=lambda item: (-item["n"], item["value"]))


def structure_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "n": len(rows),
        "categories": {
            field: category_summary(rows, field)
            for field in TITLE_CATEGORIES + ALIGNMENT_CATEGORIES
        },
        "scores": {
            field: {
                "median": median(rows, field),
                "vs_log_views": spearman(rows, field, "views", log_y=True),
                "vs_ctr": spearman(rows, field, "reach_ctr"),
                "vs_avp": spearman(rows, field, "averageViewPercentage"),
                "vs_subscribers_per_1000_views": spearman(rows, field, "subscribers_per_1000_views"),
            }
            for field in TITLE_SCORES
        },
        "top_views": [
            {
                "video_id": row["video_id"],
                "views": row["views"],
                "views_per_day": row["views_per_day"],
                "ctr": row["reach_ctr"],
                "avp": row["averageViewPercentage"],
                "title_promise": row["title_promise_type"],
                "outcome_tease": row["title_outcome_tease"],
                "numeric_hook": row["title_numeric_hook"],
                "alignment": row["alignment_type"],
            }
            for row in sorted(rows, key=lambda item: number(item.get("views")), reverse=True)[:5]
        ],
    }


def main() -> int:
    client = bigquery.Client(project=PROJECT_ID)
    rows = load_rows(client)
    if len(rows) != 25 or len({row["video_id"] for row in rows}) != 25:
        raise ValueError(f"expected 25 unique rows, got {len(rows)}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_DIR / "calibration_25_title_thumbnail_performance_join_20260814.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "row_count": len(rows),
        "title_needs_review": sum(bool(row.get("title_needs_review")) for row in rows),
        "alignment_needs_review": sum(bool(row.get("alignment_needs_review")) for row in rows),
        "all": structure_summary(rows),
        "structures": {
            structure: structure_summary([row for row in rows if row.get("primary_structure") == structure])
            for structure in ("P03B", "P06B")
        },
    }
    summary_path = OUTPUT_DIR / "calibration_25_title_thumbnail_performance_summary_20260814.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    table_id = f"{PROJECT_ID}.{DATASET}.manus_title_thumbnail_performance_v1"
    client.load_table_from_json(
        rows, table_id,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE", autodetect=True),
    ).result()
    print(json.dumps({
        "rows": len(rows),
        "title_needs_review": summary["title_needs_review"],
        "alignment_needs_review": summary["alignment_needs_review"],
        "csv": str(csv_path),
        "summary": str(summary_path),
        "bigquery_table": table_id,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
