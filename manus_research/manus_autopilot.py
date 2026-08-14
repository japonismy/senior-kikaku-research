# -*- coding: utf-8 -*-
"""Keep the single Free-plan Manus execution slot continuously occupied.

The worker intentionally runs one task at a time. It first recovers any task
already marked running in BigQuery, then chooses the highest-priority pending
story/thumbnail classification across task types.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import manus_pipeline as pipeline


ROOT = Path(__file__).resolve().parent
DEFAULT_LOG = ROOT.parent / "logs" / "manus_autopilot.jsonl"
DEFAULT_STATUS = ROOT.parent / "logs" / "manus_autopilot_status.json"
DEFAULT_LOCK = ROOT.parent / "logs" / "manus_autopilot.lock"
DEFAULT_STOP = ROOT / "STOP_MANUS_AUTOPILOT"
TASK_TYPE_ORDER = ("classify_story", "classify_thumbnail")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_event(path: Path, event: str, **fields: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": utc_now(), "event": event, **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def write_status(path: Path, state: str, **fields: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"updated_at": utc_now(), "state": state, **fields}, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Hold a one-byte OS lock so scheduled/manual workers cannot overlap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise RuntimeError(f"別のManus自動ワーカーが稼働中です: {path}") from exc
    try:
        yield
    finally:
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def choose_next_task_type() -> tuple[str, dict[str, Any]] | None:
    candidates: list[tuple[int, int, str, dict[str, Any]]] = []
    for order, task_type in enumerate(TASK_TYPE_ORDER):
        rows = pipeline.fetch_pending(task_type, 1)
        if rows:
            row = rows[0]
            candidates.append((-int(row.get("priority") or 0), order, task_type, row))
    if not candidates:
        return None
    _, _, task_type, row = min(candidates)
    return task_type, row


def wait_for_running(
    *, poll_sec: int, log_path: Path, status_path: Path, stop_path: Path
) -> int:
    """Recover and collect an already-submitted task before creating another."""
    terminal = 0
    while pipeline.fetch_running(20):
        if stop_path.exists():
            write_status(status_path, "stopping_after_running_task")
        try:
            outcomes = pipeline.poll_once(20)
            append_event(log_path, "poll", outcomes=outcomes)
            terminal += sum(row.get("status") in {"completed", "failed"} for row in outcomes)
        except Exception as exc:  # keep the queue recoverable after transient API errors
            append_event(log_path, "poll_error", error=f"{type(exc).__name__}: {exc}"[:1500])
            write_status(status_path, "poll_error", error=str(exc)[:1500])
        if pipeline.fetch_running(20):
            time.sleep(poll_sec)
    return terminal


def run_worker(args: argparse.Namespace) -> dict[str, Any]:
    log_path = Path(args.log_path)
    status_path = Path(args.status_path)
    stop_path = Path(args.stop_path)
    terminal_count = 0
    error_streak = 0
    started_at = utc_now()
    append_event(log_path, "worker_started", profile=args.profile, max_terminal=args.max_terminal)
    write_status(status_path, "starting", pid=os.getpid(), started_at=started_at)

    while True:
        terminal_count += wait_for_running(
            poll_sec=args.poll_sec, log_path=log_path, status_path=status_path, stop_path=stop_path
        )
        if stop_path.exists():
            append_event(log_path, "worker_stopped", reason="stop_file", terminal_count=terminal_count)
            write_status(status_path, "stopped", reason="stop_file", terminal_count=terminal_count)
            break
        if args.max_terminal and terminal_count >= args.max_terminal:
            append_event(log_path, "worker_stopped", reason="max_terminal", terminal_count=terminal_count)
            write_status(status_path, "stopped", reason="max_terminal", terminal_count=terminal_count)
            break

        candidate = choose_next_task_type()
        if candidate is None:
            append_event(log_path, "idle", reason="no_pending_supported_tasks")
            write_status(status_path, "idle", reason="no_pending_supported_tasks", terminal_count=terminal_count)
            if args.exit_when_empty:
                break
            time.sleep(args.idle_sec)
            continue

        task_type, row = candidate
        write_status(
            status_path,
            "submitting",
            task_type=task_type,
            video_id=row.get("video_id"),
            queue_id=row.get("queue_id"),
            priority=row.get("priority"),
            terminal_count=terminal_count,
        )
        try:
            result = pipeline.run_and_wait(
                task_type,
                1,
                args.profile,
                args.timeout_sec,
                args.poll_sec,
                args.anchor_task_id,
            )
            append_event(log_path, "task_cycle", task_type=task_type, video_id=row.get("video_id"), result=result)
            outcomes = result.get("outcomes") or []
            terminal_count += sum(item.get("status") in {"completed", "failed"} for item in outcomes)
            error_streak = 0
            write_status(
                status_path,
                "cycle_finished" if not result.get("timeout") else "awaiting_recovery",
                task_type=task_type,
                video_id=row.get("video_id"),
                result=result,
                terminal_count=terminal_count,
            )
        except Exception as exc:
            error_streak += 1
            delay = min(args.error_max_sec, args.error_base_sec * (2 ** min(error_streak - 1, 5)))
            error = f"{type(exc).__name__}: {exc}"[:1500]
            append_event(log_path, "cycle_error", task_type=task_type, video_id=row.get("video_id"), error=error, retry_sec=delay)
            write_status(
                status_path,
                "backoff",
                task_type=task_type,
                video_id=row.get("video_id"),
                error=error,
                retry_sec=delay,
                terminal_count=terminal_count,
            )
            time.sleep(delay)

    return {"started_at": started_at, "ended_at": utc_now(), "terminal_count": terminal_count}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["manus-1.6", "manus-1.6-lite"], default="manus-1.6")
    parser.add_argument("--anchor-task-id", default=os.environ.get("MANUS_ANCHOR_TASK_ID", pipeline.DEFAULT_ANCHOR_TASK_ID))
    parser.add_argument("--poll-sec", type=int, default=15)
    parser.add_argument("--timeout-sec", type=int, default=1200)
    parser.add_argument("--idle-sec", type=int, default=300)
    parser.add_argument("--error-base-sec", type=int, default=60)
    parser.add_argument("--error-max-sec", type=int, default=1800)
    parser.add_argument("--max-terminal", type=int, default=0, help="0 means run until stopped or the queue is empty")
    parser.add_argument("--exit-when-empty", action="store_true")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG))
    parser.add_argument("--status-path", default=str(DEFAULT_STATUS))
    parser.add_argument("--lock-path", default=str(DEFAULT_LOCK))
    parser.add_argument("--stop-path", default=str(DEFAULT_STOP))
    args = parser.parse_args()

    with exclusive_lock(Path(args.lock_path)):
        result = run_worker(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
