"""Create a read-only, reproducible inventory for a managed GeoWorld world.

The report deliberately reads both the registry and referenced artifacts.  A
registry entry alone is not sufficient evidence that a version is usable for
task generation or evaluation.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def safe_read(path: Path) -> tuple[Any | None, str | None]:
    try:
        return read_json(path), None
    except Exception as exc:  # Audit malformed historical files instead of hiding them.
        return None, f"{type(exc).__name__}: {exc}"


def path_from_root(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def world_counts(world: Any) -> dict[str, int]:
    if not isinstance(world, dict):
        return {}
    return {
        key: len(world.get(key) or [])
        for key in ("districts", "corridors", "places", "services", "information_items", "event_templates")
    }


def character_counts(characters: Any) -> dict[str, int]:
    if not isinstance(characters, dict):
        return {}
    return {
        "characters": len(characters.get("characters") or []),
        "relationships": len(characters.get("relationships") or []),
    }


def version_summary(city: dict[str, Any]) -> dict[str, Any]:
    artifacts = city.get("artifacts") or {}
    map_path = path_from_root((artifacts.get("map_world") or {}).get("path"))
    char_path = path_from_root((artifacts.get("characters") or {}).get("path"))
    map_data, map_error = safe_read(map_path) if map_path else (None, "missing registry map path")
    char_data, char_error = safe_read(char_path) if char_path else (None, "missing registry character path")
    return {
        "city_id": city.get("city_id"),
        "city_name": city.get("city_name"),
        "lifecycle_status": city.get("lifecycle_status"),
        "active_version": city.get("active_version") or (city.get("world_binding") or {}).get("city_version"),
        "readiness": city.get("readiness"),
        "missing_capabilities": city.get("missing_capabilities") or [],
        "registry_versions": [
            {
                "version": item.get("version"),
                "version_id": item.get("version_id"),
                "kind": item.get("kind"),
                "parent_version": item.get("parent_version"),
                "counts": item.get("counts") or {},
                "requested_targets": (item.get("registration_metadata") or {}).get("requested_targets") or {},
            }
            for item in (city.get("city_versions") or [])
            if isinstance(item, dict)
        ],
        "map_path": str(map_path.relative_to(ROOT)) if map_path and map_path.is_relative_to(ROOT) else str(map_path or ""),
        "map_exists": bool(map_path and map_path.is_file()),
        "map_counts": world_counts(map_data),
        "map_read_error": map_error,
        "characters_path": str(char_path.relative_to(ROOT)) if char_path and char_path.is_relative_to(ROOT) else str(char_path or ""),
        "characters_exists": bool(char_path and char_path.is_file()),
        "character_counts": character_counts(char_data),
        "characters_read_error": char_error,
        "route_cache_status": (artifacts.get("route_cache") or {}).get("status"),
        "cultural_constraints_status": (artifacts.get("cultural_constraints") or {}).get("status"),
        "cultural_evidence_status": (artifacts.get("cultural_evidence") or {}).get("status"),
        "construction_attempt_count": len(city.get("construction_attempts") or []),
    }


def task_batches(world_id: str) -> list[dict[str, Any]]:
    root = ROOT / "outputs" / "benchmarks"
    result: list[dict[str, Any]] = []
    if not root.is_dir():
        return result
    for provenance in sorted(root.glob("*/*/*/world_provenance.json")):
        data, error = safe_read(provenance)
        if not isinstance(data, dict) or data.get("world_id") != world_id:
            continue
        batch_dir = provenance.parent
        tasks_dir = batch_dir / "tasks"
        task_dirs = [path for path in tasks_dir.iterdir() if path.is_dir()] if tasks_dir.is_dir() else []
        review_path = batch_dir / "review_status.json"
        review, review_error = safe_read(review_path) if review_path.is_file() else ({}, None)
        statuses: dict[str, int] = {}
        if isinstance(review, dict):
            for record in review.values():
                if isinstance(record, dict):
                    status = str(record.get("status", "missing"))
                    statuses[status] = statuses.get(status, 0) + 1
        result.append({
            "task_type": data.get("task_type") or batch_dir.parents[2].name,
            "city_id": data.get("city_id") or batch_dir.parent.name,
            "batch": batch_dir.name,
            "path": str(batch_dir.relative_to(ROOT)),
            "task_count": len(task_dirs),
            "required_city_versions": data.get("required_city_versions") or {},
            "review_status_counts": statuses,
            "review_read_error": review_error,
            "review_state": "not_started" if not review_path.is_file() else "recorded",
        })
    return result


def evaluation_runs(world_id: str) -> list[dict[str, Any]]:
    root = ROOT / "outputs" / "benchmarks"
    result: list[dict[str, Any]] = []
    for summary_path in sorted(root.glob("*/*/*/runs/*/results.json")):
        payload, error = safe_read(summary_path)
        if not isinstance(payload, dict):
            continue
        # Runs predate explicit world binding in some cases.  Preserve them as
        # historical evidence but never attribute them to a new validation world.
        if payload.get("world_id") not in (world_id, None, ""):
            continue
        result.append({
            "path": str(summary_path.relative_to(ROOT)),
            "explicit_world_id": payload.get("world_id"),
            "task_count": payload.get("task_count"),
            "passed": payload.get("passed"),
            "model_key": payload.get("model_key"),
            "model_name": payload.get("model_name"),
            "started_at": payload.get("started_at"),
            "finished_at": payload.get("finished_at"),
            "read_error": error,
        })
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--world-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    world_root = ROOT / "outputs" / "worlds" / args.world_id
    manifest_path = world_root / "world_manifest.json"
    manifest, manifest_error = safe_read(manifest_path)
    if not isinstance(manifest, dict):
        raise SystemExit(f"Cannot read world manifest {manifest_path}: {manifest_error}")

    cities: list[dict[str, Any]] = []
    for path in sorted((world_root / "cities").glob("*.json")):
        city, error = safe_read(path)
        if not isinstance(city, dict):
            cities.append({"registry_path": str(path.relative_to(ROOT)), "read_error": error})
            continue
        summary = version_summary(city)
        summary["registry_path"] = str(path.relative_to(ROOT))
        cities.append(summary)

    report = {
        "schema_version": "geoword.validation.preflight.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "world_id": args.world_id,
        "world_manifest": {
            "display_name": manifest.get("display_name"),
            "lifecycle_status": manifest.get("lifecycle_status"),
            "city_count": manifest.get("city_count"),
            "city_ids": manifest.get("city_ids") or [],
            "registry_history_count": len(manifest.get("history") or []),
        },
        "city_versions": cities,
        "task_batches": task_batches(args.world_id),
        "evaluation_runs": evaluation_runs(args.world_id),
    }
    output = path_from_root(args.output)
    if output is None:
        raise SystemExit("An output path is required")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output)
    print(json.dumps({
        "cities": len(cities),
        "task_batches": len(report["task_batches"]),
        "evaluation_runs": len(report["evaluation_runs"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
