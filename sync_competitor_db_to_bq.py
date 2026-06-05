# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd


PROJECT = "rugged-destiny-408613"
DATASET = "senior_reading_all"
GCS_BUCKET = "gs://senior-share-staging-570862915709"
GCS_PREFIX = "senior_reading_all"
ALIAS = "analysis_competitor_db"
HERE = Path(__file__).resolve().parent
DB_PATH = HERE / ".." / ".." / "analysis" / "competitor_db.sqlite"
EXPORT_DIR = HERE / "bq_sync_export"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", nargs="*", default=[])
    args = ap.parse_args()

    os.environ.setdefault("CLOUDSDK_CORE_ACCOUNT", "japonismy@gmail.com")
    EXPORT_DIR.mkdir(exist_ok=True)
    tables = args.tables or list_sqlite_tables()
    exported = []
    for table in tables:
        bq_table = f"{ALIAS}__{sanitize(table)}"
        parquet = EXPORT_DIR / f"{bq_table}.parquet"
        export_table(table, parquet)
        exported.append((bq_table, parquet))

    for bq_table, parquet in exported:
        gcs_uri = f"{GCS_BUCKET}/{GCS_PREFIX}/{parquet.name}"
        run(["gcloud", "storage", "cp", str(parquet), gcs_uri])
        run([
            "bq",
            "--project_id",
            PROJECT,
            "load",
            "--replace",
            "--source_format=PARQUET",
            f"{PROJECT}:{DATASET}.{bq_table}",
            gcs_uri,
        ])

    print(json.dumps({"synced_tables": [t for t, _ in exported]}, ensure_ascii=False))
    return 0


def list_sqlite_tables() -> list[str]:
    con = sqlite3.connect(str(DB_PATH))
    try:
        rows = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def export_table(table: str, parquet: Path) -> None:
    con = sqlite3.connect(str(DB_PATH))
    try:
        df = pd.read_sql(f'SELECT * FROM "{table}"', con)
    finally:
        con.close()
    for col in df.columns:
        if df[col].dtype == object:
            types = {type(x).__name__ for x in df[col].dropna()}
            if len(types) > 1:
                df[col] = df[col].apply(lambda x: None if x is None else str(x))
    df.to_parquet(parquet, index=False)
    print(f"exported {table} -> {parquet.name} rows={len(df)} cols={len(df.columns)}")


def sanitize(value: str) -> str:
    s = re.sub(r"[^0-9A-Za-z_]+", "_", value).strip("_").lower()
    if not s or not re.match(r"[A-Za-z_]", s[0]):
        s = "db_" + s
    return s[:64]


def run(cmd: list[str]) -> None:
    print("RUN", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=HERE, text=True, encoding="utf-8", errors="replace", capture_output=True, shell=(sys.platform == "win32"))
    if proc.returncode:
        raise SystemExit((proc.stderr or proc.stdout).strip())
    out = (proc.stdout or "").strip()
    if out:
        print(out[-1000:])


if __name__ == "__main__":
    raise SystemExit(main())
