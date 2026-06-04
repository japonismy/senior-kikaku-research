# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--update-youtube", action="store_true")
    ap.add_argument("--deploy", action="store_true")
    args = ap.parse_args()

    if args.update_youtube:
        run([sys.executable, "update_youtube_metadata.py", "--limit", str(args.limit)])
    run([sys.executable, "download_best_thumbnails.py", "--limit", str(args.limit), "--sleep", "0.05"])
    run(["uv", "run", "--with", "google-genai", "python", "ocr_thumbnail_text.py", "--engine", "gemini", "--limit", str(args.limit), "--sleep", "0.5"])
    run([sys.executable, "generate_portal_data.py"])
    if args.deploy:
        run([sys.executable, "deploy_pages.py"])
    return 0


def run(cmd: list[str]) -> None:
    print("RUN", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=HERE, text=True, encoding="utf-8", errors="replace")
    if proc.returncode:
        raise SystemExit(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
