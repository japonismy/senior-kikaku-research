# -*- coding: utf-8 -*-
"""Build a blind 25-case calibration set across own-channel performance quintiles."""
from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


VAULT = Path(r"E:\Data\ObsidianVault")
LIB_DIR = (
    VAULT / "02_Channels" / "シニア朗読" / "企画戦略" / "長尺化・重複回避リライト戦略"
    / "03_根拠データ・分析" / "長尺構造型ライブラリ_20260808"
)
AUDIT_DIR = VAULT / "02_Channels" / "シニア朗読" / "企画戦略" / "台本振り返り監査" / "runs" / "20260808"
OUTPUT_DIR = VAULT / "02_Channels" / "シニア朗読" / "analysis" / "20260813_一次調査" / "manus_calibration_v1"


def read_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    performance = [row for row in read_jsonl(LIB_DIR / "17_own_structure_performance.jsonl") if row.get("video_id")]
    structures = {
        row["audit_id"]: row.get("classification") or {}
        for row in read_jsonl(LIB_DIR / "16_own_structure_classifications.jsonl")
    }
    cases = {
        data["audit_id"]: (path, data)
        for path in sorted((AUDIT_DIR / "cases").glob("*/case.json"))
        for data in [json.loads(path.read_text(encoding="utf-8"))]
    }

    candidates = []
    for row in performance:
        classification = structures.get(row["audit_id"]) or {}
        source_path, case = cases[row["audit_id"]]
        candidates.append({
            **row,
            "source_case": str(source_path),
            "script_chars": len(case.get("script") or ""),
            "primary_cluster": classification.get("primary_cluster") or row.get("primary_cluster") or "",
            "director_card": classification.get("director_card") or row.get("director_card") or "",
            "food_role": (classification.get("food_role") or {}).get("level") or row.get("food_role") or "",
        })

    candidates.sort(key=lambda row: float(row.get("lifetime_views") or 0))
    combo_counts = Counter((row["primary_cluster"], row["director_card"], row["food_role"]) for row in candidates)
    buckets = [[] for _ in range(5)]
    for index, row in enumerate(candidates):
        bucket = min(4, index * 5 // len(candidates))
        row["performance_quintile"] = bucket + 1
        buckets[bucket].append(row)
    for bucket in buckets:
        bucket.sort(key=lambda row: (
            combo_counts[(row["primary_cluster"], row["director_card"], row["food_role"])],
            row["primary_cluster"], row["director_card"], row["management_number"],
        ))
    selected = [row for bucket in buckets for row in bucket[:5]]

    blind_rows = [{
        "calibration_id": f"CAL-{index:02d}",
        "audit_id": row["audit_id"],
        "video_id": row["video_id"],
        "management_number": row["management_number"],
        "title": row["title"],
        "source_case": row["source_case"],
        "script_chars": row["script_chars"],
    } for index, row in enumerate(selected, 1)]
    key_rows = [{
        "calibration_id": f"CAL-{index:02d}",
        "audit_id": row["audit_id"],
        "video_id": row["video_id"],
        "performance_quintile": row["performance_quintile"],
        "primary_cluster_prior": row["primary_cluster"],
        "director_card_prior": row["director_card"],
        "food_role_prior": row["food_role"],
        "views_7d": row.get("views_7d"),
        "views_28d": row.get("views_28d"),
        "lifetime_views": row.get("lifetime_views"),
        "D7_CTR_percent": row.get("D7_CTR_percent"),
        "avp_7d": row.get("avp_7d"),
    } for index, row in enumerate(selected, 1)]

    write_jsonl(OUTPUT_DIR / "calibration_25_blind.jsonl", blind_rows)
    write_jsonl(OUTPUT_DIR / "calibration_25_performance_key.jsonl", key_rows)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "taxonomy_version": "jinsei_recipe_v1",
        "total_candidates": len(candidates),
        "selected": len(selected),
        "selection_method": "5 performance quintiles x 5; within each quintile prioritize rare prior structure/card/food combinations",
        "blindness": "Manus input excludes views, CTR, retention and prior classifications.",
        "quintile_counts": dict(Counter(str(row["performance_quintile"]) for row in selected)),
        "prior_cluster_counts": dict(Counter(row["primary_cluster"] for row in selected)),
        "prior_card_counts": dict(Counter(row["director_card"] for row in selected)),
        "files": {
            "blind": "calibration_25_blind.jsonl",
            "performance_key": "calibration_25_performance_key.jsonl"
        }
    }
    (OUTPUT_DIR / "calibration_25_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
