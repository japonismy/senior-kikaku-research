# -*- coding: utf-8 -*-
from __future__ import annotations

import subprocess
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
    run(["python", "generate_portal_data.py"])
    run(["git", "add", "index.html", "README.md", "generate_portal_data.py", "thumbnail_text_overrides.csv", "thumbnail_analysis_overrides.jsonl", "data", "reports", ".gitignore", "download_best_thumbnails.py", "ocr_thumbnail_text.py", "update_youtube_metadata.py", "run_maintenance_batch.py", "deploy_pages.py"])
    status = subprocess.run(["git", "status", "--short"], cwd=HERE, text=True, encoding="utf-8", errors="replace", capture_output=True).stdout.strip()
    if not status:
        print("No changes to deploy.")
        return 0
    run(["git", "commit", "-m", "Update企画リサーチ data"])
    run(["git", "push"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
