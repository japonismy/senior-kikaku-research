# -*- coding: utf-8 -*-
"""Analyze disclosed related-video source edges against calibrated performance."""
from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from google.cloud import bigquery


PROJECT_ID = "rugged-destiny-408613"
DATASET = "senior_reading_all"
VAULT = Path(r"E:\Data\ObsidianVault")
OUTPUT_DIR = (
    VAULT / "02_Channels" / "シニア朗読" / "analysis" / "20260813_一次調査"
    / "manus_calibration_v1" / "related_video_20260814"
)


def number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def median(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [number(row[key]) for row in rows if row.get(key) not in (None, "")]
    return round(statistics.median(values), 4) if values else None


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def load_data(client: bigquery.Client) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    performance = [dict(row) for row in client.query(f"""
      SELECT * FROM `{PROJECT_ID}.{DATASET}.manus_title_thumbnail_performance_v1`
      ORDER BY calibration_id
    """).result()]
    edges = [dict(row) for row in client.query(f"""
      SELECT * FROM `{PROJECT_ID}.{DATASET}.youtube_related_video_edges_current_v1`
      ORDER BY target_video_id, related_views DESC
    """).result()]
    for rows in (performance, edges):
        for row in rows:
            for key, value in list(row.items()):
                if isinstance(value, (date, datetime)):
                    row[key] = value.isoformat()
    return performance, edges


def build_outputs(
    performance: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    lookup = {row["video_id"]: row for row in performance}
    by_target: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        target = lookup[edge["target_video_id"]]
        source = lookup.get(edge["source_video_id"])
        edge["target_primary_structure"] = target.get("primary_structure")
        edge["target_title_promise"] = target.get("title_promise_type")
        edge["target_alignment_type"] = target.get("alignment_type")
        edge["source_is_calibration"] = source is not None
        edge["source_primary_structure"] = source.get("primary_structure") if source else None
        edge["source_title_promise"] = source.get("title_promise_type") if source else None
        by_target[edge["target_video_id"]].append(edge)

    targets = []
    for video_id, performance_row in lookup.items():
        members = by_target.get(video_id, [])
        if not members:
            continue
        disclosed_self = sum(int(row["related_views"]) for row in members if row.get("source_is_self"))
        disclosed_external = sum(int(row["related_views"]) for row in members if not row.get("source_is_self"))
        disclosed_total = disclosed_self + disclosed_external
        self_share = disclosed_self / disclosed_total if disclosed_total else 0
        if self_share >= 0.8:
            network_mode = "internal_cluster"
        elif self_share <= 0.5:
            network_mode = "external_bridge"
        else:
            network_mode = "mixed"
        top = max(members, key=lambda item: int(item["related_views"]))
        related_total = int(members[0]["target_related_views"])
        views = int(performance_row["views"])
        targets.append({
            "calibration_id": performance_row["calibration_id"],
            "video_id": video_id,
            "title": performance_row["title_text"],
            "primary_structure": performance_row["primary_structure"],
            "title_promise_type": performance_row["title_promise_type"],
            "thumbnail_visual_promise": performance_row["visual_promise"],
            "alignment_type": performance_row["alignment_type"],
            "views": views,
            "views_per_day": performance_row["views_per_day"],
            "average_view_percentage": performance_row["averageViewPercentage"],
            "reach_ctr": performance_row["reach_ctr"],
            "related_views": related_total,
            "related_view_share": round(related_total / views, 6) if views else None,
            "disclosed_detail_views": disclosed_total,
            "disclosed_detail_coverage": members[0]["detail_coverage"],
            "disclosed_source_count": len(members),
            "disclosed_self_views": disclosed_self,
            "disclosed_external_views": disclosed_external,
            "disclosed_self_share": round(self_share, 6) if disclosed_total else None,
            "disclosed_network_mode": network_mode,
            "top_source_video_id": top["source_video_id"],
            "top_source_title": top["source_title"],
            "top_source_channel": top["source_channel_title"],
            "top_source_is_self": top["source_is_self"],
            "top_source_related_views": top["related_views"],
        })

    source_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    external_channel_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    transition_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        source_groups[edge["source_video_id"]].append(edge)
        if not edge.get("source_is_self"):
            external_channel_groups[str(edge.get("source_channel_id") or "unknown")].append(edge)
        if edge.get("source_primary_structure"):
            transition_groups[(edge["source_primary_structure"], edge["target_primary_structure"])].append(edge)

    sources = []
    for source_id, members in source_groups.items():
        first = members[0]
        sources.append({
            "source_video_id": source_id,
            "source_title": first.get("source_title"),
            "source_channel_id": first.get("source_channel_id"),
            "source_channel_title": first.get("source_channel_title"),
            "source_is_self": first.get("source_is_self"),
            "source_is_calibration": first.get("source_is_calibration"),
            "source_primary_structure": first.get("source_primary_structure"),
            "source_title_promise": first.get("source_title_promise"),
            "disclosed_edge_views": sum(int(row["related_views"]) for row in members),
            "target_count": len({row["target_video_id"] for row in members}),
        })
    sources.sort(key=lambda row: row["disclosed_edge_views"], reverse=True)

    external_channels = []
    for channel_id, members in external_channel_groups.items():
        first = members[0]
        external_channels.append({
            "source_channel_id": channel_id,
            "source_channel_title": first.get("source_channel_title"),
            "disclosed_edge_views": sum(int(row["related_views"]) for row in members),
            "target_count": len({row["target_video_id"] for row in members}),
            "source_video_count": len({row["source_video_id"] for row in members}),
        })
    external_channels.sort(key=lambda row: row["disclosed_edge_views"], reverse=True)

    transitions = []
    for (source_structure, target_structure), members in transition_groups.items():
        transitions.append({
            "source_primary_structure": source_structure,
            "target_primary_structure": target_structure,
            "disclosed_edge_views": sum(int(row["related_views"]) for row in members),
            "edge_count": len(members),
            "source_count": len({row["source_video_id"] for row in members}),
            "target_count": len({row["target_video_id"] for row in members}),
        })
    transitions.sort(key=lambda row: row["disclosed_edge_views"], reverse=True)
    return targets, sources, external_channels, transitions, edges


def save_table(client: bigquery.Client, table_name: str, rows: list[dict[str, Any]]) -> str:
    table_id = f"{PROJECT_ID}.{DATASET}.{table_name}"
    client.load_table_from_json(
        rows, table_id,
        job_config=bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE", autodetect=True),
    ).result()
    return table_id


def main() -> int:
    client = bigquery.Client(project=PROJECT_ID)
    performance, edges = load_data(client)
    targets, sources, external_channels, transitions, enriched_edges = build_outputs(performance, edges)
    if len(targets) != 25:
        raise ValueError(f"expected 25 target summaries, got {len(targets)}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    files = {
        "targets": OUTPUT_DIR / "calibration_25_related_video_target_summary_20260814.csv",
        "sources": OUTPUT_DIR / "calibration_25_related_video_source_centrality_20260814.csv",
        "external_channels": OUTPUT_DIR / "calibration_25_related_video_external_channels_20260814.csv",
        "transitions": OUTPUT_DIR / "calibration_25_related_video_structure_transitions_20260814.csv",
        "enriched_edges": OUTPUT_DIR / "calibration_25_related_video_edges_enriched_20260814.csv",
    }
    for key, rows in (
        ("targets", targets), ("sources", sources), ("external_channels", external_channels),
        ("transitions", transitions), ("enriched_edges", enriched_edges),
    ):
        write_csv(files[key], rows)

    table_ids = {
        "targets": save_table(client, "youtube_related_target_summary_v1", targets),
        "sources": save_table(client, "youtube_related_source_centrality_v1", sources),
        "external_channels": save_table(client, "youtube_related_external_channels_v1", external_channels),
        "transitions": save_table(client, "youtube_related_structure_transitions_v1", transitions),
        "enriched_edges": save_table(client, "youtube_related_video_edges_enriched_v1", enriched_edges),
    }
    structures = {}
    for structure in ("P03B", "P06B"):
        members = [row for row in targets if row["primary_structure"] == structure]
        structures[structure] = {
            "n": len(members),
            "median_views": median(members, "views"),
            "median_related_views": median(members, "related_views"),
            "median_related_view_share": median(members, "related_view_share"),
            "median_disclosed_detail_coverage": median(members, "disclosed_detail_coverage"),
            "network_modes": {
                mode: sum(row["disclosed_network_mode"] == mode for row in members)
                for mode in ("internal_cluster", "mixed", "external_bridge")
            },
        }
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_count": len(targets),
        "edge_count": len(edges),
        "source_count": len(sources),
        "external_channel_count": len(external_channels),
        "structures": structures,
        "top_sources": sources[:20],
        "top_external_channels": external_channels[:20],
        "structure_transitions": transitions,
        "files": {key: str(path) for key, path in files.items()},
        "bigquery": table_ids,
        "disclosure_caveat": "Source detail contains only API-disclosed top sources; use target related_views for totals.",
    }
    summary_path = OUTPUT_DIR / "calibration_25_related_video_network_summary_20260814.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "targets": len(targets), "edges": len(edges), "sources": len(sources),
        "external_channels": len(external_channels), "structures": structures,
        "summary": str(summary_path), "bigquery": table_ids,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
