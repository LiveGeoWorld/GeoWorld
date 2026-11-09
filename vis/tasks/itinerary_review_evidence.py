"""Project version-bound Long-Horizon review evidence."""
from pathlib import Path

from shared.io import read_json
from app.runtime.asset_bindings import _resolve_place_world_snapshot
from vis.tasks.itinerary_comparison import restore_comparisons
from world_module.registry import resolve_world_route_cache


def inject_itinerary_review(folder: Path, sandbox: dict) -> None:
    provenance_path = folder / "task_provenance.json"
    if not provenance_path.is_file():
        return
    provenance = read_json(provenance_path)
    context = provenance.get("generation_context") or {}
    city, world = provenance.get("city_id"), provenance.get("world_id")
    version = (context.get("required_city_versions") or {}).get(city) or context.get("city_version")
    if not (city and world and version):
        return
    try:
        bound = resolve_world_route_cache(world, city, version)
        route_payload = read_json(bound) if bound and bound.is_file() else {}
        _inject_itinerary_review_routes(sandbox, route_payload)
        if any(not contract.get("candidate_comparison") for contract in sandbox.get("requirement_contracts", [])):
            task = read_json(folder / "task.json")
            snapshot = _resolve_place_world_snapshot(None, folder, task)
            world_path = dict(zip(snapshot["city_versions"], snapshot["world_paths"]))[city]
            restore_comparisons(sandbox, read_json(world_path), route_payload)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        sandbox["_itinerary_comparison_warning"] = str(exc)


def _inject_itinerary_review_routes(sb: dict, route_payload: dict) -> None:
    """Project only saved execution edges from their version-bound route cache."""
    if not sb or not route_payload:
        return
    routes = {entry["route_id"]: entry for entry in route_payload.get("cache", []) if entry.get("route_id")}
    wanted = {(e.get("from_place_id"), e.get("to_place_id"), e.get("mode"))
              for d in (sb.get("correct_itinerary") or {}).get("days", [])
              for e in d.get("events", []) if e.get("block_kind") == "transit"}
    result = []
    for rid, entry in routes.items():
        key = (entry.get("origin_place_id"), entry.get("dest_place_id"), entry.get("mode"))
        if key not in wanted or entry.get("waypoint_place_id"):
            continue
        source, seen = entry, {rid}
        while not source.get("route") and source.get("route_ref"):
            ref = source["route_ref"]
            if ref in seen or ref not in routes:
                break
            seen.add(ref)
            source = routes[ref]
        result.append({**entry, "route_id": rid, "route": source.get("route") or {},
                       "from_place_id": key[0], "to_place_id": key[1]})
    sb["_itinerary_review_routes"] = result
