# -*- coding: utf-8 -*-
"""Import completed own-channel scripts and prior blind classifications to BigQuery."""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import bigquery


PROJECT_ID = "rugged-destiny-408613"
DATASET = "senior_reading_all"
VAULT = Path(r"E:\Data\ObsidianVault")
LIB_DIR = (
    VAULT / "02_Channels" / "シニア朗読" / "企画戦略" / "長尺化・重複回避リライト戦略"
    / "03_根拠データ・分析" / "長尺構造型ライブラリ_20260808"
)
AUDIT_DIR = VAULT / "02_Channels" / "シニア朗読" / "企画戦略" / "台本振り返り監査" / "runs" / "20260808"
TOOLS_REPO = VAULT / "04_Tools" / "senior-kikaku-research"
CALIBRATION_DIR = VAULT / "02_Channels" / "シニア朗読" / "analysis" / "20260813_一次調査" / "manus_calibration_v1"
EXTENSION_CANDIDATES_PATH = CALIBRATION_DIR / "追加校正候補_不足P型5本_20260813.json"
OVERRIDES_PATH = LIB_DIR / "17a_own_structure_human_overrides.json"
sys.path.insert(0, str(TOOLS_REPO / "cloud_batch"))
import daily_youtube_metadata_update as daily_job  # noqa: E402


def read_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def load_rows() -> tuple[list[dict], list[dict]]:
    performance = {row["audit_id"]: row for row in read_jsonl(LIB_DIR / "17_own_structure_performance.jsonl")}
    classifications = {row["audit_id"]: row for row in read_jsonl(LIB_DIR / "16_own_structure_classifications.jsonl")}
    overrides = {
        row["audit_id"]: row
        for row in json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    }
    transcript_rows: list[dict] = []
    structure_rows: list[dict] = []
    imported_at = datetime.now(timezone.utc).isoformat()

    for path in sorted((AUDIT_DIR / "cases").glob("*/case.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        audit_id = case["audit_id"]
        perf = performance.get(audit_id) or {}
        classification = classifications.get(audit_id) or {}
        video_id = perf.get("video_id") or ""
        script = case.get("script") or ""
        if video_id and script:
            transcript_rows.append({
                "video_id": video_id,
                "transcript_text": script,
                "transcript_json": json.dumps({"audit_id": audit_id, "source_case": str(path)}, ensure_ascii=False),
                "language": "ja",
                "source": "local_completed_script",
                "fetched_at": imported_at,
            })
        if classification:
            raw_classification = classification.get("classification") or {}
            structure_rows.append({
                "audit_id": audit_id,
                "video_id": video_id,
                "management_number": str(case.get("management_number") or ""),
                "title": case.get("title") or "",
                "taxonomy_version": "p_r_blind_20260808",
                "model": classification.get("model") or "",
                "classification_json": json.dumps(raw_classification, ensure_ascii=False),
                "classified_at": classification.get("classified_at") or imported_at,
                "source_case": str(path),
                "imported_at": imported_at,
            })
            effective_classification = dict(raw_classification)
            for key in ("primary_cluster", "director_card", "food_role", "structure_fingerprint", "similarity_risk_signature"):
                if perf.get(key) not in (None, ""):
                    effective_classification[key] = perf[key]
            override = overrides.get(audit_id) or {}
            for key in ("primary_cluster", "director_card", "food_role"):
                if override.get(key) not in (None, ""):
                    effective_classification[key] = override[key]
            effective_classification["human_override_reason"] = override.get("reason") or perf.get("human_override_reason")
            structure_rows.append({
                "audit_id": audit_id,
                "video_id": video_id,
                "management_number": str(case.get("management_number") or ""),
                "title": case.get("title") or "",
                "taxonomy_version": "p_r_effective_20260808",
                "model": classification.get("model") or "",
                "classification_json": json.dumps(effective_classification, ensure_ascii=False),
                "classified_at": classification.get("classified_at") or imported_at,
                "source_case": str(path),
                "imported_at": imported_at,
            })
    return transcript_rows, structure_rows


def merge_transcripts(client: bigquery.Client, rows: list[dict]) -> None:
    temp = f"{PROJECT_ID}.{DATASET}._tmp_own_transcripts_{uuid.uuid4().hex}"
    schema = [
        bigquery.SchemaField("video_id", "STRING"),
        bigquery.SchemaField("transcript_text", "STRING"),
        bigquery.SchemaField("transcript_json", "STRING"),
        bigquery.SchemaField("language", "STRING"),
        bigquery.SchemaField("source", "STRING"),
        bigquery.SchemaField("fetched_at", "STRING"),
    ]
    client.load_table_from_json(rows, temp, job_config=bigquery.LoadJobConfig(schema=schema)).result()
    sql = f"""
    MERGE `{PROJECT_ID}.{DATASET}.analysis_competitor_db__transcripts` T
    USING `{temp}` S ON T.video_id=S.video_id
    WHEN MATCHED THEN UPDATE SET
      transcript_text=S.transcript_text, transcript_json=S.transcript_json,
      language=S.language, source=S.source, fetched_at=S.fetched_at
    WHEN NOT MATCHED THEN INSERT
      (video_id, transcript_text, transcript_json, language, source, fetched_at)
    VALUES
      (S.video_id, S.transcript_text, S.transcript_json, S.language, S.source, S.fetched_at)
    """
    try:
        client.query(sql).result()
    finally:
        client.delete_table(temp, not_found_ok=True)


def merge_structures(client: bigquery.Client, rows: list[dict]) -> None:
    temp = f"{PROJECT_ID}.{DATASET}._tmp_own_structures_{uuid.uuid4().hex}"
    schema = [
        bigquery.SchemaField("audit_id", "STRING"),
        bigquery.SchemaField("video_id", "STRING"),
        bigquery.SchemaField("management_number", "STRING"),
        bigquery.SchemaField("title", "STRING"),
        bigquery.SchemaField("taxonomy_version", "STRING"),
        bigquery.SchemaField("model", "STRING"),
        bigquery.SchemaField("classification_json", "STRING"),
        bigquery.SchemaField("classified_at", "TIMESTAMP"),
        bigquery.SchemaField("source_case", "STRING"),
        bigquery.SchemaField("imported_at", "TIMESTAMP"),
    ]
    client.load_table_from_json(rows, temp, job_config=bigquery.LoadJobConfig(schema=schema)).result()
    target = f"{PROJECT_ID}.{DATASET}.own_script_structure_classifications"
    sql = f"""
    MERGE `{target}` T
    USING `{temp}` S ON T.audit_id=S.audit_id AND T.taxonomy_version=S.taxonomy_version
    WHEN MATCHED THEN UPDATE SET
      video_id=NULLIF(S.video_id, ''), management_number=S.management_number, title=S.title,
      model=S.model, classification_json=S.classification_json, classified_at=S.classified_at,
      source_case=S.source_case, imported_at=S.imported_at
    WHEN NOT MATCHED THEN INSERT
      (audit_id, video_id, management_number, title, taxonomy_version, model,
       classification_json, classified_at, source_case, imported_at)
    VALUES
      (S.audit_id, NULLIF(S.video_id, ''), S.management_number, S.title, S.taxonomy_version, S.model,
       S.classification_json, S.classified_at, S.source_case, S.imported_at)
    """
    try:
        client.query(sql).result()
    finally:
        client.delete_table(temp, not_found_ok=True)


def merge_calibration(client: bigquery.Client) -> int:
    path = CALIBRATION_DIR / "calibration_25_blind.jsonl"
    if not path.exists():
        return 0
    selected_at = datetime.now(timezone.utc).isoformat()
    rows = []
    for row in read_jsonl(path):
        rows.append({
            "calibration_id": row["calibration_id"],
            "batch_id": "calibration_25_v1",
            "audit_id": row["audit_id"],
            "video_id": row["video_id"],
            "title": row["title"],
            "source_case": row["source_case"],
            "script_chars": row["script_chars"],
            "taxonomy_version": "jinsei_recipe_v1",
            "status": "selected",
            "selected_at": selected_at,
            "updated_at": selected_at,
        })
    temp = f"{PROJECT_ID}.{DATASET}._tmp_calibration_{uuid.uuid4().hex}"
    schema = [
        bigquery.SchemaField("calibration_id", "STRING"),
        bigquery.SchemaField("batch_id", "STRING"),
        bigquery.SchemaField("audit_id", "STRING"),
        bigquery.SchemaField("video_id", "STRING"),
        bigquery.SchemaField("title", "STRING"),
        bigquery.SchemaField("source_case", "STRING"),
        bigquery.SchemaField("script_chars", "INT64"),
        bigquery.SchemaField("taxonomy_version", "STRING"),
        bigquery.SchemaField("status", "STRING"),
        bigquery.SchemaField("selected_at", "TIMESTAMP"),
        bigquery.SchemaField("updated_at", "TIMESTAMP"),
    ]
    client.load_table_from_json(rows, temp, job_config=bigquery.LoadJobConfig(schema=schema)).result()
    target = f"{PROJECT_ID}.{DATASET}.research_calibration_cases"
    sql = f"""
    MERGE `{target}` T
    USING `{temp}` S ON T.calibration_id=S.calibration_id AND T.batch_id=S.batch_id
    WHEN MATCHED THEN UPDATE SET
      audit_id=S.audit_id, video_id=S.video_id, title=S.title, source_case=S.source_case,
      script_chars=S.script_chars, taxonomy_version=S.taxonomy_version, updated_at=S.updated_at
    WHEN NOT MATCHED THEN INSERT
      (calibration_id, batch_id, audit_id, video_id, title, source_case, script_chars,
       taxonomy_version, status, selected_at, updated_at)
    VALUES
      (S.calibration_id, S.batch_id, S.audit_id, S.video_id, S.title, S.source_case, S.script_chars,
       S.taxonomy_version, S.status, S.selected_at, S.updated_at)
    """
    try:
        client.query(sql).result()
    finally:
        client.delete_table(temp, not_found_ok=True)
    return len(rows)


def merge_extension_candidates(client: bigquery.Client) -> int:
    if not EXTENSION_CANDIDATES_PATH.exists():
        return 0
    payload = json.loads(EXTENSION_CANDIDATES_PATH.read_text(encoding="utf-8"))
    selected_at = payload.get("created_at") or datetime.now(timezone.utc).isoformat()
    rows = []
    for index, candidate in enumerate(payload.get("candidates") or [], start=1):
        rows.append({
            "calibration_id": f"EXT-CAL-{index:02d}",
            "batch_id": "calibration_extension_missing_p_v1",
            "audit_id": candidate["audit_id"],
            "management_number": candidate.get("management_number") or "",
            "title": candidate["title"],
            "source_case": candidate["source_case"],
            "prior_primary_structure": candidate["prior_primary_structure"],
            "prior_director_card": candidate["prior_director_card"],
            "prior_food_role": candidate["prior_food_role"],
            "taxonomy_version": "jinsei_recipe_v1",
            "status": "selected",
            "selected_at": selected_at,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
    if not rows:
        return 0
    schema = [
        bigquery.SchemaField("calibration_id", "STRING"),
        bigquery.SchemaField("batch_id", "STRING"),
        bigquery.SchemaField("audit_id", "STRING"),
        bigquery.SchemaField("management_number", "STRING"),
        bigquery.SchemaField("title", "STRING"),
        bigquery.SchemaField("source_case", "STRING"),
        bigquery.SchemaField("prior_primary_structure", "STRING"),
        bigquery.SchemaField("prior_director_card", "STRING"),
        bigquery.SchemaField("prior_food_role", "STRING"),
        bigquery.SchemaField("taxonomy_version", "STRING"),
        bigquery.SchemaField("status", "STRING"),
        bigquery.SchemaField("selected_at", "TIMESTAMP"),
        bigquery.SchemaField("updated_at", "TIMESTAMP"),
    ]
    target = f"{PROJECT_ID}.{DATASET}.research_calibration_extension_candidates"
    client.query(f"""
    CREATE TABLE IF NOT EXISTS `{target}` (
      calibration_id STRING, batch_id STRING, audit_id STRING, management_number STRING,
      title STRING, source_case STRING, prior_primary_structure STRING,
      prior_director_card STRING, prior_food_role STRING, taxonomy_version STRING,
      status STRING, selected_at TIMESTAMP, updated_at TIMESTAMP
    )
    """).result()
    temp = f"{PROJECT_ID}.{DATASET}._tmp_calibration_extension_{uuid.uuid4().hex}"
    client.load_table_from_json(rows, temp, job_config=bigquery.LoadJobConfig(schema=schema)).result()
    try:
        client.query(f"""
        MERGE `{target}` T
        USING `{temp}` S ON T.calibration_id=S.calibration_id AND T.batch_id=S.batch_id
        WHEN MATCHED THEN UPDATE SET
          audit_id=S.audit_id, management_number=S.management_number, title=S.title,
          source_case=S.source_case, prior_primary_structure=S.prior_primary_structure,
          prior_director_card=S.prior_director_card, prior_food_role=S.prior_food_role,
          taxonomy_version=S.taxonomy_version, status=S.status, updated_at=S.updated_at
        WHEN NOT MATCHED THEN INSERT
          (calibration_id, batch_id, audit_id, management_number, title, source_case,
           prior_primary_structure, prior_director_card, prior_food_role, taxonomy_version,
           status, selected_at, updated_at)
        VALUES
          (S.calibration_id, S.batch_id, S.audit_id, S.management_number, S.title, S.source_case,
           S.prior_primary_structure, S.prior_director_card, S.prior_food_role, S.taxonomy_version,
           S.status, S.selected_at, S.updated_at)
        """).result()
    finally:
        client.delete_table(temp, not_found_ok=True)
    return len(rows)


def create_calibration_review_view(client: bigquery.Client) -> None:
    """Keep the review one-row-per-case even if concurrent collectors wrote twice."""
    sql = f"""
    CREATE OR REPLACE VIEW `{PROJECT_ID}.{DATASET}.manus_calibration_review_v1` AS
    WITH prior AS (
      SELECT * EXCEPT(rn)
      FROM (
        SELECT p.*, ROW_NUMBER() OVER (
          PARTITION BY audit_id, taxonomy_version ORDER BY imported_at DESC
        ) AS rn
        FROM `{PROJECT_ID}.{DATASET}.own_script_structure_classifications` p
        WHERE taxonomy_version='p_r_effective_20260808'
      )
      WHERE rn=1
    ), results AS (
      SELECT * EXCEPT(rn)
      FROM (
        SELECT r.*, ROW_NUMBER() OVER (
          PARTITION BY entity_id, task_type, taxonomy_version ORDER BY updated_at DESC, created_at DESC
        ) AS rn
        FROM `{PROJECT_ID}.{DATASET}.manus_classification_results` r
        WHERE task_type='classify_story'
      )
      WHERE rn=1
    )
    SELECT
      k.calibration_id, k.batch_id, k.video_id, k.title,
      JSON_VALUE(p.classification_json, '$.primary_cluster') AS prior_primary_structure,
      JSON_VALUE(p.classification_json, '$.director_card') AS prior_director_card,
      JSON_VALUE(p.classification_json, '$.food_role') AS prior_food_role,
      JSON_VALUE(r.result_json, '$.primary_structure') AS manus_primary_structure,
      JSON_VALUE(r.result_json, '$.director_card') AS manus_director_card,
      JSON_VALUE(r.result_json, '$.food_role') AS manus_food_role,
      r.confidence, r.needs_review AS manus_needs_review,
      JSON_VALUE(p.classification_json, '$.primary_cluster') = JSON_VALUE(r.result_json, '$.primary_structure') AS primary_agreement,
      JSON_VALUE(p.classification_json, '$.director_card') = JSON_VALUE(r.result_json, '$.director_card') AS card_agreement,
      JSON_VALUE(p.classification_json, '$.food_role') = JSON_VALUE(r.result_json, '$.food_role') AS food_role_agreement,
      r.entity_id IS NOT NULL AND (
        r.needs_review OR COALESCE(r.confidence, 0) < 0.75
          OR JSON_VALUE(p.classification_json, '$.primary_cluster') != JSON_VALUE(r.result_json, '$.primary_structure')
          OR JSON_VALUE(p.classification_json, '$.director_card') != JSON_VALUE(r.result_json, '$.director_card')
          OR JSON_VALUE(p.classification_json, '$.food_role') != JSON_VALUE(r.result_json, '$.food_role')
      ) AS needs_human_calibration_review,
      r.result_json, r.updated_at
    FROM `{PROJECT_ID}.{DATASET}.research_calibration_cases` k
    LEFT JOIN prior p ON p.audit_id=k.audit_id
    LEFT JOIN results r
      ON r.entity_id=k.video_id AND r.taxonomy_version=k.taxonomy_version
    """
    client.query(sql).result()


def main() -> int:
    client = bigquery.Client(project=PROJECT_ID)
    daily_job.ensure_research_tables(client)
    transcripts, structures = load_rows()
    merge_transcripts(client, transcripts)
    merge_structures(client, structures)
    calibration_rows = merge_calibration(client)
    extension_candidates = merge_extension_candidates(client)
    daily_job.refresh_research_coverage_and_queue(client)
    create_calibration_review_view(client)
    print(json.dumps({
        "transcripts_imported": len(transcripts),
        "structures_imported": len(structures),
        "mapped_structures": sum(bool(row["video_id"]) for row in structures),
        "calibration_rows": calibration_rows,
        "extension_candidates": extension_candidates,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
