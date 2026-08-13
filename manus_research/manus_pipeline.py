# -*- coding: utf-8 -*-
"""Submit and collect structured research tasks through Manus API v2.

The API key is read from MANUS_API_KEY or the Windows user environment registry.
It is never printed or written to BigQuery.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid
from pathlib import Path
from typing import Any


PROJECT_ID = "rugged-destiny-408613"
DATASET = "senior_reading_all"
BASE_URL = "https://api.manus.ai/v2"
DEFAULT_ANCHOR_TASK_ID = "eMrKZvhdv3vA9JDFKBEgww"
ROOT = Path(__file__).resolve().parent
SCHEMAS = {
    "classify_story": ROOT / "story_schema_v1.json",
    "classify_thumbnail": ROOT / "thumbnail_schema_v1.json",
}
CLASSIFICATION_DEFINITIONS = ROOT / "classification_definitions_v1.json"


def load_api_key() -> str:
    value = os.environ.get("MANUS_API_KEY", "").strip()
    if value:
        return value
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value = str(winreg.QueryValueEx(key, "MANUS_API_KEY")[0]).strip()
                if value:
                    return value
        except (FileNotFoundError, OSError):
            pass
    raise RuntimeError("MANUS_API_KEY is not configured.")


def bq_query(sql: str, parameters: dict[str, str] | None = None, json_output: bool = False) -> Any:
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise RuntimeError(
            r"google-cloud-bigquery is required. Run with E:\Data\ObsidianVault\04_Tools\envs\senior_reading\Scripts\python.exe"
        ) from exc
    client = bigquery.Client(project=PROJECT_ID)
    config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(name, "STRING", value)
            for name, value in (parameters or {}).items()
        ]
    )
    rows = list(client.query(sql, job_config=config).result())
    if json_output:
        return [dict(row) for row in rows]
    return ""


def api_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{BASE_URL}/{path}",
        data=body,
        method=method,
        headers={
            "x-manus-api-key": load_api_key(),
            "Content-Type": "application/json",
            "User-Agent": "senior-kikaku-research/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
            error = detail.get("error") or detail
            code = error.get("code") or f"HTTP_{exc.code}"
            message = error.get("message") or raw
        except json.JSONDecodeError:
            code = f"HTTP_{exc.code}"
            message = raw
        raise RuntimeError(f"Manus API: {code}: {message[:1500]}") from exc
    if result.get("ok") is False:
        error = result.get("error") or {}
        raise RuntimeError(f"Manus API: {error.get('code', 'error')}: {error.get('message', '')}")
    return result


def find_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = find_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_value(child, key)
            if found is not None:
                return found
    return None


def find_structured_output(value: Any, after_timestamp_ms: int = 0) -> dict[str, Any] | None:
    messages = value.get("messages", []) if isinstance(value, dict) else []
    for message in messages:
        if int(message.get("timestamp") or 0) < after_timestamp_ms:
            continue
        if message.get("type") == "structured_output_result":
            result = message.get("structured_output_result")
            return result if isinstance(result, dict) else None
    return None


def extract_json_object(text: str) -> dict[str, Any] | None:
    value = (text or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[1] if "\n" in value else value
        if value.endswith("```"):
            value = value[:-3].strip()
    start = value.find("{")
    if start < 0:
        return None
    try:
        parsed, _ = json.JSONDecoder(strict=False).raw_decode(value[start:])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def find_direct_output(value: Any, after_timestamp_ms: int, required_keys: set[str]) -> dict[str, Any] | None:
    messages = value.get("messages", []) if isinstance(value, dict) else []
    stopped = any(
        int(message.get("timestamp") or 0) >= after_timestamp_ms
        and message.get("type") == "status_update"
        and (message.get("status_update") or {}).get("agent_status") == "stopped"
        for message in messages
    )
    if not stopped:
        return None
    for message in messages:
        if int(message.get("timestamp") or 0) < after_timestamp_ms:
            continue
        if message.get("type") != "assistant_message":
            continue
        parsed = extract_json_object((message.get("assistant_message") or {}).get("content") or "")
        if parsed is not None and required_keys.issubset(parsed):
            return {"success": True, "value": parsed, "error": None}
    return None


def fetch_pending(task_type: str, limit: int) -> list[dict[str, Any]]:
    sql = f"""
    WITH effective_prior AS (
      SELECT
        video_id,
        JSON_VALUE(classification_json, '$.primary_cluster') AS prior_primary_structure,
        JSON_VALUE(classification_json, '$.director_card') AS prior_director_card
      FROM `{PROJECT_ID}.{DATASET}.own_script_structure_classifications`
      WHERE taxonomy_version='p_r_effective_20260808' AND video_id IS NOT NULL
      QUALIFY ROW_NUMBER() OVER (PARTITION BY video_id ORDER BY imported_at DESC)=1
    ), completed_by_pair AS (
      SELECT
        p.prior_primary_structure,
        p.prior_director_card,
        COUNT(DISTINCT r.entity_id) AS completed_count
      FROM `{PROJECT_ID}.{DATASET}.manus_classification_results` r
      JOIN effective_prior p ON p.video_id=r.entity_id
      WHERE r.task_type='classify_story'
      GROUP BY 1, 2
    ), completed_by_primary AS (
      SELECT
        p.prior_primary_structure,
        COUNT(DISTINCT r.entity_id) AS completed_count
      FROM `{PROJECT_ID}.{DATASET}.manus_classification_results` r
      JOIN effective_prior p ON p.video_id=r.entity_id
      WHERE r.task_type='classify_story'
      GROUP BY 1
    )
    SELECT
      q.queue_id, q.entity_id AS video_id, q.channel_id, q.task_type,
      q.taxonomy_version, q.priority, c.title, c.thumbnail_gcs_uri,
      v.thumbnail_url, COALESCE(t.transcript_text, '') AS transcript_text
    FROM `{PROJECT_ID}.{DATASET}.research_processing_queue` q
    JOIN `{PROJECT_ID}.{DATASET}.research_data_coverage` c
      ON c.video_id = q.entity_id
    JOIN `{PROJECT_ID}.{DATASET}.analysis_competitor_db__videos` v
      ON v.video_id = q.entity_id
    LEFT JOIN `{PROJECT_ID}.{DATASET}.analysis_competitor_db__transcripts` t
      ON t.video_id = q.entity_id
    LEFT JOIN effective_prior p ON p.video_id=q.entity_id
    LEFT JOIN completed_by_pair d
      ON d.prior_primary_structure=p.prior_primary_structure
     AND d.prior_director_card=p.prior_director_card
    LEFT JOIN completed_by_primary s
      ON s.prior_primary_structure=p.prior_primary_structure
    WHERE q.status = 'pending'
      AND q.task_type = @task_type
    QUALIFY ROW_NUMBER() OVER (PARTITION BY q.entity_id ORDER BY LENGTH(COALESCE(t.transcript_text, '')) DESC) = 1
    ORDER BY COALESCE(s.completed_count, 0), COALESCE(d.completed_count, 0),
             q.priority DESC, c.is_self DESC, c.view_count DESC
    LIMIT {int(limit)}
    """
    return bq_query(sql, {"task_type": task_type}, json_output=True)


def make_prompt(row: dict[str, Any]) -> str:
    video_id = row["video_id"]
    title = row.get("title") or ""
    if row["task_type"] == "classify_thumbnail":
        url = row.get("thumbnail_url") or ""
        return f"""You are classifying a YouTube thumbnail for the Japanese senior long-form reading channel research database.
Open and visually inspect the thumbnail URL. Classify only what is visibly supported by the image; do not infer from performance metrics. The four scores must be integers from 0 to 10, never 0-to-1 decimals or percentages. Write Japanese strings in strengths, weaknesses, evidence, and review_reason.
Do not create or attach a report file. In your final response, explicitly state every requested classification field and its value, including all four scores, confidence, needs_review, and the evidence arrays. Never leave a meaningful score at 0 merely because it was omitted from your prose.

video_id: {video_id}
title (context only): {title}
thumbnail_url: {url}
"""
    transcript = row.get("transcript_text") or ""
    definitions = CLASSIFICATION_DEFINITIONS.read_text(encoding="utf-8")
    return f"""あなたは「人生のレシピ」の長尺感動朗読を構造分類します。
表面題材ではなく、誰の行動で何が変わるかという因果骨格を分類してください。再生数、CTR、維持率は与えられていません。台本にない出来事を補わないでください。各スコアは0〜10です。evidenceには判定根拠となる出来事を短い日本語で書いてください。
レポートファイルを作成・添付しないでください。最終回答の本文に、次の全項目名と値を明記してください: video_id, emotional_contract, primary_structure, director_card, rescue_direction, midpoint_mechanism, climax_resolution, ending_reward, food_role, plot_fingerprintの全8項目, similarity_risk_signature, novelty_sources, evidence, hook_strength, midpoint_strength, ending_satisfaction, exchangeability_risk, confidence, needs_review, review_reason。
0〜10の4スコアとconfidenceを省略しないでください。分析できたのに本文で省略したという理由で0を使わないでください。evidence、repeated_actions、similarity_risk_signature、novelty_sourcesは少なくとも1件書いてください。
primary_structureとdirector_cardは、次の定義本文を読み、主要事件が条件を満たすものだけを選んでください。名称の印象だけで選ばないでください。特に長期養育がない物語をR3へ分類してはいけません。

CLASSIFICATION_DEFINITIONS:
{definitions}

video_id: {video_id}
title: {title}
transcript:
{transcript}
"""


def set_queue_running(row: dict[str, Any], task_id: str, profile: str, request_record: dict[str, Any]) -> None:
    sql = f"""
    UPDATE `{PROJECT_ID}.{DATASET}.research_processing_queue`
    SET status='running', attempt_count=attempt_count+1, manus_task_id=@task_id,
        last_error='', updated_at=CURRENT_TIMESTAMP()
    WHERE queue_id=@queue_id;
    INSERT INTO `{PROJECT_ID}.{DATASET}.manus_task_runs`
      (manus_task_id, queue_id, status, agent_profile, request_json, response_json, error, created_at, updated_at)
    VALUES
      (@task_id, @queue_id, 'running', @profile, @request_json, '', '', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP());
    """
    bq_query(sql, {
        "task_id": task_id,
        "queue_id": row["queue_id"],
        "profile": profile,
        "request_json": json.dumps(request_record, ensure_ascii=False, separators=(",", ":")),
    })


def mark_failed(queue_id: str, task_id: str, error: str) -> None:
    sql = f"""
    UPDATE `{PROJECT_ID}.{DATASET}.research_processing_queue`
    SET status='failed', last_error=@error, updated_at=CURRENT_TIMESTAMP()
    WHERE queue_id=@queue_id;
    UPDATE `{PROJECT_ID}.{DATASET}.manus_task_runs`
    SET status='failed', error=@error, updated_at=CURRENT_TIMESTAMP()
    WHERE manus_task_id=@task_id AND queue_id=@queue_id;
    """
    bq_query(sql, {"queue_id": queue_id, "task_id": task_id, "error": error[:1500]})


def mark_completed(row: dict[str, Any], task_id: str, profile: str, output: dict[str, Any], messages: dict[str, Any]) -> None:
    value = output.get("value") or {}
    confidence_value = float(value.get("confidence") or 0)
    if 1 < confidence_value <= 10:
        value["confidence"] = confidence_value / 10
    elif 10 < confidence_value <= 100:
        value["confidence"] = confidence_value / 100
    quality_errors = validate_output_quality(row["task_type"], value)
    if quality_errors:
        value["needs_review"] = True
        prior_reason = str(value.get("review_reason") or "").strip()
        gate_reason = "品質ゲート: " + " / ".join(quality_errors)
        value["review_reason"] = f"{prior_reason} / {gate_reason}" if prior_reason else gate_reason
    result_json = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    confidence = float(value.get("confidence") or 0)
    needs_review = bool(value.get("needs_review"))
    prompt_hash = row.get("prompt_hash") or ""
    sql = f"""
    MERGE `{PROJECT_ID}.{DATASET}.manus_classification_results` T
    USING (SELECT @entity_id entity_id, @task_type task_type, @taxonomy taxonomy_version) S
    ON T.entity_id=S.entity_id AND T.task_type=S.task_type AND T.taxonomy_version=S.taxonomy_version
    WHEN MATCHED THEN UPDATE SET
      manus_task_id=@task_id, agent_profile=@profile, result_json=@result_json,
      confidence=CAST(@confidence AS FLOAT64), needs_review=CAST(@needs_review AS BOOL),
      prompt_hash=@prompt_hash, updated_at=CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
      (result_id, entity_type, entity_id, task_type, taxonomy_version, manus_task_id,
       agent_profile, result_json, confidence, needs_review, prompt_hash, created_at, updated_at)
    VALUES
      (@result_id, 'video', @entity_id, @task_type, @taxonomy, @task_id,
       @profile, @result_json, CAST(@confidence AS FLOAT64), CAST(@needs_review AS BOOL),
       @prompt_hash, CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP());
    UPDATE `{PROJECT_ID}.{DATASET}.research_processing_queue`
    SET status=@queue_status, last_error=@quality_error, updated_at=CURRENT_TIMESTAMP()
    WHERE queue_id=@queue_id;
    UPDATE `{PROJECT_ID}.{DATASET}.manus_task_runs`
    SET status='completed', response_json=@response_json, error='', updated_at=CURRENT_TIMESTAMP()
    WHERE manus_task_id=@task_id AND queue_id=@queue_id;
    """
    bq_query(sql, {
        "result_id": str(uuid.uuid4()),
        "entity_id": row["video_id"],
        "task_type": row["task_type"],
        "taxonomy": row.get("taxonomy_version") or "jinsei_recipe_v1",
        "task_id": task_id,
        "profile": profile,
        "result_json": result_json,
        "confidence": str(confidence),
        "needs_review": "true" if needs_review else "false",
        "prompt_hash": prompt_hash,
        "queue_id": row["queue_id"],
        "response_json": json.dumps(messages, ensure_ascii=False, separators=(",", ":")),
        "queue_status": "needs_review" if quality_errors else "completed",
        "quality_error": " / ".join(quality_errors)[:1500],
    })


def validate_output_quality(task_type: str, value: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if float(value.get("confidence") or 0) <= 0:
        errors.append("confidenceが0")
    if float(value.get("confidence") or 0) > 1:
        errors.append("confidenceが1を超過")
    if not value.get("evidence"):
        errors.append("evidenceが空")
    if task_type == "classify_story":
        fingerprint = value.get("plot_fingerprint") or {}
        if not fingerprint.get("repeated_actions"):
            errors.append("repeated_actionsが空")
        if not value.get("similarity_risk_signature"):
            errors.append("similarity_risk_signatureが空")
        if not value.get("novelty_sources"):
            errors.append("novelty_sourcesが空")
        score_keys = ["hook_strength", "midpoint_strength", "ending_satisfaction", "exchangeability_risk"]
    else:
        if not value.get("strengths"):
            errors.append("strengthsが空")
        score_keys = ["mobile_legibility", "emotion_immediacy", "story_specificity", "generic_ai_image_risk"]
    if all(float(value.get(key) or 0) == 0 for key in score_keys):
        errors.append("全スコアが0")
    if any(not isinstance(value.get(key), int) for key in score_keys):
        errors.append("スコアが整数でない")
    return errors


def submit(task_type: str, limit: int, profile: str, dry_run: bool, anchor_task_id: str) -> list[dict[str, str]]:
    if task_type not in SCHEMAS:
        raise ValueError(f"Unsupported task type: {task_type}")
    schema = json.loads(SCHEMAS[task_type].read_text(encoding="utf-8"))
    rows = fetch_pending(task_type, limit)
    submitted: list[dict[str, str]] = []
    for row in rows:
        prompt_row = dict(row)
        transcript_attachment = ""
        if task_type == "classify_story" and row.get("transcript_text"):
            transcript_attachment = str(row["transcript_text"])
            prompt_row["transcript_text"] = (
                f"添付ファイル {row['video_id']}_transcript.txt を全文台本として読み、"
                "省略せずに因果骨格を分類してください。"
            )
        prompt = make_prompt(prompt_row)
        prompt += (
            "\n\n以下のJSON Schemaと完全に同じフィールド名・型で、コードフェンス以外の文章を付けず、"
            "単一のJSONオブジェクトだけを最終回答にしてください。\nOUTPUT_JSON_SCHEMA:\n"
            + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        )
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        row["prompt_hash"] = prompt_hash
        if dry_run:
            submitted.append({"queue_id": row["queue_id"], "video_id": row["video_id"], "status": "dry_run"})
            continue
        message_content: str | list[dict[str, str]] = prompt
        if transcript_attachment:
            encoded = base64.b64encode(transcript_attachment.encode("utf-8")).decode("ascii")
            message_content = [
                {"type": "text", "text": prompt},
                {
                    "type": "file",
                    "file_data": f"data:text/plain;base64,{encoded}",
                    "filename": f"{row['video_id']}_transcript.txt",
                    "mime_type": "text/plain",
                },
            ]
        payload = {
            "task_id": anchor_task_id,
            "message": {"content": message_content},
            "agent_profile": profile,
            "structured_output_schema": schema,
        }
        submitted_after_ms = int(time.time() * 1000)
        response = api_request("POST", "task.sendMessage", payload)
        task_id = find_value(response, "task_id")
        if not task_id:
            raise RuntimeError(f"task.sendMessage did not return task_id: {json.dumps(response, ensure_ascii=False)[:500]}")
        request_record = {
            "task_type": task_type,
            "video_id": row["video_id"],
            "profile": profile,
            "prompt_hash": prompt_hash,
            "prompt_chars": len(prompt),
            "attachment_chars": len(transcript_attachment),
            "schema_file": SCHEMAS[task_type].name,
            "submitted_after_ms": submitted_after_ms,
        }
        set_queue_running(row, str(task_id), profile, request_record)
        submitted.append({"queue_id": row["queue_id"], "video_id": row["video_id"], "task_id": str(task_id), "status": "running"})
    return submitted


def fetch_running(limit: int) -> list[dict[str, Any]]:
    sql = f"""
    SELECT q.queue_id, q.entity_id AS video_id, q.task_type, q.taxonomy_version,
           q.manus_task_id, r.agent_profile, r.request_json
    FROM `{PROJECT_ID}.{DATASET}.research_processing_queue` q
    JOIN `{PROJECT_ID}.{DATASET}.manus_task_runs` r
      ON r.manus_task_id=q.manus_task_id AND r.queue_id=q.queue_id
    WHERE q.status='running' AND r.status='running'
    QUALIFY ROW_NUMBER() OVER (PARTITION BY q.queue_id ORDER BY r.created_at DESC) = 1
    ORDER BY r.created_at
    LIMIT {int(limit)}
    """
    return bq_query(sql, json_output=True)


def poll_once(limit: int) -> list[dict[str, str]]:
    outcomes: list[dict[str, str]] = []
    for row in fetch_running(limit):
        task_id = row["manus_task_id"]
        query = urllib.parse.urlencode({"task_id": task_id, "limit": 200, "order": "desc", "verbose": "true"})
        messages = api_request("GET", f"task.listMessages?{query}")
        request_record = json.loads(row.get("request_json") or "{}")
        after_timestamp_ms = int(request_record.get("submitted_after_ms") or 0)
        schema = json.loads(SCHEMAS[row["task_type"]].read_text(encoding="utf-8"))
        structured = find_direct_output(messages, after_timestamp_ms, set(schema.get("required") or []))
        if structured is None:
            structured = find_structured_output(messages, after_timestamp_ms)
        if structured is None:
            outcomes.append({"task_id": task_id, "video_id": row["video_id"], "status": "running"})
            continue
        if not structured.get("success"):
            error = str(structured.get("error") or "Structured output extraction failed")
            mark_failed(row["queue_id"], task_id, error)
            outcomes.append({"task_id": task_id, "video_id": row["video_id"], "status": "failed"})
            continue
        row["prompt_hash"] = request_record.get("prompt_hash", "")
        mark_completed(row, task_id, row.get("agent_profile") or "", structured, messages)
        outcomes.append({"task_id": task_id, "video_id": row["video_id"], "status": "completed"})
    return outcomes


def run_and_wait(task_type: str, limit: int, profile: str, timeout_sec: int, poll_sec: int, anchor_task_id: str) -> dict[str, Any]:
    if limit != 1:
        raise ValueError("Anchor-task mode is sequential; use --limit 1 and wait for completion before the next submission.")
    submitted = submit(task_type, limit, profile, dry_run=False, anchor_task_id=anchor_task_id)
    deadline = time.time() + timeout_sec
    latest: list[dict[str, str]] = []
    while time.time() < deadline:
        latest = poll_once(max(limit * 2, 20))
        task_ids = {row.get("task_id") for row in submitted}
        relevant = [row for row in latest if row.get("task_id") in task_ids]
        if relevant and all(row["status"] in {"completed", "failed"} for row in relevant) and len(relevant) == len(submitted):
            return {"submitted": submitted, "outcomes": relevant}
        time.sleep(poll_sec)
    return {"submitted": submitted, "outcomes": latest, "timeout": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit_parser = subparsers.add_parser("submit")
    submit_parser.add_argument("--task-type", choices=sorted(SCHEMAS), required=True)
    submit_parser.add_argument("--limit", type=int, default=1)
    submit_parser.add_argument("--profile", choices=["manus-1.6", "manus-1.6-lite", "manus-1.6-max"], default="manus-1.6")
    submit_parser.add_argument("--dry-run", action="store_true")
    submit_parser.add_argument("--anchor-task-id", default=os.environ.get("MANUS_ANCHOR_TASK_ID", DEFAULT_ANCHOR_TASK_ID))

    poll_parser = subparsers.add_parser("poll")
    poll_parser.add_argument("--limit", type=int, default=20)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--task-type", choices=sorted(SCHEMAS), required=True)
    run_parser.add_argument("--limit", type=int, default=1)
    run_parser.add_argument("--profile", choices=["manus-1.6", "manus-1.6-lite", "manus-1.6-max"], default="manus-1.6")
    run_parser.add_argument("--timeout-sec", type=int, default=900)
    run_parser.add_argument("--poll-sec", type=int, default=15)
    run_parser.add_argument("--anchor-task-id", default=os.environ.get("MANUS_ANCHOR_TASK_ID", DEFAULT_ANCHOR_TASK_ID))

    args = parser.parse_args()
    if args.command == "submit":
        result = submit(args.task_type, args.limit, args.profile, args.dry_run, args.anchor_task_id)
    elif args.command == "poll":
        result = poll_once(args.limit)
    else:
        result = run_and_wait(args.task_type, args.limit, args.profile, args.timeout_sec, args.poll_sec, args.anchor_task_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
