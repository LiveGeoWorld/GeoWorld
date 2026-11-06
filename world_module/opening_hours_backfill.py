"""Explicit, journaled same-version completion of registered historical POIs."""

import copy
import os
from datetime import datetime, timezone
from pathlib import Path

from shared.io import read_json, write_json
from shared.opening_hours import FIELDS
from world_module.opening_hours_completion import complete_world_hours
from world_module.service_detail_snapshot import build_local_service_world_state, sha256_file


def verify_supplement_only(original, completed):
    stripped = copy.deepcopy(completed)
    if len(original.get("places", [])) != len(stripped.get("places", [])):
        raise ValueError("Opening-hours completion changed the POI set")
    for before, after in zip(original.get("places", []), stripped.get("places", [])):
        old = (before.get("map_projection") or {}).get("google_place_details") or {}
        details = (after.get("map_projection") or {}).get("google_place_details") or {}
        for field in FIELDS[1:]:
            if field not in old:
                details.pop(field, None)
    if stripped != original:
        raise ValueError("Opening-hours completion changed existing world facts")


def refresh_live_references(value, *, replacements, city_id, version, artifacts):
    """Update explicit shared paths, never hash-match arbitrary frozen copies."""
    if isinstance(value, list):
        return [refresh_live_references(v, replacements=replacements, city_id=city_id,
                                       version=version, artifacts=artifacts) for v in value]
    if not isinstance(value, dict):
        return value
    result = {k: refresh_live_references(v, replacements=replacements, city_id=city_id,
                                       version=version, artifacts=artifacts)
              for k, v in value.items()}
    path = str(result.get("path", "")).replace("\\", "/")
    replacement = replacements.get(path)
    if replacement and result.get("sha256") == replacement["before"]:
        result["sha256"] = replacement["after"]
        if "bytes" in result:
            result["bytes"] = replacement["bytes"]
    # Artifact snapshots have no paths, but are explicitly city/version-bound.
    if (result.get("city_id") == city_id
            and str(result.get("city_version", "")).lstrip("v") == str(version)):
        snapshot = result.get("artifact_snapshot")
        if isinstance(snapshot, dict):
            for name, ref in artifacts.items():
                change = replacements.get(str(ref.get("path", "")).replace("\\", "/"))
                if change and snapshot.get(name) == change["before"]:
                    snapshot[name] = change["after"]
    return result


def apply_journal(root, operation):
    journal = read_json(operation / "journal.json")
    if journal["status"] == "completed":
        return journal
    # Accept only original bytes or this operation's own previously installed bytes.
    for row in journal["files"]:
        path = root / row["path"]
        actual = sha256_file(path) if path.exists() else None
        if actual not in {row["before"], row["after"]}:
            raise ValueError(f"Concurrent change blocks hours completion: {row['path']}")
    for row in journal["files"]:
        write_json(root / row["path"], read_json(operation / row["staged"]))
    for row in journal["files"]:
        if sha256_file(root / row["path"]) != row["after"]:
            raise ValueError(f"Post-write hash mismatch: {row['path']}")
    journal["status"] = "completed"
    journal["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(operation / "journal.json", journal)
    return journal


def backfill_city(root, world_id, city_id, operation_root, *, endpoint_name,
                  config_path="config.yaml", progress_callback=None):
    root, operation_root = Path(root).resolve(), Path(operation_root).resolve()
    if os.name == "nt" and not str(root).startswith("\\\\?\\"):
        root = Path("\\\\?\\" + str(root))
    operation = operation_root / city_id
    operation.mkdir(parents=True, exist_ok=True)
    if (operation / "journal.json").exists():
        return apply_journal(root, operation)
    registry_path = root / "outputs" / "worlds" / world_id / "cities" / f"{city_id}.json"
    record = read_json(registry_path)
    record_hash = sha256_file(registry_path)
    version = record["active_version"]
    artifacts = copy.deepcopy(record["artifacts"])
    world_path = root / artifacts["map_world"]["path"]
    original = read_json(world_path)
    original_hash = sha256_file(world_path)
    backup = operation / "source_world.json"
    if not backup.exists():
        write_json(backup, original)
    elif read_json(backup) != original:
        raise ValueError("World changed since this backfill started")
    cache_path = world_path.parent / "service_detail_cache.json"
    completed, cache = complete_world_hours(
        original, output_dir=operation, config_path=config_path, endpoint_name=endpoint_name,
        website_cache_paths=[cache_path], progress_callback=progress_callback)
    verify_supplement_only(original, completed)
    if sha256_file(world_path) != original_hash or sha256_file(registry_path) != record_hash:
        raise ValueError("City changed while opening hours were being completed")
    files, replacements = [], {}

    def stage(path, payload):
        number = len(files)
        relative = path.relative_to(root).as_posix()
        before = sha256_file(path) if path.exists() else None
        if path.exists():
            write_json(operation / "backups" / f"{number}.json", read_json(path))
        staged = f"staged/{number}.json"
        write_json(operation / staged, payload)
        after = sha256_file(operation / staged)
        files.append({"path": relative, "before": before, "after": after, "staged": staged})
        replacements[relative] = {"before": before, "after": after,
                                  "bytes": (operation / staged).stat().st_size}
        return {"status": "available", "path": relative, "sha256": after,
                "bytes": replacements[relative]["bytes"],
                "modified_at": datetime.now(timezone.utc).isoformat()}

    map_ref = stage(world_path, completed)
    artifacts["map_world"] = {**artifacts["map_world"], **map_ref}
    cache["world_binding"]["map_world_sha256"] = map_ref["sha256"]
    state = build_local_service_world_state(cache, completed)
    artifacts["service_detail_cache"] = stage(cache_path, cache)
    artifacts["local_service_world_state"] = stage(world_path.parent / "local_service_world_state.json", state)
    correction = {"operation": "complete_historical_opening_hours", "at": datetime.now(timezone.utc).isoformat(),
                  "city_id": city_id, "city_version": version, "before_map_sha256": original_hash,
                  "after_map_sha256": map_ref["sha256"], "audit_path": str(operation)}
    record["artifacts"] = artifacts
    for item in record["city_versions"]:
        if item["version"] == version:
            item["artifacts"] = copy.deepcopy(artifacts)
    record.setdefault("derived_data_corrections", []).append(correction)
    stage(registry_path, record)
    manifest_path = registry_path.parent.parent / "world_manifest.json"
    manifest = read_json(manifest_path)
    manifest.setdefault("history", []).append(correction)
    stage(manifest_path, manifest)
    benchmarks = root / "outputs" / "benchmarks"
    provenance_paths = list(benchmarks.rglob("world_provenance.json")) + list(benchmarks.rglob("task_provenance.json"))
    for path in provenance_paths:
        payload = read_json(path)
        if payload.get("world_id") != world_id:
            continue
        updated = refresh_live_references(payload, replacements=replacements,
            city_id=city_id, version=version, artifacts=artifacts)
        if updated != payload:
            updated.setdefault("derived_data_corrections", []).append(correction)
            stage(path, updated)
    write_json(operation / "journal.json", {"status": "prepared", "files": files, **correction})
    return apply_journal(root, operation)
