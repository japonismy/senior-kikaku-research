# -*- coding: utf-8 -*-
"""Classify 25 calibration titles and judge title/thumbnail alignment with Manus."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import bigquery

from manus_pipeline import (
    DEFAULT_ANCHOR_TASK_ID,
    api_request,
    find_direct_output,
    find_structured_output,
    find_value,
)


PROJECT_ID = "rugged-destiny-408613"
DATASET = "senior_reading_all"
PROFILE = "manus-1.6"
VAULT = Path(r"E:\Data\ObsidianVault")
CALIBRATION_FILE = (
    VAULT / "02_Channels" / "シニア朗読" / "analysis" / "20260813_一次調査"
    / "manus_calibration_v1" / "calibration_25_blind.jsonl"
)
OUTPUT_DIR = (
    VAULT / "02_Channels" / "シニア朗読" / "analysis" / "20260813_一次調査"
    / "manus_calibration_v1" / "title_alignment_20260814"
)
ROOT = Path(__file__).resolve().parent
TITLE_SCHEMA_PATH = ROOT / "title_batch_schema_v1.json"
ALIGNMENT_SCHEMA_PATH = ROOT / "title_thumbnail_alignment_schema_v1.json"
TITLE_BATCH_ID = "title_calibration_25_v1"
ALIGNMENT_BATCH_ID = "title_thumbnail_alignment_25_v1"
TITLE_SCORE_FIELDS = (
    "specificity_score",
    "stakes_clarity",
    "reversal_clarity",
    "curiosity_strength",
    "emotional_intensity",
    "formulaic_risk",
)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def normalize_confidence(value: Any) -> float:
    result = float(value or 0)
    if 1 < result <= 10:
        result /= 10
    elif 10 < result <= 100:
        result /= 100
    return round(result, 4)


def validate_batch(
    payload: dict[str, Any], expected_ids: set[str], batch_id: str, title_batch: bool
) -> list[dict[str, Any]]:
    if payload.get("batch_id") != batch_id:
        raise ValueError(f"batch_id mismatch: {payload.get('batch_id')} != {batch_id}")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("items is not an array")
    actual_ids = [str(item.get("video_id") or "") for item in items]
    if len(actual_ids) != len(expected_ids) or set(actual_ids) != expected_ids or len(actual_ids) != len(set(actual_ids)):
        missing = sorted(expected_ids - set(actual_ids))
        extra = sorted(set(actual_ids) - expected_ids)
        raise ValueError(f"video_id coverage error: count={len(actual_ids)} missing={missing} extra={extra}")
    for item in items:
        item["confidence"] = normalize_confidence(item.get("confidence"))
        if not 0 < item["confidence"] <= 1:
            raise ValueError(f"invalid confidence: {item['video_id']}={item['confidence']}")
        if title_batch:
            if not item.get("evidence"):
                raise ValueError(f"empty evidence: {item['video_id']}")
            for field in TITLE_SCORE_FIELDS:
                value = item.get(field)
                if not isinstance(value, int) or not 0 <= value <= 10:
                    raise ValueError(f"invalid {field}: {item['video_id']}={value}")
        elif not str(item.get("rationale") or "").strip():
            raise ValueError(f"empty rationale: {item['video_id']}")
    payload["batch_confidence"] = normalize_confidence(payload.get("batch_confidence"))
    return items


def log_start(
    client: bigquery.Client, queue_id: str, task_id: str, prompt: str, submitted_after_ms: int
) -> None:
    query = f"""
    INSERT INTO `{PROJECT_ID}.{DATASET}.manus_task_runs`
      (manus_task_id, queue_id, status, agent_profile, request_json, response_json,
       error, created_at, updated_at)
    VALUES
      (@task_id, @queue_id, 'running', @profile, @request_json, '', '',
       CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
    """
    config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("task_id", "STRING", task_id),
        bigquery.ScalarQueryParameter("queue_id", "STRING", queue_id),
        bigquery.ScalarQueryParameter("profile", "STRING", PROFILE),
        bigquery.ScalarQueryParameter("request_json", "STRING", json.dumps({
            "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt_chars": len(prompt),
            "submitted_after_ms": submitted_after_ms,
        }, separators=(",", ":"))),
    ])
    client.query(query, job_config=config).result()


def log_finish(
    client: bigquery.Client, queue_id: str, task_id: str, status: str,
    payload: dict[str, Any] | None = None, error: str = ""
) -> None:
    query = f"""
    UPDATE `{PROJECT_ID}.{DATASET}.manus_task_runs`
    SET status=@status, response_json=@response_json, error=@error,
        updated_at=CURRENT_TIMESTAMP()
    WHERE manus_task_id=@task_id AND queue_id=@queue_id AND status='running'
    """
    config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("status", "STRING", status),
        bigquery.ScalarQueryParameter("response_json", "STRING", json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":"))),
        bigquery.ScalarQueryParameter("error", "STRING", error[:1500]),
        bigquery.ScalarQueryParameter("task_id", "STRING", task_id),
        bigquery.ScalarQueryParameter("queue_id", "STRING", queue_id),
    ])
    client.query(query, job_config=config).result()


def send_and_wait(
    client: bigquery.Client, queue_id: str, prompt: str, schema: dict[str, Any],
    anchor_task_id: str, timeout_sec: int = 900
) -> dict[str, Any]:
    prompt_with_schema = (
        prompt
        + "\n\nReturn only one JSON object matching this schema. Do not add a report or code fence.\nOUTPUT_JSON_SCHEMA:\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )
    submitted_after_ms = int(time.time() * 1000)
    response = api_request("POST", "task.sendMessage", {
        "task_id": anchor_task_id,
        "message": {"content": prompt_with_schema},
        "agent_profile": PROFILE,
        "structured_output_schema": schema,
    })
    task_id = str(find_value(response, "task_id") or "")
    if not task_id:
        raise RuntimeError("task.sendMessage returned no task_id")
    log_start(client, queue_id, task_id, prompt_with_schema, submitted_after_ms)
    required = set(schema.get("required") or [])
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        query = f"task.listMessages?task_id={task_id}&limit=200&order=desc&verbose=true"
        messages = api_request("GET", query)
        output = find_direct_output(messages, submitted_after_ms, required)
        if output is None:
            output = find_structured_output(messages, submitted_after_ms)
        if output is not None:
            if not output.get("success"):
                error = str(output.get("error") or "structured output failed")
                log_finish(client, queue_id, task_id, "failed", error=error)
                raise RuntimeError(error)
            value = output.get("value")
            if not isinstance(value, dict):
                log_finish(client, queue_id, task_id, "failed", error="output value is not an object")
                raise RuntimeError("output value is not an object")
            log_finish(client, queue_id, task_id, "completed", payload=value)
            return value
        time.sleep(15)
    log_finish(client, queue_id, task_id, "running", error="local wait timeout; poll existing task before resubmission")
    raise TimeoutError(f"Manus task timed out locally: {queue_id}")


def load_thumbnails(client: bigquery.Client) -> list[dict[str, Any]]:
    query = f"""
    SELECT video_id, primary_subject, relationship_cue, dominant_emotion,
           composition, visual_promise, curiosity_device, food_visibility,
           emotion_immediacy, story_specificity
    FROM `{PROJECT_ID}.{DATASET}.manus_calibration_thumbnail_performance_v1`
    ORDER BY calibration_id
    """
    return [dict(row) for row in client.query(query).result()]


def load_completed_payload(client: bigquery.Client, queue_id: str) -> dict[str, Any] | None:
    query = f"""
    SELECT response_json
    FROM `{PROJECT_ID}.{DATASET}.manus_task_runs`
    WHERE queue_id=@queue_id AND status='completed' AND response_json != ''
    ORDER BY updated_at DESC
    LIMIT 1
    """
    config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("queue_id", "STRING", queue_id),
    ])
    rows = list(client.query(query, job_config=config).result())
    return json.loads(rows[0]["response_json"]) if rows else None


def title_prompt(inputs: list[dict[str, Any]]) -> str:
    compact = [
        {"video_id": row["video_id"], "title": row["title"]}
        for row in inputs
    ]
    return f"""You are classifying Japanese YouTube titles for the senior long-form reading channel 人生のレシピ.
Analyze each title only. You are not given views, CTR, retention, script labels, or thumbnails. Do not browse the web and do not infer events not promised by the title.

Classify all 25 items exactly once. Scores are integers from 0 to 10. confidence and batch_confidence are decimals from 0 to 1. evidence must quote or precisely point to visible wording in the title. Mark needs_review only for genuine ambiguity.

Definitions:
- protagonist_frame: who the title frames as the main vulnerable or acting party.
- hardship_signal: the main hardship explicitly signaled.
- reversal_device: what the title says or implies will reverse the situation.
- promise_type: the main emotional/narrative payoff promised to the viewer.
- outcome_tease: how the title withholds or previews the result.
- formulaic_risk: likelihood that the title could be swapped with many similar senior-reading stories without changing its identity.

batch_id must be {TITLE_BATCH_ID}.
INPUT_TITLES:
{json.dumps(compact, ensure_ascii=False, separators=(",", ":"))}
"""


def alignment_prompt(
    titles: list[dict[str, Any]], title_items: list[dict[str, Any]], thumbnails: list[dict[str, Any]]
) -> str:
    title_map = {row["video_id"]: row for row in title_items}
    inputs = []
    for thumbnail in thumbnails:
        video_id = thumbnail["video_id"]
        title_item = title_map[video_id]
        inputs.append([
            video_id,
            title_item["protagonist_frame"],
            title_item["hardship_signal"],
            title_item["reversal_device"],
            title_item["promise_type"],
            title_item["outcome_tease"],
            thumbnail["primary_subject"],
            thumbnail["relationship_cue"],
            thumbnail["visual_promise"],
            thumbnail["curiosity_device"],
            thumbnail["food_visibility"],
        ])
    return f"""Judge title/thumbnail promise alignment for 25 Japanese YouTube videos. Use only the supplied title text and blind classifications. You are not given views, CTR, retention, or script labels. Do not browse the web.

Definitions:
- exact: title and thumbnail lead with the same central promise.
- complementary: they show different details that clearly support one story promise.
- partial_mismatch: one emphasizes a major promise or reversal the other does not visually/textually support.
- contradictory: the two promises actively conflict. Use sparingly.
- unclear: evidence is insufficient.

Classify every video_id exactly once. confidence and batch_confidence are decimals from 0 to 1. Keep rationale concise and concrete. missing_visual_element should name the title element absent from the thumbnail, or an empty string when none. recommended_direction must identify the smallest useful change.

batch_id must be {ALIGNMENT_BATCH_ID}.
Each compact input array uses this order:
[video_id, title_protagonist, title_hardship, title_reversal, title_promise, title_outcome_tease, thumbnail_subject, thumbnail_relationship, thumbnail_visual_promise, thumbnail_curiosity, thumbnail_food_visibility]
INPUT_PAIRS:
{json.dumps(inputs, ensure_ascii=False, separators=(",", ":"))}
"""


def write_outputs(
    client: bigquery.Client, titles: list[dict[str, Any]], title_payload: dict[str, Any],
    alignment_payload: dict[str, Any]
) -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    title_map = {row["video_id"]: row for row in titles}
    title_rows = []
    for item in title_payload["items"]:
        row = {"calibration_id": title_map[item["video_id"]]["calibration_id"], "title": title_map[item["video_id"]]["title"], **item}
        row["evidence"] = json.dumps(row["evidence"], ensure_ascii=False)
        title_rows.append(row)
    alignment_rows = []
    for item in alignment_payload["items"]:
        alignment_rows.append({
            "calibration_id": title_map[item["video_id"]]["calibration_id"],
            "title": title_map[item["video_id"]]["title"],
            **item,
        })

    title_json = OUTPUT_DIR / "manus_title_classification_25_20260814.json"
    alignment_json = OUTPUT_DIR / "manus_title_thumbnail_alignment_25_20260814.json"
    title_json.write_text(json.dumps(title_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    alignment_json.write_text(json.dumps(alignment_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    for path, rows in (
        (OUTPUT_DIR / "manus_title_classification_25_20260814.csv", title_rows),
        (OUTPUT_DIR / "manus_title_thumbnail_alignment_25_20260814.csv", alignment_rows),
    ):
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    configs = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE", autodetect=True)
    client.load_table_from_json(title_rows, f"{PROJECT_ID}.{DATASET}.manus_title_calibration_v1", job_config=configs).result()
    client.load_table_from_json(
        alignment_rows,
        f"{PROJECT_ID}.{DATASET}.manus_title_thumbnail_alignment_v1",
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE", autodetect=True),
    ).result()
    return {
        "title_rows": len(title_rows),
        "alignment_rows": len(alignment_rows),
        "title_average_confidence": round(statistics.mean(float(row["confidence"]) for row in title_rows), 4),
        "alignment_average_confidence": round(statistics.mean(float(row["confidence"]) for row in alignment_rows), 4),
        "output_dir": str(OUTPUT_DIR),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anchor-task-id", default=DEFAULT_ANCHOR_TASK_ID)
    parser.add_argument("--timeout-sec", type=int, default=900)
    args = parser.parse_args()

    client = bigquery.Client(project=PROJECT_ID)
    titles = read_jsonl(CALIBRATION_FILE)
    if len(titles) != 25 or len({row["video_id"] for row in titles}) != 25:
        raise ValueError("calibration title input must contain 25 unique video IDs")
    expected_ids = {row["video_id"] for row in titles}
    title_schema = json.loads(TITLE_SCHEMA_PATH.read_text(encoding="utf-8"))
    alignment_schema = json.loads(ALIGNMENT_SCHEMA_PATH.read_text(encoding="utf-8"))

    title_queue_id = f"classify_title_batch:{TITLE_BATCH_ID}"
    title_payload = load_completed_payload(client, title_queue_id)
    if title_payload is None:
        title_payload = send_and_wait(
            client, title_queue_id, title_prompt(titles),
            title_schema, args.anchor_task_id, args.timeout_sec,
        )
    title_items = validate_batch(title_payload, expected_ids, TITLE_BATCH_ID, title_batch=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "manus_title_classification_25_20260814.json").write_text(
        json.dumps(title_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    thumbnails = load_thumbnails(client)
    if {row["video_id"] for row in thumbnails} != expected_ids:
        raise ValueError("thumbnail coverage does not match title calibration set")
    alignment_queue_id = f"classify_alignment_batch:{ALIGNMENT_BATCH_ID}"
    alignment_payload = load_completed_payload(client, alignment_queue_id)
    if alignment_payload is None:
        alignment_payload = send_and_wait(
            client, alignment_queue_id,
            alignment_prompt(titles, title_items, thumbnails), alignment_schema,
            args.anchor_task_id, args.timeout_sec,
        )
    validate_batch(alignment_payload, expected_ids, ALIGNMENT_BATCH_ID, title_batch=False)
    result = write_outputs(client, titles, title_payload, alignment_payload)
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
