# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path


HERE = Path(__file__).resolve().parent
DB_PATH = (HERE / ".." / ".." / "analysis" / "competitor_db.sqlite").resolve()
ANALYSIS_PATH = HERE / "thumbnail_analysis_overrides.jsonl"


def main() -> int:
    rows = read_analysis_rows()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(str(DB_PATH))
    try:
        with con:
            ocr_count = analysis_count = tag_count = 0
            for row in rows:
                video_id = row.get("video_id", "").strip()
                if not video_id:
                    continue
                raw_json = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                thumbnail_text = clean(row.get("thumbnail_text", ""))
                con.execute(
                    """
                    INSERT INTO thumbnail_ocr (
                        video_id, combined_text, raw_json, notes, analyzed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(video_id) DO UPDATE SET
                        combined_text=excluded.combined_text,
                        raw_json=excluded.raw_json,
                        notes=excluded.notes,
                        analyzed_at=excluded.analyzed_at
                    """,
                    (video_id, thumbnail_text, raw_json, clean(row.get("notes", "")), now),
                )
                ocr_count += 1

                con.execute(
                    """
                    INSERT INTO thumbnail_analysis (
                        video_id, person_count, person_gender, person_age_group,
                        person_expression, text_amount, layout_type, emotion_appeal,
                        special_elements, raw_json, analyzed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(video_id) DO UPDATE SET
                        person_count=excluded.person_count,
                        person_gender=excluded.person_gender,
                        person_age_group=excluded.person_age_group,
                        person_expression=excluded.person_expression,
                        text_amount=excluded.text_amount,
                        layout_type=excluded.layout_type,
                        emotion_appeal=excluded.emotion_appeal,
                        special_elements=excluded.special_elements,
                        raw_json=excluded.raw_json,
                        analyzed_at=excluded.analyzed_at
                    """,
                    (
                        video_id,
                        infer_person_count(row),
                        clean(row.get("people", "")),
                        "",
                        "",
                        "あり" if thumbnail_text else "なし",
                        clean(row.get("composition", "")),
                        clean(row.get("emotion_appeal", "")),
                        " / ".join(
                            x for x in [
                                clean(row.get("main_subject", "")),
                                clean(row.get("setting", "")),
                                clean(row.get("story_hook", "")),
                            ] if x
                        ),
                        raw_json,
                        now,
                    ),
                )
                analysis_count += 1

                con.execute("DELETE FROM thumbnail_axis_tags WHERE video_id = ? AND source = ?", (video_id, "gemini_thumbnail_analysis"))
                for axis, values in [
                    ("pattern", row.get("pattern_tags", [])),
                    ("visual", row.get("visual_tags", [])),
                    ("search", row.get("search_tags", [])),
                ]:
                    for tag in normalize_tags(values):
                        con.execute(
                            """
                            INSERT OR REPLACE INTO thumbnail_axis_tags (
                                video_id, axis, code, confidence, source
                            ) VALUES (?, ?, ?, ?, ?)
                            """,
                            (video_id, axis, tag, safe_float(row.get("confidence")), "gemini_thumbnail_analysis"),
                        )
                        tag_count += 1
        print(json.dumps({"analysis_rows": analysis_count, "ocr_rows": ocr_count, "axis_tags": tag_count}, ensure_ascii=False))
    finally:
        con.close()
    return 0


def read_analysis_rows() -> list[dict]:
    rows = []
    if not ANALYSIS_PATH.exists():
        return rows
    with ANALYSIS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def clean(value: object) -> str:
    return " ".join(str(value or "").replace("\r", "\n").split())


def normalize_tags(value: object) -> list[str]:
    if isinstance(value, list):
        return [clean(x) for x in value if clean(x)]
    if isinstance(value, str) and value.strip():
        return [clean(x) for x in value.replace("、", ",").split(",") if clean(x)]
    return []


def safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def infer_person_count(row: dict) -> int | None:
    text = clean(row.get("people", ""))
    for n in range(1, 10):
        if str(n) in text or f"{n}人" in text:
            return n
    return None


if __name__ == "__main__":
    raise SystemExit(main())
