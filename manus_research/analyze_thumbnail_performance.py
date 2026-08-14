# -*- coding: utf-8 -*-
"""Join Manus thumbnail classifications to calibrated story performance."""
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
    / "manus_calibration_v1" / "thumbnail_performance_20260814"
)
CATEGORICAL_FIELDS = (
    "subject_count",
    "primary_subject",
    "relationship_cue",
    "dominant_emotion",
    "gaze_pattern",
    "composition",
    "text_density",
    "visual_promise",
    "curiosity_device",
    "food_visibility",
    "background_type",
)
SCORE_FIELDS = (
    "mobile_legibility",
    "emotion_immediacy",
    "story_specificity",
    "generic_ai_image_risk",
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
    pairs: list[tuple[float, float]] = []
    for row in rows:
        if row.get(x_key) in (None, "") or row.get(y_key) in (None, ""):
            continue
        x_value = number(row[x_key])
        y_value = number(row[y_key])
        pairs.append((x_value, math.log1p(max(y_value, 0))) if log_y else (x_value, y_value))
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
    WITH thumbnails AS (
      SELECT entity_id AS video_id, result_json, confidence thumbnail_confidence,
             needs_review thumbnail_needs_review
      FROM `{PROJECT_ID}.{DATASET}.manus_classification_results`
      WHERE task_type='classify_thumbnail'
      QUALIFY ROW_NUMBER() OVER (PARTITION BY entity_id ORDER BY created_at DESC)=1
    )
    SELECT p.*, t.result_json, t.thumbnail_confidence, t.thumbnail_needs_review
    FROM `{PROJECT_ID}.{DATASET}.manus_calibration_performance_v1` p
    JOIN thumbnails t USING (video_id)
    ORDER BY p.calibration_id
    """
    output: list[dict[str, Any]] = []
    for source in client.query(query).result():
        row = dict(source)
        for key, value in list(row.items()):
            if isinstance(value, (datetime,)):
                row[key] = value.isoformat()
        result = json.loads(row.pop("result_json") or "{}")
        for field in CATEGORICAL_FIELDS + SCORE_FIELDS:
            row[field] = result.get(field)
        row["thumbnail_strengths"] = json.dumps(result.get("strengths") or [], ensure_ascii=False)
        row["thumbnail_weaknesses"] = json.dumps(result.get("weaknesses") or [], ensure_ascii=False)
        row["thumbnail_evidence"] = json.dumps(result.get("evidence") or [], ensure_ascii=False)
        output.append(row)
    return output


def category_summary(rows: list[dict[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(field) or "unknown")].append(row)
    return sorted(
        [
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
        ],
        key=lambda item: (-item["n"], item["value"]),
    )


def structure_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "n": len(rows),
        "categories": {field: category_summary(rows, field) for field in CATEGORICAL_FIELDS},
        "scores": {
            field: {
                "median": median(rows, field),
                "vs_log_views": spearman(rows, field, "views", log_y=True),
                "vs_ctr": spearman(rows, field, "reach_ctr"),
                "vs_avp": spearman(rows, field, "averageViewPercentage"),
                "vs_subscribers_per_1000_views": spearman(rows, field, "subscribers_per_1000_views"),
            }
            for field in SCORE_FIELDS
        },
        "top_ctr": [
            {
                "video_id": row["video_id"],
                "ctr": row["reach_ctr"],
                "views": row["views"],
                "avp": row["averageViewPercentage"],
                "composition": row["composition"],
                "visual_promise": row["visual_promise"],
                "curiosity_device": row["curiosity_device"],
                "emotion_immediacy": row["emotion_immediacy"],
                "story_specificity": row["story_specificity"],
                "generic_ai_image_risk": row["generic_ai_image_risk"],
            }
            for row in sorted(rows, key=lambda item: number(item.get("reach_ctr")), reverse=True)[:5]
        ],
    }


def main() -> int:
    client = bigquery.Client(project=PROJECT_ID)
    rows = load_rows(client)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    csv_path = OUTPUT_DIR / "calibration_25_thumbnail_performance_join_20260814.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "row_count": len(rows),
        "needs_review_count": sum(bool(row.get("thumbnail_needs_review")) for row in rows),
        "average_confidence": round(statistics.mean(number(row.get("thumbnail_confidence")) for row in rows), 4),
        "all": structure_summary(rows),
        "structures": {
            structure: structure_summary([row for row in rows if row.get("primary_structure") == structure])
            for structure in ("P03B", "P06B")
        },
    }
    summary_path = OUTPUT_DIR / "calibration_25_thumbnail_performance_summary_20260814.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    table_id = f"{PROJECT_ID}.{DATASET}.manus_calibration_thumbnail_performance_v1"
    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE", autodetect=True)
    client.load_table_from_json(rows, table_id, job_config=job_config).result()
    print(json.dumps({
        "rows": len(rows),
        "needs_review": summary["needs_review_count"],
        "average_confidence": summary["average_confidence"],
        "csv": str(csv_path),
        "summary": str(summary_path),
        "bigquery_table": table_id,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
