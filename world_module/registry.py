"""Non-destructive registry for a multi-city GeoWorld world."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from shared.io import read_json, write_json
from world_module.map._internal.factual_service_index import (
    build_factual_service_index_artifact,
    factual_service_place_signature,
    materialize_factual_services,
)
from world_module.cultural_assets import ensure_cultural_assets, materialize_world_cultural_assets
from world_module.service_detail_snapshot import (
    service_detail_cache_metadata,
    write_service_detail_snapshot,
)


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
WORLD_ROOT = PIPELINE_ROOT / "outputs" / "worlds"
MAPSEED_ROOT = PIPELINE_ROOT / "outputs" / "mapseeds"
ROUTE_ROOT = PIPELINE_ROOT / "outputs" / "routes"
CHARACTER_ROOT = PIPELINE_ROOT / "outputs" / "characters"
BENCHMARK_ROOT = PIPELINE_ROOT / "outputs" / "benchmarks"
CULTURE_ROOT = BENCHMARK_ROOT / "region_aware" / "cultural_constraints"
EVIDENCE_ROOT = BENCHMARK_ROOT / "region_aware" / "cultural_sources" / "wikivoyage"

SCHEMA_VERSION = "geoword.world.registry.v2"
PROVENANCE_SCHEMA_VERSION = "geoword.task.world_provenance.v2"

# These are the active, non-backup city worlds used to initialize world_001.
DEFAULT_CITY_WORLD_SOURCES = {
    "shanghai_smoke": "outputs/mapseeds/shanghai_smoke/base/world.json",
    "tokyo_complex": "outputs/mapseeds/tokyo_complex/complex/world.json",
    "london_smoke": "outputs/mapseeds/london_smoke/base/world.json",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def relative_path(path: Path) -> str:
    return path.resolve().relative_to(PIPELINE_ROOT.resolve()).as_posix()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def artifact_ref(path: Path, *, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "missing", "path": relative_path(path) if path.exists() else str(path)}
    payload = {
        "status": "available",
        "path": relative_path(path),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
    }
    if metadata:
        payload["metadata"] = metadata
    return payload


def resolve_world_route_cache(
    world_id: str,
    city_id: str,
    city_version: int | str | None = None,
) -> Path | None:
    """Resolve only the route cache belonging to one world city version."""
    version = _version_number(city_version)
    if not version:
        city_path = WORLD_ROOT / world_id / "cities" / f"{city_id}.json"
        if city_path.is_file():
            try:
                version = _version_number(read_json(city_path).get("active_version"))
            except Exception:
                version = 0
    if not version:
        return None
    candidate = WORLD_ROOT / world_id / "artifacts" / "routes" / city_id / f"v{version}" / "route_cache.json"
    return candidate if candidate.is_file() else None


def _route_cache_metadata(route_path: Path | None) -> dict[str, Any]:
    if not route_path or not route_path.is_file():
        return {}
    route_data = read_json(route_path)
    return {
        "city_id": route_data.get("city_id", ""),
        "cached_pair_count": route_data.get("cached_pair_count", 0),
        "cache_entries": len(route_data.get("cache", [])),
        "generation_statistics": route_data.get("generation_statistics", {}),
    }


def _city_counts(world: dict[str, Any]) -> dict[str, int]:
    return {
        "districts": len(world.get("districts", [])),
        "corridors": len(world.get("corridors", [])),
        "places": len(world.get("places", [])),
        "services": len(world.get("services", [])),
        "information_items": len(world.get("information_items", [])),
        "event_templates": len(world.get("event_templates", [])),
        "actors": len(world.get("actors", [])),
    }


def _version_number(value: Any) -> int:
    """Accept only the compact, ordered city version used by the UI."""
    if isinstance(value, int):
        return value if value > 0 else 0
    text = str(value or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    try:
        return max(0, int(text))
    except ValueError:
        return 0


def _version_counts(artifacts: dict[str, Any]) -> dict[str, int]:
    map_counts = ((artifacts.get("map_world") or {}).get("metadata") or {}).get("counts") or {}
    character_counts = ((artifacts.get("characters") or {}).get("metadata") or {})
    return {
        "districts": int(map_counts.get("districts", 0) or 0),
        "corridors": int(map_counts.get("corridors", 0) or 0),
        "places": int(map_counts.get("places", 0) or 0),
        "characters": int(character_counts.get("characters", 0) or 0),
    }


def _version_entry(record: dict[str, Any], version: int) -> dict[str, Any] | None:
    for entry in record.get("city_versions", []):
        if _version_number(entry.get("version")) == version:
            return entry
    return None


def _ensure_city_versions(record: dict[str, Any]) -> bool:
    """Give legacy records one explicit v1 snapshot without touching source assets."""
    if record.get("city_versions"):
        active = _version_number(record.get("active_version")) or 1
        record["active_version"] = active
        active_entry = _version_entry(record, active) or record["city_versions"][-1]
        record["artifacts"] = deepcopy(active_entry.get("artifacts") or record.get("artifacts") or {})
        record.setdefault("world_binding", {})["city_version"] = f"v{active}"
        return False

    created_at = (record.get("world_binding") or {}).get("city_version") or utc_now()
    artifacts = deepcopy(record.get("artifacts") or {})
    record["city_versions"] = [{
        "version": 1,
        "version_id": "v1",
        "kind": "initial",
        "created_at": created_at,
        "parent_version": None,
        "artifacts": artifacts,
        "counts": _version_counts(artifacts),
        "migration": "legacy_current_artifact_as_v1",
        "note": "Existing artifacts were registered as v1 without copying, regenerating, or modifying source entity files.",
    }]
    record["active_version"] = 1
    record.setdefault("world_binding", {})["city_version"] = "v1"
    record.setdefault("version_history", []).append({
        "operation": "migrate_legacy_city_to_v1",
        "at": utc_now(),
        "from": "legacy_unversioned",
        "to": "v1",
        "note": "Only registry metadata changed; source map, character, task, GT, review, and run files were retained.",
    })
    return True


def migrate_city_versions(world_id: str, city_id: str = "") -> dict[str, Any]:
    """Persist v1 metadata for legacy registered cities, never rewriting source assets."""
    manifest_path = WORLD_ROOT / world_id / "world_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"World registry does not exist: {manifest_path}")
    manifest = read_json(manifest_path)
    migrated: list[str] = []
    for entry in manifest.get("cities", []):
        candidate = entry.get("city_id", "")
        if city_id and candidate != city_id:
            continue
        record_path = PIPELINE_ROOT / entry.get("registry_path", "")
        if not record_path.is_file():
            continue
        record = read_json(record_path)
        changed = _ensure_city_versions(record)
        entry["active_version"] = record["active_version"]
        if changed:
            write_json(record_path, record)
            migrated.append(candidate)
    if migrated:
        manifest.setdefault("history", []).append({
            "operation": "migrate_legacy_city_versions",
            "at": utc_now(),
            "cities": migrated,
            "note": "Added registry-only v1 snapshots; no source city, entity, task, GT, review, or run files were changed.",
        })
        write_json(manifest_path, manifest)
    return {"world_id": world_id, "migrated_city_ids": migrated}


def city_version_context(world_id: str) -> dict[str, Any]:
    """Return the effective city version boundary for map, task, and test consumers."""
    manifest_path = WORLD_ROOT / world_id / "world_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"World registry does not exist: {manifest_path}")
    migrate_city_versions(world_id)
    manifest = read_json(manifest_path)
    versions: dict[str, int] = {}
    for entry in manifest.get("cities", []):
        record_path = PIPELINE_ROOT / entry.get("registry_path", "")
        if not record_path.is_file():
            continue
        record = read_json(record_path)
        versions[record.get("city_id", entry.get("city_id", ""))] = _version_number(record.get("active_version")) or 1
    return {"world_id": world_id, "active_city_versions": versions}


def select_city_version(world_id: str, city_id: str, version: int | str) -> dict[str, Any]:
    """Switch one city's effective data boundary while retaining all later artifacts."""
    desired = _version_number(version)
    if not desired:
        raise ValueError("city version must be a positive integer")
    manifest_path, city_path = _registry_paths(world_id, city_id)
    record = read_json(city_path)
    _ensure_city_versions(record)
    selected = _version_entry(record, desired)
    if not selected:
        raise ValueError(f"City version v{desired} does not exist for {city_id}")
    previous = _version_number(record.get("active_version")) or 1
    record["active_version"] = desired
    record["artifacts"] = deepcopy(selected.get("artifacts") or {})
    record.setdefault("world_binding", {})["city_version"] = f"v{desired}"
    record.setdefault("version_history", []).append({
        "operation": "select_city_version",
        "at": utc_now(),
        "from": f"v{previous}",
        "to": f"v{desired}",
        "note": "Changed only the active version boundary; no historical assets, tasks, reviews, GT, or runs were deleted or rewritten.",
    })
    write_json(city_path, record)
    manifest = read_json(manifest_path)
    for entry in manifest.get("cities", []):
        if entry.get("city_id") == city_id:
            entry["active_version"] = desired
    manifest.setdefault("history", []).append({
        "operation": "select_city_version",
        "at": utc_now(),
        "city_id": city_id,
        "from": f"v{previous}",
        "to": f"v{desired}",
    })
    write_json(manifest_path, manifest)
    return {"world_id": world_id, "city_id": city_id, "active_version": desired,
            "counts": selected.get("counts") or _version_counts(selected.get("artifacts") or {})}


def _find_matching_character_artifact(city_id: str) -> tuple[Path | None, list[dict[str, str]]]:
    candidates: list[dict[str, str]] = []
    matched: Path | None = None
    if not CHARACTER_ROOT.is_dir():
        return None, candidates
    for path in sorted(CHARACTER_ROOT.rglob("characters.json")):
        try:
            payload = read_json(path)
        except Exception:
            continue
        source_city_id = str(payload.get("city_id", ""))
        candidates.append({"path": relative_path(path), "city_id": source_city_id})
        if source_city_id == city_id:
            matched = path
    return matched, candidates


def _resolve_cultural_artifacts(city_id: str, *, world_id: str = "", import_mode: str = "legacy_import") -> tuple[Path | None, Path | None, str | None]:
    if world_id and import_mode in {"world_build", "city_expansion"}:
        local_root = WORLD_ROOT / world_id / "artifacts" / "cultural" / city_id
        constraint = local_root / "cultural_constraints.json"
        evidence = local_root / "wikivoyage_evidence.json"
        return (
            constraint if constraint.is_file() else None,
            evidence if evidence.is_file() else None,
            city_id,
        )
    candidate_ids = [city_id]
    for suffix in ("_corridor_smoke", "_smoke", "_complex", "_large", "_base"):
        if city_id.endswith(suffix):
            candidate_ids.append(city_id[: -len(suffix)])
            break
    first_partial: tuple[Path | None, Path | None, str] | None = None
    for candidate_id in candidate_ids:
        constraint = CULTURE_ROOT / f"{candidate_id}.json"
        evidence = EVIDENCE_ROOT / f"{candidate_id}.json"
        if constraint.is_file() and evidence.is_file():
            return (
                constraint,
                evidence,
                candidate_id,
            )
        if first_partial is None and (constraint.is_file() or evidence.is_file()):
            first_partial = (
                constraint if constraint.is_file() else None,
                evidence if evidence.is_file() else None,
                candidate_id,
            )
    if first_partial is not None:
        return first_partial
    return None, None, None


def _build_city_record(
    city_id: str,
    world_path: Path,
    *,
    character_path: Path | None = None,
    world_id: str = "",
    import_mode: str = "legacy_import",
    route_version: int | str | None = None,
) -> dict[str, Any]:
    world = read_json(world_path)
    city = (world.get("cities") or [{}])[0]
    source_city_id = city.get("city_id") or city_id
    if source_city_id != city_id:
        raise ValueError(f"World source {world_path} declares {source_city_id}, expected {city_id}")

    map_manifest_path = world_path.parent / "map_manifest.json"
    factual_service_index_path = world_path.parent / "factual_service_index.json"
    service_detail_cache_path = world_path.parent / "service_detail_cache.json"
    local_service_state_path = world_path.parent / "local_service_world_state.json"
    # Route caches are task-demanded and must be world-local. A formal city
    # build must never silently attach a cache created for another world.
    is_world_bound_build = world_id and import_mode in {"world_build", "city_expansion"}
    route_path = (
        resolve_world_route_cache(world_id, city_id, route_version)
        if is_world_bound_build
        else ROUTE_ROOT / city_id / "route_cache.json"
    )
    character_candidates: list[dict[str, str]] = []
    if character_path is None and import_mode == "legacy_import":
        character_path, character_candidates = _find_matching_character_artifact(city_id)
    constraint_path, evidence_path, culture_source_city = _resolve_cultural_artifacts(
        city_id, world_id=world_id, import_mode=import_mode,
    )

    route_metadata = _route_cache_metadata(route_path)

    character_metadata: dict[str, Any] = {}
    if character_path:
        character_data = read_json(character_path)
        character_metadata = {
            "city_id": character_data.get("city_id", ""),
            "characters": len(character_data.get("characters", [])),
            "relationships": len(character_data.get("relationships", [])),
        }

    factual_service_metadata: dict[str, Any] = {}
    if factual_service_index_path.is_file():
        factual_service_data = read_json(factual_service_index_path)
        factual_service_metadata = {
            "city_id": factual_service_data.get("city_id", ""),
            "services": int(factual_service_data.get("service_count", 0) or 0),
            "source_place_count": int(factual_service_data.get("source_place_count", 0) or 0),
            "source_place_signature": factual_service_data.get("source_place_signature", ""),
        }

    service_detail_metadata = service_detail_cache_metadata(service_detail_cache_path)
    local_service_state_metadata: dict[str, Any] = {}
    if local_service_state_path.is_file():
        local_service_state = read_json(local_service_state_path)
        local_service_state_metadata = {
            "services": len(local_service_state.get("services", []) or []),
            "city_id": ((local_service_state.get("world_binding") or {}).get("city_id") or ""),
            "map_world_sha256": ((local_service_state.get("world_binding") or {}).get("map_world_sha256") or ""),
        }

    constraint_metadata: dict[str, Any] = {}
    evidence_metadata: dict[str, Any] = {}
    if constraint_path:
        constraint_data = read_json(constraint_path)
        constraint_metadata = {
            "resolved_city_id": culture_source_city,
            "constraints": len(constraint_data.get("constraints", [])),
        }
    if evidence_path:
        evidence_data = read_json(evidence_path)
        evidence_metadata = {
            "resolved_city_id": culture_source_city,
            "evidence_cards": len(evidence_data.get("evidence_cards", [])),
        }

    capabilities = {
        "map_world": True,
        "route_runtime": bool(route_path and route_path.is_file()),
        "characters": character_path is not None,
        "regional_knowledge": constraint_path is not None and evidence_path is not None,
    }
    # Route cache is task-demanded and generated later by route-aware task
    # construction. It must not make an otherwise complete city partial.
    missing = [name for name, available in capabilities.items()
               if not available and name != "route_runtime"]
    return {
        "schema_version": SCHEMA_VERSION,
        "city_id": city_id,
        "city_name": city.get("name", ""),
        "country": city.get("country", ""),
        "timezone": city.get("timezone", ""),
        "lifecycle_status": "active",
        "import_mode": import_mode,
        "world_binding": {
            "world_id": world_id,
            "city_id": city_id,
            "city_version": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "binding_mode": "explicit_artifact_paths" if world_id else "legacy_artifact_discovery",
        },
        "capabilities": capabilities,
        "readiness": "ready" if not missing else "partial",
        "missing_capabilities": missing,
        "artifacts": {
            "map_world": artifact_ref(world_path, metadata={"counts": _city_counts(world)}),
            "map_manifest": artifact_ref(map_manifest_path),
            "factual_service_index": artifact_ref(factual_service_index_path, metadata=factual_service_metadata),
            "service_detail_cache": artifact_ref(service_detail_cache_path, metadata=service_detail_metadata),
            "local_service_world_state": artifact_ref(local_service_state_path, metadata=local_service_state_metadata),
        "route_cache": artifact_ref(
            route_path or (WORLD_ROOT / world_id / "artifacts" / "routes" / city_id / f"v{_version_number(route_version) or 1}" / "route_cache.json"),
            metadata=route_metadata,
        ),
            "characters": artifact_ref(character_path, metadata=character_metadata) if character_path else {
                "status": "missing",
                "reason": "No characters.json declares this exact city_id.",
                "legacy_candidates": character_candidates,
            },
            "cultural_constraints": artifact_ref(constraint_path, metadata=constraint_metadata) if constraint_path else {
                "status": "missing",
                "reason": "No admissible cultural constraint file was found for this city.",
            },
            "cultural_evidence": artifact_ref(evidence_path, metadata=evidence_metadata) if evidence_path else {
                "status": "missing",
                "reason": "No Wikivoyage evidence-card file was found for this city.",
            },
        },
        "construction_statistics": {
            "status": "legacy_not_reconstructed",
            "api_cost": None,
            "elapsed_seconds": None,
            "cache_hit_rate": None,
            "note": "Historical aggregate cost and duration were not fabricated during migration.",
        },
        # Appended operational facts only.  Historical values stay unknown unless
        # a real construction command records them here.
        "construction_attempts": [],
    }


def _unregistered_world_sources(imported_paths: set[Path]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    for world_path in MAPSEED_ROOT.rglob("world.json"):
        if "_backup_" in world_path.as_posix() or "intermediates" in world_path.parts:
            continue
        if world_path.resolve() in imported_paths:
            continue
        try:
            payload = read_json(world_path)
            city = (payload.get("cities") or [{}])[0]
            sources.append({
                "path": relative_path(world_path),
                "city_id": city.get("city_id", ""),
                "reason": "Legacy alternative source; not automatically imported into the active world.",
            })
        except Exception:
            continue
    return sorted(sources, key=lambda item: item["path"])


def build_legacy_world_registry(world_id: str = "world_001") -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    city_records: dict[str, dict[str, Any]] = {}
    imported_paths: set[Path] = set()
    for city_id, relative_source in DEFAULT_CITY_WORLD_SOURCES.items():
        world_path = PIPELINE_ROOT / relative_source
        if not world_path.is_file():
            raise FileNotFoundError(f"Required legacy city world is missing: {world_path}")
        city_records[city_id] = _build_city_record(city_id, world_path)
        imported_paths.add(world_path.resolve())

    registry = {
        "schema_version": SCHEMA_VERSION,
        "world_id": world_id,
        "display_name": "GeoWorld Initial Multi-City World",
        "lifecycle_status": "active",
        "created_at": utc_now(),
        "import_mode": "legacy_import",
        "city_count": len(city_records),
        "city_ids": sorted(city_records),
        "cities": [
            {
                "city_id": record["city_id"],
                "city_name": record["city_name"],
                "readiness": record["readiness"],
                "registry_path": f"outputs/worlds/{world_id}/cities/{record['city_id']}.json",
            }
            for record in city_records.values()
        ],
        "relationship_graph": {
            "default_scope": "current_city",
            "available_scopes": ["current_city", "world"],
            "world_graph_status": "not_materialized",
            "note": "Cross-city relationships are optional and are not inferred by legacy migration.",
        },
        "unregistered_legacy_world_sources": _unregistered_world_sources(imported_paths),
        "history": [{
            "operation": "legacy_import",
            "at": utc_now(),
            "note": "Registered existing city artifacts without copying, moving, regenerating, or mutating source data.",
        }],
    }
    return registry, city_records


def _task_batch_directories() -> list[Path]:
    if not BENCHMARK_ROOT.is_dir():
        return []
    directories: list[Path] = []
    for task_type_dir in BENCHMARK_ROOT.iterdir():
        if not task_type_dir.is_dir() or task_type_dir.name.startswith("_"):
            continue
        for city_dir in task_type_dir.iterdir():
            if not city_dir.is_dir() or city_dir.name.startswith("_"):
                continue
            for batch_dir in city_dir.iterdir():
                tasks_dir = batch_dir / "tasks"
                if batch_dir.is_dir() and batch_dir.name != "latest" and tasks_dir.is_dir():
                    if any((task_dir / "task.json").is_file() for task_dir in tasks_dir.iterdir() if task_dir.is_dir()):
                        directories.append(batch_dir)
    return sorted(directories)


def _task_count(tasks_dir: Path) -> int:
    """Count completed task folders; an unstarted batch has zero tasks."""
    if not tasks_dir.is_dir():
        return 0
    return sum(1 for task_dir in tasks_dir.iterdir() if task_dir.is_dir() and (task_dir / "task.json").is_file())


def write_legacy_world_import(world_id: str = "world_001", *, overwrite: bool = False) -> dict[str, Any]:
    registry, city_records = build_legacy_world_registry(world_id)
    world_dir = WORLD_ROOT / world_id
    manifest_path = world_dir / "world_manifest.json"
    if manifest_path.exists() and not overwrite:
        raise FileExistsError(f"World registry already exists: {manifest_path}. Use overwrite only for an intentional rebuild.")

    world_dir.mkdir(parents=True, exist_ok=True)
    city_dir = world_dir / "cities"
    city_dir.mkdir(exist_ok=True)
    for city_id, record in city_records.items():
        existing_path = city_dir / f"{city_id}.json"
        if existing_path.is_file():
            try:
                existing = read_json(existing_path)
                # Refresh artifact snapshots without losing measured commands that
                # were recorded after the original legacy import.
                record["construction_attempts"] = existing.get("construction_attempts", [])
                # A registry refresh must not erase later city versions or a
                # user's currently selected version.
                if existing.get("city_versions"):
                    record["city_versions"] = existing["city_versions"]
                    record["active_version"] = existing.get("active_version", 1)
                    record["version_history"] = existing.get("version_history", [])
                    active = _version_entry(record, _version_number(record["active_version"]))
                    if active:
                        record["artifacts"] = deepcopy(active.get("artifacts") or record["artifacts"])
                        record.setdefault("world_binding", {})["city_version"] = active.get("version_id", "v1")
            except Exception:
                pass
        write_json(existing_path, record)
    write_json(manifest_path, registry)

    provenance_written: list[str] = []
    unresolved_batches: list[str] = []
    for batch_dir in _task_batch_directories():
        task_type = batch_dir.parent.parent.name
        city_id = batch_dir.parent.name
        city_record = city_records.get(city_id)
        if city_record is None:
            unresolved_batches.append(relative_path(batch_dir))
            continue
        provenance_path = batch_dir / "world_provenance.json"
        if provenance_path.exists() and not overwrite:
            continue
        city_registry_path = city_dir / f"{city_id}.json"
        provenance = {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "world_id": world_id,
            "city_id": city_id,
            "task_type": task_type,
            "batch_id": batch_dir.name,
            "task_count": _task_count(batch_dir / "tasks"),
            "city_registry_path": relative_path(city_registry_path),
            "artifact_snapshot": {
                name: artifact.get("sha256")
                for name, artifact in city_record["artifacts"].items()
                if artifact.get("status") == "available" and artifact.get("sha256")
            },
            "import_mode": "legacy_import",
            "created_at": utc_now(),
            "note": "Sidecar provenance only; task payloads, ground truth, and review state were not modified.",
        }
        write_json(provenance_path, provenance)
        provenance_written.append(relative_path(provenance_path))

    report = {
        "schema_version": SCHEMA_VERSION,
        "world_id": world_id,
        "created_at": utc_now(),
        "city_count": len(city_records),
        "cities": sorted(city_records),
        "provenance_written": provenance_written,
        "unresolved_task_batches": unresolved_batches,
        "note": "No source world, task, ground-truth, review, or run artifact was changed by this import.",
    }
    write_json(world_dir / "legacy_import_report.json", report)
    return report


def create_empty_world(world_id: str, *, display_name: str | None = None) -> dict[str, Any]:
    """Create an empty multi-city container without creating any city artifacts."""
    if not world_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in world_id):
        raise ValueError("world_id must contain only lowercase letters, digits, '_' or '-'.")
    manifest_path = WORLD_ROOT / world_id / "world_manifest.json"
    if manifest_path.exists():
        raise FileExistsError(f"World registry already exists: {manifest_path}")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    registry = {
        "schema_version": SCHEMA_VERSION,
        "world_id": world_id,
        "display_name": display_name or world_id,
        "lifecycle_status": "active",
        "created_at": utc_now(),
        "import_mode": "native_empty_world",
        "city_count": 0,
        "city_ids": [],
        "cities": [],
        "relationship_graph": {
            "default_scope": "current_city",
            "available_scopes": ["current_city", "world"],
            "world_graph_status": "not_materialized",
            "note": "Cross-city relationships are optional and are never inferred automatically.",
        },
        "history": [{
            "operation": "create_empty_world",
            "at": utc_now(),
            "note": "Created an empty world container; no city, POI, route, character, or task data was generated.",
        }],
    }
    write_json(manifest_path, registry)
    return registry


def attach_city_artifact(
    world_id: str,
    city_id: str,
    world_path: str | Path,
    *,
    character_path: str | Path | None = None,
    import_mode: str = "world_build",
    city_version: int | str | None = None,
    registration_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Register one explicit city artifact snapshot and make that version active."""
    manifest_path = WORLD_ROOT / world_id / "world_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"World registry does not exist: {manifest_path}")
    path = Path(world_path)
    if not path.is_absolute():
        path = PIPELINE_ROOT / path
    city_path = manifest_path.parent / "cities" / f"{city_id}.json"
    explicit_char_path = Path(character_path) if character_path else None
    if explicit_char_path and not explicit_char_path.is_absolute():
        explicit_char_path = PIPELINE_ROOT / explicit_char_path
    world_payload = read_json(path)
    source_city = (world_payload.get("cities") or [{}])[0]
    if import_mode == "world_build":
        cultural = materialize_world_cultural_assets(
            world_id, city_id, str(source_city.get("name") or city_id),
        )
        if cultural.get("status") != "available":
            raise ValueError(f"World-local cultural assets unavailable: {cultural.get('reason', 'unknown')}")
    requested_version = _version_number(city_version)
    if city_path.is_file() and not requested_version:
        try:
            requested_version = _version_number(read_json(city_path).get("active_version"))
        except Exception:
            requested_version = 0
    requested_version = requested_version or 1
    if city_path.is_file() and requested_version > 1 and import_mode == "city_expansion":
        previous_record = read_json(city_path)
        _inherit_parent_route_cache(
            world_id, city_id, requested_version, previous_record, world_payload,
        )
    record = _build_city_record(
        city_id,
        path,
        character_path=explicit_char_path,
        world_id=world_id,
        import_mode=import_mode,
        route_version=requested_version,
    )
    if city_path.is_file():
        try:
            previous = read_json(city_path)
            _ensure_city_versions(previous)
            record["construction_attempts"] = previous.get("construction_attempts", [])
            record["expansion_history"] = previous.get("expansion_history", [])
            record["version_history"] = previous.get("version_history", [])
            versions = deepcopy(previous.get("city_versions", []))
            active_version = _version_number(previous.get("active_version")) or 1
            existing_version = _version_entry({"city_versions": versions}, requested_version)
            snapshot = {
                "version": requested_version,
                "version_id": f"v{requested_version}",
                "kind": "initial" if requested_version == 1 else "expansion",
                "created_at": (existing_version or {}).get("created_at", utc_now()),
                "parent_version": None if requested_version == 1 else requested_version - 1,
                "artifacts": deepcopy(record["artifacts"]),
                "counts": _version_counts(record["artifacts"]),
            }
            if registration_metadata:
                snapshot["registration_metadata"] = deepcopy(registration_metadata)
            if existing_version:
                versions = [snapshot if _version_number(item.get("version")) == requested_version else item for item in versions]
            else:
                versions.append(snapshot)
            record["city_versions"] = sorted(versions, key=lambda item: _version_number(item.get("version")))
            record["active_version"] = requested_version
            record["world_binding"]["city_version"] = f"v{requested_version}"
            if previous.get("expansion_history"):
                record["expansion_history"] = previous["expansion_history"]
        except Exception:
            pass
    else:
        record["city_versions"] = [{
            "version": requested_version,
            "version_id": f"v{requested_version}",
            "kind": "initial" if requested_version == 1 else "expansion",
            "created_at": utc_now(),
            "parent_version": None if requested_version == 1 else requested_version - 1,
            "artifacts": deepcopy(record["artifacts"]),
            "counts": _version_counts(record["artifacts"]),
        }]
        if registration_metadata:
            record["city_versions"][0]["registration_metadata"] = deepcopy(registration_metadata)
        record["active_version"] = requested_version
        record["world_binding"]["city_version"] = f"v{requested_version}"
    city_path.parent.mkdir(exist_ok=True)
    write_json(city_path, record)

    registry = read_json(manifest_path)
    city_entries = [entry for entry in registry.get("cities", []) if entry.get("city_id") != city_id]
    city_entries.append({
        "city_id": record["city_id"],
        "city_name": record["city_name"],
        "readiness": record["readiness"],
        "active_version": record["active_version"],
        "registry_path": relative_path(city_path),
    })
    registry["cities"] = sorted(city_entries, key=lambda entry: entry["city_id"])
    registry["city_ids"] = [entry["city_id"] for entry in registry["cities"]]
    registry["city_count"] = len(registry["cities"])
    registry.setdefault("history", []).append({
        "operation": "attach_existing_city_artifact",
        "at": utc_now(),
        "city_id": city_id,
        "city_version": f"v{record['active_version']}",
        "source_world": relative_path(path),
        "note": "Registered explicit city map and character artifact paths for this world version.",
        "registration_metadata": deepcopy(registration_metadata) if registration_metadata else {},
    })
    write_json(manifest_path, registry)
    return record


def _inherit_parent_route_cache(
    world_id: str, city_id: str, child_version: int,
    city_record: dict[str, Any], child_world: dict[str, Any],
) -> dict[str, Any]:
    """Seed a real expansion version with parent routes whose endpoints still exist."""
    child_path = (
        WORLD_ROOT / world_id / "artifacts" / "routes" / city_id
        / f"v{child_version}" / "route_cache.json"
    )
    if child_path.is_file():
        return {"inherited": False, "reason": "child_route_cache_exists", "path": relative_path(child_path)}
    parent_version = child_version - 1
    parent_path = (
        WORLD_ROOT / world_id / "artifacts" / "routes" / city_id
        / f"v{parent_version}" / "route_cache.json"
    )
    if not parent_path.is_file():
        return {"inherited": False, "reason": "parent_route_cache_missing"}

    places = {
        str(place.get("place_id") or ""): place
        for place in child_world.get("places", []) if place.get("place_id")
    }
    coordinates = {
        (_route_coord(place.get("location", {}).get("lat")),
         _route_coord(place.get("location", {}).get("lng"))): place
        for place in places.values()
    }

    def resolve_endpoint(entry: dict[str, Any], prefix: str) -> dict[str, Any] | None:
        place = places.get(str(entry.get(f"{prefix}_place_id") or ""))
        if place is not None:
            return place
        return coordinates.get((
            _route_coord(entry.get(f"{prefix}_lat")),
            _route_coord(entry.get(f"{prefix}_lng")),
        ))

    inherited: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in read_json(parent_path).get("cache", []):
        if not isinstance(entry.get("route"), dict) or entry.get("route_ref"):
            continue
        if entry.get("waypoint_place_id"):
            continue
        origin = resolve_endpoint(entry, "origin")
        destination = resolve_endpoint(entry, "dest")
        mode = str(entry.get("mode") or "")
        snapshot_id = str(entry.get("snapshot_id") or "static_v1")
        if not origin or not destination or not mode:
            continue
        raw = f"{origin['place_id']}|{destination['place_id']}|{mode}|snapshot={snapshot_id}"
        key = hashlib.md5(raw.encode()).hexdigest()[:16]
        if key in seen:
            continue
        seen.add(key)
        inherited.append({
            **deepcopy(entry),
            "route_id": f"route_{city_id}_{key[:12]}",
            "origin_place_id": origin["place_id"],
            "origin_name": origin.get("name", ""),
            "origin_lat": origin.get("location", {}).get("lat", 0),
            "origin_lng": origin.get("location", {}).get("lng", 0),
            "dest_place_id": destination["place_id"],
            "dest_name": destination.get("name", ""),
            "dest_lat": destination.get("location", {}).get("lat", 0),
            "dest_lng": destination.get("location", {}).get("lng", 0),
            "snapshot_id": snapshot_id,
        })
    write_json(child_path, {
        "city_id": city_id,
        "cached_pair_count": len(inherited),
        "generation_statistics": {
            "inherited_from_city_version": f"v{parent_version}",
            "external_api_requests": 0,
        },
        "cache": inherited,
    })
    return {
        "inherited": True, "from_version": f"v{parent_version}",
        "to_version": f"v{child_version}", "route_count": len(inherited),
        "path": relative_path(child_path),
    }


def _route_coord(value: Any) -> str | None:
    try:
        number = round(float(value), 6)
    except (TypeError, ValueError):
        return None
    return f"{number:.6f}"


def refresh_city_route_cache_asset(world_id: str, city_id: str) -> dict[str, Any]:
    """Register a generated versioned route cache without creating a city version.

    Routes are derived task-runtime data.  Refreshing their reference changes
    neither the POI/person/entity boundary nor the selected city version.
    """
    _, city_path = _registry_paths(world_id, city_id)
    record = read_json(city_path)
    _ensure_city_versions(record)
    version = _version_number(record.get("active_version")) or 1
    route_path = resolve_world_route_cache(world_id, city_id, version)
    if not route_path:
        return {"updated": False, "reason": "versioned_route_cache_missing", "city_version": f"v{version}"}

    route_ref = artifact_ref(route_path, metadata=_route_cache_metadata(route_path))
    existing = _version_entry(record, version)
    if not existing:
        raise ValueError(f"City {city_id} has no active version v{version}.")
    prior = (existing.get("artifacts") or {}).get("route_cache") or {}
    if prior.get("sha256") == route_ref.get("sha256"):
        return {"updated": False, "reason": "already_current", "city_version": f"v{version}", "route_cache": route_ref}

    existing.setdefault("artifacts", {})["route_cache"] = route_ref
    record["artifacts"] = deepcopy(existing["artifacts"])
    record.setdefault("capabilities", {})["route_runtime"] = True
    record.setdefault("derived_asset_refreshes", []).append({
        "operation": "refresh_versioned_route_cache",
        "at": utc_now(),
        "city_version": f"v{version}",
        "route_cache": relative_path(route_path),
        "note": "Registered task-generated route cache for the existing city version; no entity or version change.",
    })
    write_json(city_path, record)
    return {"updated": True, "city_version": f"v{version}", "route_cache": route_ref}


def record_city_route_coverage_preparation(
    world_id: str,
    city_id: str,
    *,
    coverage_report: Path,
    route_cache_before_sha256: str,
    route_cache_after_sha256: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Append a version-local route coverage record without changing entities.

    A coverage report describes pre-generation derived-route work.  It is
    intentionally separate from task provenance and from city-version changes;
    later task provenance binds the resulting route-cache hash.
    """
    _, city_path = _registry_paths(world_id, city_id)
    record = read_json(city_path)
    _ensure_city_versions(record)
    version = _version_number(record.get("active_version")) or 1
    cache_path = resolve_world_route_cache(world_id, city_id, version)
    if not cache_path:
        raise FileNotFoundError(f"Missing versioned route cache for {world_id}/{city_id}/v{version}.")
    if sha256_file(cache_path) != route_cache_after_sha256:
        raise ValueError("Coverage report hash does not match the current versioned route cache.")
    entry = {
        "operation": "prepare_region_route_coverage",
        "at": utc_now(),
        "city_version": f"v{version}",
        "coverage_report": relative_path(coverage_report),
        "route_cache": relative_path(cache_path),
        "route_cache_before_sha256": route_cache_before_sha256,
        "route_cache_after_sha256": route_cache_after_sha256,
        "note": "Bounded pre-generation Region route coverage; no city entities or historical task files changed.",
        "details": details,
    }
    record.setdefault("derived_asset_refreshes", []).append(entry)
    write_json(city_path, record)
    return entry


def record_city_expansion(
    world_id: str,
    city_id: str,
    *,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Append one non-destructive city expansion to the registry and manifest."""
    manifest_path, city_path = _registry_paths(world_id, city_id)
    record = read_json(city_path)
    entry = {"at": utc_now(), **details}
    record.setdefault("expansion_history", []).append(entry)
    write_json(city_path, record)

    manifest = read_json(manifest_path)
    manifest.setdefault("history", []).append({
        "operation": "expand_registered_city",
        "at": entry["at"],
        "city_id": city_id,
        "expansion_id": details.get("expansion_id", ""),
        "note": "Added a new city construction batch while retaining prior artifact paths and provenance.",
    })
    write_json(manifest_path, manifest)
    return entry


def refresh_city_cultural_assets(world_id: str, city_id: str = "") -> dict[str, Any]:
    """Backfill regional source/candidate assets without rebuilding city data."""
    manifest_path = WORLD_ROOT / world_id / "world_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"World registry does not exist: {manifest_path}")
    manifest = read_json(manifest_path)
    refreshed: list[dict[str, Any]] = []
    for entry in manifest.get("cities", []):
        candidate = str(entry.get("city_id", ""))
        if city_id and candidate != city_id:
            continue
        record_path = PIPELINE_ROOT / entry.get("registry_path", "")
        if not record_path.is_file():
            continue
        record = read_json(record_path)
        _ensure_city_versions(record)
        result = ensure_cultural_assets(candidate, str(record.get("city_name", candidate)))
        constraint_path, evidence_path, source_city = _resolve_cultural_artifacts(candidate)
        artifacts = record.setdefault("artifacts", {})
        artifacts["cultural_constraints"] = artifact_ref(constraint_path) if constraint_path else {
            "status": "missing", "reason": result.get("reason", "regional_asset_unavailable"),
        }
        artifacts["cultural_evidence"] = artifact_ref(evidence_path) if evidence_path else {
            "status": "missing", "reason": result.get("reason", "regional_asset_unavailable"),
        }
        capabilities = record.setdefault("capabilities", {})
        capabilities["regional_knowledge"] = constraint_path is not None and evidence_path is not None
        record["missing_capabilities"] = [name for name, available in capabilities.items()
                                         if not available and name != "route_runtime"]
        record["readiness"] = "ready" if not record["missing_capabilities"] else "partial"
        active = _version_entry(record, _version_number(record.get("active_version")) or 1)
        if active:
            active["artifacts"] = deepcopy(artifacts)
            active["counts"] = _version_counts(artifacts)
        record.setdefault("construction_attempts", []).append({
            "at": utc_now(), "artifact_type": "cultural_knowledge",
            "status": "completed" if result.get("status") == "available" else "skipped",
            "details": {"operation": "registry_cultural_backfill", "result": result,
                        "resolved_city_id": source_city or ""},
        })
        write_json(record_path, record)
        entry["readiness"] = record["readiness"]
        refreshed.append({"city_id": candidate, "status": result.get("status"),
                          "readiness": record["readiness"], "result": result})
    manifest.setdefault("history", []).append({
        "operation": "refresh_city_cultural_assets", "at": utc_now(),
        "city_id": city_id or "all", "cities": [item["city_id"] for item in refreshed],
        "note": "Refreshed Wikivoyage evidence and candidate constraints only; maps, characters, tasks, GT, reviews, and runs were retained.",
    })
    write_json(manifest_path, manifest)
    return {"world_id": world_id, "refreshed": refreshed}


def materialize_city_factual_service_indexes(world_id: str, city_id: str = "") -> dict[str, Any]:
    """Write and register version-local factual service sidecars.

    This is a deterministic derived-artifact operation.  It reads only the
    active map's POI types, must not create/select a city version, and leaves
    task batches and their immutable provenance sidecars untouched.
    """
    manifest_path = WORLD_ROOT / world_id / "world_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"World registry does not exist: {manifest_path}")
    manifest = read_json(manifest_path)
    materialized: list[dict[str, Any]] = []
    for entry in manifest.get("cities", []):
        candidate = str(entry.get("city_id") or "")
        if city_id and candidate != city_id:
            continue
        record_path = PIPELINE_ROOT / entry.get("registry_path", "")
        if not record_path.is_file():
            continue
        record = read_json(record_path)
        _ensure_city_versions(record)
        world_ref = (record.get("artifacts") or {}).get("map_world") or {}
        world_path = PIPELINE_ROOT / str(world_ref.get("path") or "")
        if world_ref.get("status") != "available" or not world_path.is_file():
            materialized.append({"city_id": candidate, "status": "skipped", "reason": "map_world_unavailable"})
            continue

        world = read_json(world_path)
        index = build_factual_service_index_artifact(world)
        index_path = world_path.parent / "factual_service_index.json"
        write_json(index_path, index)
        artifacts = record.setdefault("artifacts", {})
        artifacts["factual_service_index"] = artifact_ref(index_path, metadata={
            "city_id": index["city_id"],
            "services": index["service_count"],
            "source_place_count": index["source_place_count"],
            "source_place_signature": index["source_place_signature"],
        })
        active_version = _version_number(record.get("active_version")) or 1
        active = _version_entry(record, active_version)
        if active:
            active["artifacts"] = deepcopy(artifacts)
            active["counts"] = _version_counts(artifacts)
        record.setdefault("derived_artifact_history", []).append({
            "operation": "materialize_factual_service_index",
            "at": utc_now(),
            "city_version": f"v{active_version}",
            "path": relative_path(index_path),
            "services": index["service_count"],
            "source_place_signature": factual_service_place_signature(list(world.get("places") or [])),
            "note": "Deterministic POI-type projection only; no POI, character, district, corridor, task, GT, review, or run changed.",
        })
        write_json(record_path, record)
        materialized.append({
            "city_id": candidate,
            "city_version": f"v{active_version}",
            "status": "available",
            "path": relative_path(index_path),
            "services": index["service_count"],
        })

    manifest.setdefault("history", []).append({
        "operation": "materialize_factual_service_indexes",
        "at": utc_now(),
        "city_id": city_id or "all",
        "cities": [{"city_id": item["city_id"], "city_version": item.get("city_version", "")}
                   for item in materialized if item.get("status") == "available"],
        "note": "Registered deterministic POI-type sidecars only; no city version was created or selected and no task/review/evaluation artifact changed.",
    })
    write_json(manifest_path, manifest)
    return {"world_id": world_id, "materialized": materialized}


def materialize_city_service_detail_snapshots(
    world_id: str,
    city_id: str = "",
    *,
    resume: bool = True,
    retry_failures: bool = False,
    expected_city_version: int | None = None,
    expected_map_world_sha256: str = "",
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Fetch and freeze service details for every POI in active city versions.

    This writes companion artifacts only. It never changes a map entity, POI
    identity, active city version, task, GT, review, or evaluation result.
    """
    manifest_path = WORLD_ROOT / world_id / "world_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"World registry does not exist: {manifest_path}")
    manifest = read_json(manifest_path)
    materialized: list[dict[str, Any]] = []
    for entry in manifest.get("cities", []):
        candidate = str(entry.get("city_id") or "")
        if city_id and candidate != city_id:
            continue
        record_path = PIPELINE_ROOT / str(entry.get("registry_path") or "")
        if not record_path.is_file():
            continue
        record = read_json(record_path)
        _ensure_city_versions(record)
        active_version = _version_number(record.get("active_version")) or 1
        if expected_city_version is not None and active_version != int(expected_city_version):
            raise ValueError("The active city version changed before service-detail construction started")
        world_ref = (record.get("artifacts") or {}).get("map_world") or {}
        world_path = PIPELINE_ROOT / str(world_ref.get("path") or "")
        if world_ref.get("status") != "available" or not world_path.is_file():
            materialized.append({"city_id": candidate, "status": "skipped", "reason": "map_world_unavailable"})
            continue
        map_world_sha256 = sha256_file(world_path)
        if expected_map_world_sha256 and map_world_sha256 != expected_map_world_sha256:
            raise ValueError("The city map changed before service-detail construction started")
        result = write_service_detail_snapshot(
            world_path,
            resume=resume,
            retry_failures=retry_failures,
            progress_callback=progress_callback,
        )
        cache_path = Path(result["cache_path"])
        state_path = Path(result["state_path"])
        checkpoint_path = Path(result["checkpoint_path"])
        cache_metadata = service_detail_cache_metadata(cache_path)
        state = result["state"]
        state_metadata = {
            "city_id": ((state.get("world_binding") or {}).get("city_id") or ""),
            "map_world_sha256": ((state.get("world_binding") or {}).get("map_world_sha256") or ""),
            "services": len(state.get("services") or []),
        }
        if progress_callback is not None:
            progress_callback({"phase": "registering"})
        latest_record = read_json(record_path)
        _ensure_city_versions(latest_record)
        latest_active_version = _version_number(latest_record.get("active_version")) or 1
        latest_world_ref = (latest_record.get("artifacts") or {}).get("map_world") or {}
        latest_world_path = PIPELINE_ROOT / str(latest_world_ref.get("path") or "")
        if (latest_active_version != active_version or not latest_world_path.is_file()
                or latest_world_path.resolve() != world_path.resolve()
                or sha256_file(latest_world_path) != map_world_sha256):
            raise ValueError("The active city version changed while service details were being built")
        record = latest_record
        artifacts = record.setdefault("artifacts", {})
        artifacts["service_detail_cache"] = artifact_ref(cache_path, metadata=cache_metadata)
        artifacts["local_service_world_state"] = artifact_ref(state_path, metadata=state_metadata)
        active = _version_entry(record, active_version)
        if active:
            active["artifacts"] = deepcopy(artifacts)
            active["counts"] = _version_counts(artifacts)
        record.setdefault("derived_artifact_history", []).append({
            "operation": "materialize_service_detail_snapshot",
            "at": utc_now(),
            "city_version": f"v{active_version}",
            "cache_path": relative_path(cache_path),
            "state_path": relative_path(state_path),
            "services": len(state.get("services") or []),
            "source_place_count": cache_metadata.get("source_place_count", 0),
            "map_world_sha256": cache_metadata.get("map_world_sha256", ""),
            "note": "Website-backed companion artifacts only; map facts, city versions, tasks, GT, reviews, and runs were not modified.",
        })
        write_json(record_path, record)
        checkpoint_path.unlink(missing_ok=True)
        materialized.append({
            "city_id": candidate,
            "city_version": f"v{active_version}",
            "status": "available",
            "cache_path": relative_path(cache_path),
            "state_path": relative_path(state_path),
            "services": len(state.get("services") or []),
            "status_counts": cache_metadata.get("status_counts", {}),
        })
    manifest.setdefault("history", []).append({
        "operation": "materialize_service_detail_snapshots",
        "at": utc_now(),
        "city_id": city_id or "all",
        "cities": [{"city_id": item["city_id"], "city_version": item.get("city_version", "")}
                   for item in materialized if item.get("status") == "available"],
        "note": "Registered version-bound service cache and LocalServiceApp state companions; no map or task artifact was rewritten.",
    })
    write_json(manifest_path, manifest)
    return {"world_id": world_id, "materialized": materialized}


def repair_missing_factual_services(world_id: str, city_id: str = "") -> dict[str, Any]:
    """Correct historical service projections without version churn.

    A projection is corrected when it is empty, incomplete, or still carries
    obsolete non-factual service fields.  It does not alter any POI,
    character, district, corridor, task, ground-truth, review, or evaluation
    payload.  Task provenance sidecars receive an explicit hash correction
    entry so a map checksum never changes silently.
    """
    manifest_path = WORLD_ROOT / world_id / "world_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"World registry does not exist: {manifest_path}")
    manifest = read_json(manifest_path)
    corrections: list[dict[str, Any]] = []
    hash_replacements: dict[tuple[str, int, str], dict[str, str]] = {}

    for entry in manifest.get("cities", []):
        candidate = str(entry.get("city_id") or "")
        if city_id and candidate != city_id:
            continue
        record_path = PIPELINE_ROOT / entry.get("registry_path", "")
        if not record_path.is_file():
            continue
        record = read_json(record_path)
        _ensure_city_versions(record)
        changed_versions: list[dict[str, Any]] = []
        for version in record.get("city_versions", []):
            number = _version_number(version.get("version"))
            artifacts = version.setdefault("artifacts", {})
            world_ref = artifacts.get("map_world") or {}
            world_path = PIPELINE_ROOT / str(world_ref.get("path") or "")
            if world_ref.get("status") != "available" or not world_path.is_file():
                continue
            world = read_json(world_path)
            before_hash = sha256_file(world_path)
            corrected = materialize_factual_services(world)
            services = list(corrected.get("services") or [])
            if not services:
                continue
            projection_changed = list(world.get("services") or []) != services
            if projection_changed:
                # Only the derived service field is corrected.  The rest of
                # this historical world artifact remains byte-for-byte
                # semantically untouched.
                world["services"] = services
                write_json(world_path, world)
            index_path = world_path.parent / "factual_service_index.json"
            index = build_factual_service_index_artifact(world)
            write_json(index_path, index)
            artifacts["map_world"] = artifact_ref(world_path, metadata={"counts": _city_counts(world)})
            artifacts["factual_service_index"] = artifact_ref(index_path, metadata={
                "city_id": index["city_id"], "services": index["service_count"],
                "source_place_count": index["source_place_count"],
                "source_place_signature": index["source_place_signature"],
            })
            version["counts"] = _version_counts(artifacts)
            after_hash = artifacts["map_world"]["sha256"]
            replacement = {"before_map_sha256": before_hash, "after_map_sha256": after_hash,
                           "factual_service_index_sha256": artifacts["factual_service_index"]["sha256"]}
            if projection_changed:
                hash_replacements[(candidate, number, before_hash)] = replacement
            changed_versions.append({
                "city_version": f"v{number}", "services": len(services),
                "projection_corrected": projection_changed, **replacement,
            })

        if not changed_versions:
            continue
        active_number = _version_number(record.get("active_version")) or 1
        active = _version_entry(record, active_number)
        if active:
            record["artifacts"] = deepcopy(active.get("artifacts") or {})
        record.setdefault("derived_data_corrections", []).append({
            "operation": "repair_missing_factual_services",
            "at": utc_now(),
            "versions": changed_versions,
            "note": "Filled only missing factual services derived from existing POI Google types; no entity-set change and no city version change.",
        })
        write_json(record_path, record)
        corrections.append({"city_id": candidate, "versions": changed_versions})

    provenance_updates = _record_factual_service_provenance_corrections(world_id, hash_replacements)
    manifest.setdefault("history", []).append({
        "operation": "repair_missing_factual_services",
        "at": utc_now(),
        "city_id": city_id or "all",
        "cities": corrections,
        "provenance_updates": provenance_updates,
        "note": "Corrected empty services fields from frozen POI types; no city version was created or selected.",
    })
    write_json(manifest_path, manifest)
    return {"world_id": world_id, "corrections": corrections, "provenance_updates": provenance_updates}


def _record_factual_service_provenance_corrections(
    world_id: str, hash_replacements: dict[tuple[str, int, str], dict[str, str]],
) -> list[str]:
    """Keep batch checksums auditable after a derived-field correction."""
    updated: list[str] = []
    if not hash_replacements:
        return updated
    for batch_dir in _task_batch_directories():
        provenance_path = batch_dir / "world_provenance.json"
        if not provenance_path.is_file():
            continue
        try:
            provenance = read_json(provenance_path)
        except Exception:
            continue
        if provenance.get("world_id") != world_id:
            continue
        city_id = str(provenance.get("city_id") or "")
        version = _version_number(provenance.get("city_version"))
        before_hash = str((provenance.get("artifact_snapshot") or {}).get("map_world") or "")
        replacement = hash_replacements.get((city_id, version, before_hash))
        if not replacement:
            continue
        snapshot = provenance.setdefault("artifact_snapshot", {})
        snapshot["map_world"] = replacement["after_map_sha256"]
        snapshot["factual_service_index"] = replacement["factual_service_index_sha256"]
        provenance.setdefault("derived_data_corrections", []).append({
            "operation": "repair_missing_factual_services",
            "at": utc_now(),
            "city_version": f"v{version}",
            "before_map_sha256": replacement["before_map_sha256"],
            "after_map_sha256": replacement["after_map_sha256"],
            "note": "Only the missing services projection was filled from the same frozen POI Google types; task payload, GT, review, and run outputs were unchanged.",
        })
        write_json(provenance_path, provenance)
        updated.append(relative_path(provenance_path))
    return updated


def next_city_version(world_id: str, city_id: str) -> int:
    """Return the next linear version number without mutating the registry."""
    _, city_path = _registry_paths(world_id, city_id)
    record = read_json(city_path)
    _ensure_city_versions(record)
    versions = [_version_number(item.get("version")) for item in record.get("city_versions", [])]
    return (max(versions) if versions else 0) + 1


def _registry_paths(world_id: str, city_id: str) -> tuple[Path, Path]:
    manifest_path = WORLD_ROOT / world_id / "world_manifest.json"
    city_path = manifest_path.parent / "cities" / f"{city_id}.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"World registry does not exist: {manifest_path}")
    if not city_path.is_file():
        raise FileNotFoundError(f"Registered city does not exist: {city_path}")
    return manifest_path, city_path


def set_city_lifecycle_status(world_id: str, city_id: str, lifecycle_status: str) -> dict[str, Any]:
    """Archive or restore a registered city without touching source assets."""
    if lifecycle_status not in {"active", "archived"}:
        raise ValueError("lifecycle_status must be active or archived")
    manifest_path, city_path = _registry_paths(world_id, city_id)
    record = read_json(city_path)
    prior = record.get("lifecycle_status", "active")
    record["lifecycle_status"] = lifecycle_status
    record.setdefault("lifecycle_history", []).append({
        "at": utc_now(),
        "from": prior,
        "to": lifecycle_status,
        "operation": "archive_city" if lifecycle_status == "archived" else "restore_city",
        "note": "Registration and source artifacts were retained; only normal world aggregation changes.",
    })
    write_json(city_path, record)
    manifest = read_json(manifest_path)
    for entry in manifest.get("cities", []):
        if entry.get("city_id") == city_id:
            entry["lifecycle_status"] = lifecycle_status
    manifest.setdefault("history", []).append({
        "operation": "archive_city" if lifecycle_status == "archived" else "restore_city",
        "at": utc_now(),
        "city_id": city_id,
        "note": "No source asset, task, ground truth, review, or run artifact was changed.",
    })
    write_json(manifest_path, manifest)
    return record


def detach_city_from_world(world_id: str, city_id: str) -> dict[str, Any]:
    """Remove a city membership while retaining the city registry record and assets."""
    manifest_path, city_path = _registry_paths(world_id, city_id)
    manifest = read_json(manifest_path)
    entries = [entry for entry in manifest.get("cities", []) if entry.get("city_id") != city_id]
    if len(entries) == len(manifest.get("cities", [])):
        raise ValueError(f"City is not attached to world: {city_id}")
    manifest["cities"] = entries
    manifest["city_ids"] = [entry.get("city_id", "") for entry in entries]
    manifest["city_count"] = len(entries)
    manifest.setdefault("detached_cities", []).append({
        "city_id": city_id,
        "registry_path": relative_path(city_path),
        "detached_at": utc_now(),
        "note": "Source assets and city registry record are retained for provenance and possible reattachment.",
    })
    manifest.setdefault("history", []).append({
        "operation": "detach_city_from_world",
        "at": utc_now(),
        "city_id": city_id,
        "note": "Removed only this world membership; did not delete source assets, tasks, ground truth, reviews, or runs.",
    })
    write_json(manifest_path, manifest)
    return {"world_id": world_id, "city_id": city_id, "detached": True, "registry_path": relative_path(city_path)}


def record_construction_attempt(
    world_id: str,
    city_id: str,
    *,
    artifact_type: str,
    status: str,
    details: dict[str, Any],
) -> dict[str, Any]:
    """Append an auditable real construction attempt to one registered city."""
    city_path = WORLD_ROOT / world_id / "cities" / f"{city_id}.json"
    if not city_path.is_file():
        raise FileNotFoundError(f"Registered city does not exist: {city_path}")
    if status not in {"completed", "failed", "skipped"}:
        raise ValueError("status must be completed, failed, or skipped")
    record = read_json(city_path)
    attempt = {
        "at": utc_now(),
        "artifact_type": artifact_type,
        "status": status,
        "details": details,
    }
    record.setdefault("construction_attempts", []).append(attempt)
    write_json(city_path, record)
    return attempt


def write_task_world_provenance(
    world_id: str,
    city_id: str,
    batch_dir: str | Path,
    *,
    task_type: str,
    mode: str,
    task_folders: list[Path] | None = None,
    generation_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record one append-only generation context and task-level bindings.

    A batch may legitimately receive tasks after a city version is switched in
    World Studio.  The batch stays world-scoped; each newly written task keeps
    its own exact source binding rather than retroactively relabelling older
    tasks.
    """
    _, city_path = _registry_paths(world_id, city_id)
    record = read_json(city_path)
    batch_path = Path(batch_dir)
    # A new formal batch acquires provenance before the first candidate is
    # written.  Establish the canonical empty bundle so this initial audit
    # record cannot fail merely because ``tasks/`` has no entries yet.
    (batch_path / "tasks").mkdir(parents=True, exist_ok=True)
    _ensure_city_versions(record)
    active_version = _version_number(record.get("active_version")) or 1
    artifacts = record.get("artifacts", {})
    version_context = city_version_context(world_id)
    world_versions = version_context["active_city_versions"]
    context = generation_context or {
        "generation_id": f"{utc_now()}::{task_type}::{mode}",
        "at": utc_now(),
        "city_id": city_id,
        "city_version": f"v{active_version}",
        "required_city_versions": {city_id: active_version},
        "world_city_versions": world_versions,
        "world_map_snapshot": _world_map_snapshot(world_id, world_versions),
        "task_type": task_type,
        "generation_mode": mode,
        "artifact_snapshot": {
            name: artifact.get("sha256")
            for name, artifact in artifacts.items()
            if artifact.get("status") == "available" and artifact.get("sha256")
        },
    }
    if (context.get("city_id") != city_id or context.get("task_type") != task_type
            or context.get("city_version") != f"v{active_version}"):
        raise ValueError("Task provenance context does not match the active world/city/type binding.")
    provenance_path = batch_path / "world_provenance.json"
    if provenance_path.is_file():
        try:
            provenance = read_json(provenance_path)
        except Exception:
            provenance = {}
    else:
        provenance = {}
    if provenance and provenance.get("world_id") not in {"", world_id}:
        raise ValueError(
            f"Cannot append world {world_id} tasks to batch bound to {provenance.get('world_id')}."
        )
    provenance.update({
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "world_id": world_id,
        "city_id": city_id,
        "city_version": f"v{active_version}",
        "required_city_versions": {city_id: active_version},
        "world_city_versions": world_versions,
        "task_type": task_type,
        "batch_id": batch_path.name,
        "task_count": _task_count(batch_path / "tasks"),
        "city_registry_path": relative_path(city_path),
        "created_at": provenance.get("created_at") or utc_now(),
        "note": "World/city version sidecar; task-level sidecars retain the exact context of every later append.",
    })
    contexts = provenance.setdefault("generation_contexts", [])
    if not isinstance(contexts, list):
        contexts = provenance["generation_contexts"] = []
    generation_id = str(context.get("generation_id") or "")
    if not any(isinstance(item, dict) and item.get("generation_id") == generation_id for item in contexts):
        contexts.append(context)
    write_json(provenance_path, provenance)

    for task_folder in task_folders or []:
        task_path = Path(task_folder)
        if not (task_path / "task.json").is_file():
            continue
        task_provenance = {
            "schema_version": "geoword.task.provenance.v1",
            "world_id": world_id,
            "city_id": city_id,
            "batch_id": batch_path.name,
            "task_type": task_type,
            "generation_context": context,
            "task_json_sha256": sha256_file(task_path / "task.json"),
            "created_at": utc_now(),
        }
        write_json(task_path / "task_provenance.json", task_provenance)
    return provenance


def _world_map_snapshot(world_id: str, city_versions: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Freeze the map artifact identity for every city visible to a task.

    The task's own city is not the Agent's search scope. Place evaluation uses
    this complete snapshot to assemble a reproducible world without exposing
    internal city/version metadata to the Agent.
    """
    snapshot: dict[str, dict[str, Any]] = {}
    for city_id, raw_version in sorted(city_versions.items()):
        try:
            version = int(str(raw_version).lstrip("vV"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid active city version for world snapshot: {city_id}={raw_version!r}") from exc
        _, city_path = _registry_paths(world_id, city_id)
        city_record = read_json(city_path)
        matched = next(
            (item for item in city_record.get("city_versions", []) if item.get("version") == version),
            None,
        )
        artifact = ((matched or {}).get("artifacts") or {}).get("map_world") or {}
        path = str(artifact.get("path") or "")
        sha256 = str(artifact.get("sha256") or "")
        if not path or not sha256:
            raise ValueError(f"World snapshot is missing map artifact metadata for {world_id}/{city_id}/v{version}")
        snapshot[city_id] = {"version": version, "path": path, "sha256": sha256}
    return snapshot


def repair_task_batch_provenance(batch_dir: str | Path) -> dict[str, Any]:
    """Backfill missing task-level provenance from an existing batch binding.

    This is intentionally metadata-only.  It never changes task payloads,
    ground truth, reviews, assets, world versions, or the recorded batch
    generation contexts.
    """
    batch_path = Path(batch_dir)
    provenance_path = batch_path / "world_provenance.json"
    if not provenance_path.is_file():
        raise FileNotFoundError(f"Missing batch world provenance: {provenance_path}")
    provenance = read_json(provenance_path)
    world_id = str(provenance.get("world_id") or "")
    city_id = str(provenance.get("city_id") or "")
    task_type = str(provenance.get("task_type") or "")
    required_versions = provenance.get("required_city_versions") or {}
    if not world_id or not city_id or not task_type or not isinstance(required_versions, dict):
        raise ValueError("Batch provenance lacks a complete world/city/type binding.")

    repaired: list[str] = []
    skipped: list[str] = []
    tasks_root = batch_path / "tasks"
    if not tasks_root.is_dir():
        raise FileNotFoundError(f"Missing batch tasks directory: {tasks_root}")
    for task_path in sorted(path for path in tasks_root.iterdir()
                            if path.is_dir() and (path / "task.json").is_file()):
        task_json = task_path / "task.json"
        sidecar = task_path / "task_provenance.json"
        if sidecar.is_file():
            skipped.append(task_path.name)
            continue
        task = read_json(task_json)
        task_id = str(task.get("task_id") or task_path.name)
        generated_at = str(task.get("generated_at") or provenance.get("created_at") or utc_now())
        context = {
            "generation_id": f"reconstructed::{batch_path.name}::{task_id}",
            "at": generated_at,
            "city_id": city_id,
            "city_version": provenance.get("city_version", ""),
            "required_city_versions": deepcopy(required_versions),
            "world_city_versions": deepcopy(provenance.get("world_city_versions") or {}),
            "task_type": str(task.get("task_type") or task_type),
            "generation_mode": provenance.get("generation_mode", "unknown"),
            "artifact_snapshot": deepcopy(provenance.get("artifact_snapshot") or {}),
            "reconstruction": {
                "status": "reconstructed_from_batch_binding",
                "at": utc_now(),
                "reason": "Task file existed without a task_provenance sidecar.",
            },
        }
        write_json(sidecar, {
            "schema_version": "geoword.task.provenance.v1",
            "world_id": world_id,
            "city_id": city_id,
            "batch_id": batch_path.name,
            "task_type": str(task.get("task_type") or task_type),
            "generation_context": context,
            "task_json_sha256": sha256_file(task_json),
            "created_at": utc_now(),
            "provenance_status": "reconstructed_from_batch_binding",
        })
        repaired.append(task_path.name)

    if repaired:
        provenance.setdefault("provenance_repairs", []).append({
            "operation": "backfill_missing_task_provenance",
            "at": utc_now(),
            "task_count": len(repaired),
            "task_folders": repaired,
            "note": "Added missing task-level sidecars from the existing batch binding; task payloads and source assets were not modified.",
        })
        write_json(provenance_path, provenance)
    return {"batch_dir": str(batch_path), "repaired": repaired, "skipped": skipped}


def migrate_task_provenance_versions(world_id: str) -> dict[str, Any]:
    """Add v1-compatible requirements to existing provenance sidecars only."""
    context = city_version_context(world_id)
    updated: list[str] = []
    skipped: list[str] = []
    for batch_dir in _task_batch_directories():
        provenance_path = batch_dir / "world_provenance.json"
        if not provenance_path.is_file():
            continue
        try:
            provenance = read_json(provenance_path)
        except Exception:
            skipped.append(relative_path(provenance_path))
            continue
        if provenance.get("world_id") != world_id:
            continue
        city_id = str(provenance.get("city_id") or batch_dir.parent.name)
        if city_id not in context["active_city_versions"]:
            skipped.append(relative_path(provenance_path))
            continue
        if provenance.get("required_city_versions") and provenance.get("city_version", "").startswith("v"):
            continue
        # Legacy batches can only be truthfully tied to their imported v1
        # baseline.  Their task/GT/review files are deliberately untouched.
        provenance["schema_version"] = PROVENANCE_SCHEMA_VERSION
        provenance["city_version"] = "v1"
        provenance["required_city_versions"] = {city_id: 1}
        provenance.setdefault("world_city_versions", {city_id: 1})
        provenance["version_migration"] = {
            "at": utc_now(),
            "strategy": "legacy_batch_assigned_to_v1",
            "note": "Only provenance metadata was extended. Task payloads, ground truth, review status, and run outputs were not modified.",
        }
        write_json(provenance_path, provenance)
        updated.append(relative_path(provenance_path))
    return {"world_id": world_id, "updated": updated, "skipped": skipped}
