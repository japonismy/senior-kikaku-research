# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, cwd=HERE, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if proc.returncode:
        raise SystemExit((proc.stderr or proc.stdout).strip())
    out = (proc.stdout or "").strip()
    if out:
        print(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-bq", action="store_true")
    args = ap.parse_args()

    generator = "generate_portal_data_from_bq.py" if args.from_bq else "generate_portal_data.py"
    run([sys.executable, generator])
    run([
        "git",
        "add",
        "index.html",
        "README.md",
        "generate_portal_data.py",
        "generate_portal_data_from_bq.py",
        "thumbnail_text_overrides.csv",
        "thumbnail_analysis_overrides.jsonl",
        "data",
        "reports",
        ".gitignore",
        "download_best_thumbnails.py",
        "ocr_thumbnail_text.py",
        "update_youtube_metadata.py",
        "run_maintenance_batch.py",
        "deploy_pages.py",
    ])
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=HERE,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    ).stdout.strip()
    if not status:
        print("No changes to deploy.")
        return 0
    run(["git", "commit", "-m", "Update senior research portal data"])
    run(["git", "push"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
