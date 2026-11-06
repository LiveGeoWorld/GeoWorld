"""Clean an already-generated ``world.json`` of dirty place data.

Runs the same :mod:`world_module.map._internal.place_quality` checks used at
generation time over the *built* world, then cascade-removes every entity that
referenced a dropped place so the artifact stays internally consistent:

    place  ──►  service (service_<place_id>)
            └─► information_item (about.place_id / about.service_id)
                 └─► event_template (state_transitions / information_effects)
                      └─► information_graph (by_about / by_author / by_conflict_group)

A timestamped ``world.json.bak`` backup is written before any change, a
``clean_report.json`` records exactly what was dropped and why, and
``tool_index.json`` is rebuilt next to the world.

Usage::

    python -m world_module.map.clean_world --world outputs/mapseeds/<city>/<variant>/world.json
    python -m world_module.map.clean_world --world <path> --config config.yaml          # with geocode
    python -m world_module.map.clean_world --world <path> --no-geocode --dry-run         # report only
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.io import read_json, write_json
from shared.tool_index import build_world_tool_index
from world_module.map._internal import place_quality


def clean_world(
    world: dict[str, Any],
    *,
    reverse_admin_fn=None,
    name_dup_distance_m: float = place_quality.DEFAULT_NAME_DUP_DISTANCE_M,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(cleaned_world, report)``. Does not mutate the input world."""
    places = world.get("places", [])
    seed_meta = world.get("generation_metadata", {}).get("seed", {})
    bbox = seed_meta.get("bbox")
    center = seed_meta.get("center")

    candidates = [place_quality.candidate_from_world_place(p) for p in places]
    verdicts = place_quality.assess_places(
        candidates,
        bbox=bbox,
        city_center=center,
        reverse_admin_fn=reverse_admin_fn,
        name_dup_distance_m=name_dup_distance_m,
    )

    removed_place_ids: set[str] = set()
    kept_places: list[dict[str, Any]] = []
    for place in places:
        pid = place.get("place_id", "")
        verdict = verdicts.get(pid)
        if verdict is None or verdict.keep:
            if verdict and verdict.flags:
                prov = dict(place.get("provenance") or {})
                prov["quality_flags"] = sorted(set(prov.get("quality_flags", [])) | set(verdict.flags))
                place = {**place, "provenance": prov}
            kept_places.append(place)
        else:
            removed_place_ids.add(pid)

    # ── Cascade removal ──
    services = world.get("services", [])
    removed_service_ids = {
        s.get("service_id")
        for s in services
        if s.get("place_id") in removed_place_ids
    }
    kept_services = [s for s in services if s.get("service_id") not in removed_service_ids]

    info_items = world.get("information_items", [])
    kept_info: list[dict[str, Any]] = []
    removed_info_ids: set[str] = set()
    for info in info_items:
        about = info.get("about", {}) or {}
        if about.get("place_id") in removed_place_ids or about.get("service_id") in removed_service_ids:
            removed_info_ids.add(info.get("info_id"))
        else:
            kept_info.append(info)

    events = world.get("event_templates", [])
    kept_events: list[dict[str, Any]] = []
    removed_event_ids: set[str] = set()
    for event in events:
        targets = {t.get("target_id") for t in event.get("state_transitions", []) or []}
        effect_infos = {e.get("info_id") for e in event.get("information_effects", []) or [] if e.get("info_id")}
        if (targets & removed_service_ids) or (effect_infos & removed_info_ids):
            removed_event_ids.add(event.get("event_template_id"))
        else:
            kept_events.append(event)

    cleaned = dict(world)
    cleaned["places"] = kept_places
    cleaned["services"] = kept_services
    cleaned["information_items"] = kept_info
    cleaned["event_templates"] = kept_events
    cleaned["information_graph"] = _rebuild_information_graph(kept_info)

    place_report = place_quality.summarize_verdicts(verdicts)
    # Re-key dropped detail with the human-readable name for the report.
    name_by_id = {p.get("place_id"): p.get("name") for p in places}
    for entry in place_report.get("dropped_detail", []):
        entry["name"] = name_by_id.get(entry["key"], "")
    report = {
        "places": place_report,
        "cascade_removed": {
            "places": len(removed_place_ids),
            "services": len(removed_service_ids),
            "information_items": len(removed_info_ids),
            "event_templates": len(removed_event_ids),
        },
        "counts_after": {
            "places": len(kept_places),
            "services": len(kept_services),
            "information_items": len(kept_info),
            "event_templates": len(kept_events),
        },
    }
    return cleaned, report


def _rebuild_information_graph(info_items: list[dict[str, Any]]) -> dict[str, Any]:
    by_conflict_group: dict[str, list[str]] = {}
    by_author: dict[str, list[str]] = {}
    by_about: dict[str, list[str]] = {}
    for info in info_items:
        info_id = info.get("info_id")
        observability = info.get("observability", {}) or {}
        conflict_group = observability.get("conflict_group")
        if conflict_group:
            by_conflict_group.setdefault(conflict_group, []).append(info_id)
        author_id = (info.get("app_payload", {}) or {}).get("author_id")
        if author_id:
            by_author.setdefault(author_id, []).append(info_id)
        for target_id in (info.get("about", {}) or {}).values():
            if target_id:
                by_about.setdefault(target_id, []).append(info_id)
    return {"by_conflict_group": by_conflict_group, "by_author": by_author, "by_about": by_about}


def _detect_provider(world: dict[str, Any]) -> str:
    for place in world.get("places", []):
        provider = (place.get("map_projection") or {}).get("provider")
        if provider == "google":
            return "google"
        if provider == "osm":
            return "osm"
    return "unknown"


def _build_reverse_fn(world: dict[str, Any], config_path: str | None):
    """Build a reverse-geocode admin function from a Google client, or None."""
    if _detect_provider(world) != "google":
        print("[clean] world is not Google-sourced; skipping reverse-geocode check.", flush=True)
        return None
    try:
        from shared.config_loader import PipelineConfig
        from shared.maps_client import GoogleMapsClient

        config = PipelineConfig(config_path) if config_path else PipelineConfig()
        client = GoogleMapsClient(config=config)
        if not client._api_key:  # noqa: SLF001 - intentional capability probe
            print("[clean] no Google Maps API key; skipping reverse-geocode check.", flush=True)
            return None
        language = world.get("generation_metadata", {}).get("seed", {}).get("language", "zh-CN")
        return place_quality.reverse_admin_via_maps_client(client, language=language)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[clean] reverse-geocode unavailable ({exc}); skipping geo check.", flush=True)
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean dirty place data from a generated world.json.")
    parser.add_argument("--world", required=True, help="Path to world.json.")
    parser.add_argument("--config", default="config.yaml", help="config.yaml for Google reverse geocoding.")
    parser.add_argument("--no-geocode", action="store_true", help="Skip reverse-geocode consistency check.")
    parser.add_argument("--dry-run", action="store_true", help="Report only; do not write any files.")
    parser.add_argument(
        "--distance",
        type=float,
        default=place_quality.DEFAULT_NAME_DUP_DISTANCE_M,
        help="Same-name de-dup distance threshold in meters.",
    )
    args = parser.parse_args(argv)

    world_path = Path(args.world).resolve()
    if not world_path.exists():
        print(f"[clean] world not found: {world_path}", flush=True)
        return 1
    world = read_json(world_path)

    reverse_fn = None if args.no_geocode else _build_reverse_fn(world, args.config)

    cleaned, report = clean_world(world, reverse_admin_fn=reverse_fn, name_dup_distance_m=args.distance)

    _print_report(report)

    if args.dry_run:
        report_path = world_path.with_name("clean_report.json")
        write_json(report_path, report)
        print(f"[clean] DRY RUN — wrote report only: {report_path}", flush=True)
        return 0

    if report["cascade_removed"]["places"] == 0 and not report["places"]["kept_with_flags"]:
        print("[clean] no dirty places found; world unchanged.", flush=True)
        write_json(world_path.with_name("clean_report.json"), report)
        return 0

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = world_path.with_name(f"world.json.{timestamp}.bak")
    shutil.copy2(world_path, backup_path)
    write_json(world_path, cleaned)
    write_json(world_path.with_name("clean_report.json"), report)

    tool_index_path = world_path.with_name("tool_index.json")
    if tool_index_path.exists():
        write_json(tool_index_path, build_world_tool_index(cleaned))

    print(f"[clean] backup:  {backup_path}", flush=True)
    print(f"[clean] cleaned: {world_path}", flush=True)
    print(f"[clean] report:  {world_path.with_name('clean_report.json')}", flush=True)
    return 0


def _print_report(report: dict[str, Any]) -> None:
    places = report["places"]
    print("=" * 60, flush=True)
    print(f"[clean] places: kept {places['kept']}/{places['total']} (dropped {places['dropped']})", flush=True)
    print(f"[clean] flag counts: {places['flag_counts']}", flush=True)
    cascade = report["cascade_removed"]
    print(
        f"[clean] cascade removed → services {cascade['services']}, "
        f"info {cascade['information_items']}, events {cascade['event_templates']}",
        flush=True,
    )
    for entry in places["dropped_detail"][:50]:
        print(f"  - DROP {entry.get('name','')} [{entry['key']}] {entry['flags']}", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
