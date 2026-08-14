# -*- coding: utf-8 -*-
"""Fill research transcript gaps without spending Manus agent time."""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import bigquery
from youtube_transcript_api import YouTubeTranscriptApi

from manus_pipeline import DATASET, PROJECT_ID


VAULT = Path(r"E:\Data\ObsidianVault")
DEFAULT_ARCHIVE = VAULT / "02_Channels" / "シニア朗読" / "analysis" / "transcript_archive"


def fetch_targets(client: bigquery.Client, limit: int, self_only: bool) -> list[dict[str, Any]]:
    self_filter = "AND c.is_self" if self_only else ""
    sql = f"""
    SELECT q.queue_id, q.entity_id AS video_id, q.channel_id, c.channel_name, c.title,
           c.is_self, q.priority
    FROM `{PROJECT_ID}.{DATASET}.research_processing_queue` q
    JOIN `{PROJECT_ID}.{DATASET}.research_data_coverage` c ON c.video_id=q.entity_id
    WHERE q.task_type='fetch_transcript' AND q.status IN ('pending', 'failed')
      {self_filter}
    ORDER BY q.priority DESC, c.view_count DESC, q.created_at
    LIMIT {int(limit)}
    """
    return [dict(row) for row in client.query(sql).result()]


def transcript_payload(video_id: str) -> tuple[str, list[dict[str, Any]]]:
    fetched = YouTubeTranscriptApi().fetch(video_id, languages=["ja", "en"])
    raw = fetched.to_raw_data()
    text = "\n".join(str(item.get("text") or "").strip() for item in raw if str(item.get("text") or "").strip())
    if not text:
        raise RuntimeError("取得字幕が空です")
    return text, raw


def merge_transcripts(client: bigquery.Client, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    temporary = f"{PROJECT_ID}.{DATASET}._tmp_fetched_transcripts_{uuid.uuid4().hex}"
    schema = [
        bigquery.SchemaField("video_id", "STRING"),
        bigquery.SchemaField("transcript_text", "STRING"),
        bigquery.SchemaField("transcript_json", "STRING"),
        bigquery.SchemaField("language", "STRING"),
        bigquery.SchemaField("source", "STRING"),
        bigquery.SchemaField("fetched_at", "STRING"),
    ]
    client.load_table_from_json(rows, temporary, job_config=bigquery.LoadJobConfig(schema=schema)).result()
    try:
        client.query(
            f"""
            MERGE `{PROJECT_ID}.{DATASET}.analysis_competitor_db__transcripts` T
            USING `{temporary}` S ON T.video_id=S.video_id
            WHEN MATCHED THEN UPDATE SET
              transcript_text=S.transcript_text, transcript_json=S.transcript_json,
              language=S.language, source=S.source, fetched_at=S.fetched_at
            WHEN NOT MATCHED THEN INSERT
              (video_id, transcript_text, transcript_json, language, source, fetched_at)
            VALUES
              (S.video_id, S.transcript_text, S.transcript_json, S.language, S.source, S.fetched_at)
            """
        ).result()
    finally:
        client.delete_table(temporary, not_found_ok=True)


def mark_failures(client: bigquery.Client, failures: list[dict[str, str]]) -> None:
    for failure in failures:
        config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("queue_id", "STRING", failure["queue_id"]),
                bigquery.ScalarQueryParameter("error", "STRING", failure["error"][:1500]),
            ]
        )
        client.query(
            f"""
            UPDATE `{PROJECT_ID}.{DATASET}.research_processing_queue`
            SET status='failed', attempt_count=attempt_count+1, last_error=@error,
                updated_at=CURRENT_TIMESTAMP()
            WHERE queue_id=@queue_id
            """,
            job_config=config,
        ).result()


def refresh_coverage_and_queue(client: bigquery.Client) -> None:
    import sys

    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from cloud_batch.daily_youtube_metadata_update import refresh_research_coverage_and_queue

    refresh_research_coverage_and_queue(client)


def archive_transcript(root: Path, target: dict[str, Any], text: str, raw: list[dict[str, Any]]) -> None:
    day = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d")
    directory = root / day
    directory.mkdir(parents=True, exist_ok=True)
    video_id = target["video_id"]
    (directory / f"{video_id}.txt").write_text(text, encoding="utf-8")
    (directory / f"{video_id}.json").write_text(
        json.dumps(
            {
                "video_id": video_id,
                "channel_name": target.get("channel_name") or "",
                "title": target.get("title") or "",
                "source": "youtube_transcript_api",
                "segments": raw,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--self-only", action="store_true")
    parser.add_argument("--archive-root", default=str(DEFAULT_ARCHIVE))
    args = parser.parse_args()

    client = bigquery.Client(project=PROJECT_ID)
    targets = fetch_targets(client, args.limit, args.self_only)
    fetched_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for target in targets:
        try:
            text, raw = transcript_payload(target["video_id"])
            rows.append(
                {
                    "video_id": target["video_id"],
                    "transcript_text": text,
                    "transcript_json": json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
                    "language": "ja",
                    "source": "youtube_transcript_api",
                    "fetched_at": fetched_at,
                }
            )
            archive_transcript(Path(args.archive_root), target, text, raw)
        except Exception as exc:
            failures.append(
                {
                    "queue_id": target["queue_id"],
                    "video_id": target["video_id"],
                    "error": f"{type(exc).__name__}: {exc}"[:1500],
                }
            )
    merge_transcripts(client, rows)
    mark_failures(client, failures)
    if rows:
        refresh_coverage_and_queue(client)
    print(
        json.dumps(
            {
                "targets": len(targets),
                "completed": len(rows),
                "failed": len(failures),
                "completed_video_ids": [row["video_id"] for row in rows],
                "failures": failures,
                "archive_root": str(Path(args.archive_root)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
