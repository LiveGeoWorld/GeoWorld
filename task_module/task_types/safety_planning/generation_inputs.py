"""Version-bound inputs for the source-first Safety construction pipeline."""

import hashlib
from pathlib import Path

from shared.io import read_json
from world_module.service_detail_snapshot import identity_key, public_identity, validate_service_detail_cache


def resolve_assets(root: Path, world_id: str, city_id: str) -> dict:
    record = read_json(root / "outputs" / "worlds" / world_id / "cities" / f"{city_id}.json")
    version = int(str(record["active_version"]).lstrip("vV"))
    active = next(r for r in record["city_versions"] if int(r["version"]) == version)
    assets = active["artifacts"]
    result = {"world_binding": f"{world_id}:{city_id}:v{version}", "city_version": f"v{version}"}
    for name in ("map_world", "service_detail_cache", "route_cache"):
        path = Path(assets[name]["path"])
        path = path if path.is_absolute() else root / path
        if not path.is_file():
            raise ValueError(f"Safety source missing for {city_id} v{version}: {name}")
        result[name] = str(path.resolve())
    result["map_world_sha256"] = assets["map_world"]["sha256"]
    if hashlib.sha256(Path(result["map_world"]).read_bytes()).hexdigest() != result["map_world_sha256"]:
        raise ValueError("Safety map asset differs from the registered city-version hash")
    return result


def service_documents(world: dict, assets: dict) -> dict:
    cache = read_json(Path(assets["service_detail_cache"]))
    errors = validate_service_detail_cache(cache, world, map_world_sha256=assets["map_world_sha256"])
    if errors:
        raise ValueError(f"Safety service-detail source binding: {errors}")
    records = {identity_key(public_identity(r)): r for r in cache["records"]}
    return {p["place_id"]: records[identity_key(public_identity(p))].get("source_documents", []) for p in world["places"]}


def time_sources(*, benchmark_root: Path, world_id: str, city_id: str, assets: dict) -> dict:
    anchors = []
    for task_type in ("region_aware", "itinerary", "safety_planning", "multi_entity", "place_grounding", "place_discovery", "route_planning"):
        base = benchmark_root / task_type / world_id / city_id
        for task_path in sorted(base.glob("*/tasks/*/task.json")):
            provenance_path = task_path.parent / "task_provenance.json"
            if not provenance_path.is_file():
                continue
            provenance = read_json(provenance_path)
            version = provenance.get("city_version") or provenance.get("generation_context", {}).get("city_version")
            if provenance.get("world_id") != world_id or version != assets["city_version"]:
                continue
            task = read_json(task_path)
            if task.get("time_context", {}).get("instruction_time"):
                anchors.append({"source_ref": str(task_path.resolve()), "task_type": task_type,
                                "time_context": task["time_context"]})
    return {"task_anchors": anchors}
