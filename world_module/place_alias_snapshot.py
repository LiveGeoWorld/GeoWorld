"""Version-bound Google place-name aliases for frozen map worlds."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from shared.io import read_json, write_json
from shared.maps_client import GoogleMapsClient


SCHEMA_VERSION = "geoword.place_alias_cache.v1"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def google_place_id(place: dict[str, Any]) -> str:
    projection = place.get("map_projection") or {}
    return str(
        projection.get("google_place_id")
        or projection.get("source_place_id")
        or place.get("google_place_id")
        or place.get("source_place_id")
        or ""
    )


def english_display_name(payload: dict[str, Any]) -> str:
    value = payload.get("displayName") or {}
    if isinstance(value, dict):
        return str(value.get("text") or "").strip()
    return str(value or "").strip()


def write_place_alias_snapshot(
    world_path: str | Path,
    *,
    client: GoogleMapsClient | None = None,
    resume: bool = True,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Fetch English display names for the exact Google IDs in one world."""
    world_path = Path(world_path)
    world = read_json(world_path)
    output_path = world_path.parent / "place_alias_cache.json"
    existing: dict[str, dict[str, Any]] = {}
    if resume and output_path.is_file():
        prior = read_json(output_path)
        if (prior.get("world_binding") or {}).get("map_world_sha256") == sha256_file(world_path):
            existing = {
                str(item.get("place_id") or ""): item
                for item in prior.get("aliases", []) if isinstance(item, dict)
            }

    maps = client or GoogleMapsClient()
    aliases: list[dict[str, Any]] = []
    places = [item for item in world.get("places", []) if isinstance(item, dict)]
    for index, place in enumerate(places, start=1):
        place_id = str(place.get("place_id") or "")
        source_id = google_place_id(place)
        cached = existing.get(place_id)
        if cached and cached.get("google_place_id") == source_id:
            entry = cached
        elif not place_id or not source_id:
            entry = {
                "place_id": place_id,
                "google_place_id": source_id,
                "source_name": str(place.get("name") or ""),
                "language_code": "en",
                "status": "source_id_missing",
                "english_display_name": "",
            }
        else:
            try:
                details = maps.place_details(source_id, fields=["id", "displayName"], language="en")
                display_name = english_display_name(details)
                entry = {
                    "place_id": place_id,
                    "google_place_id": source_id,
                    "source_name": str(place.get("name") or ""),
                    "language_code": "en",
                    "status": "available" if display_name else "display_name_missing",
                    "english_display_name": display_name,
                    "provider_resource_id": str(details.get("id") or source_id),
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as exc:
                entry = {
                    "place_id": place_id,
                    "google_place_id": source_id,
                    "source_name": str(place.get("name") or ""),
                    "language_code": "en",
                    "status": "request_failed",
                    "english_display_name": "",
                    "error_class": type(exc).__name__,
                }
        aliases.append(entry)
        payload = _payload(world, world_path, aliases, len(places), maps)
        write_json(output_path, payload)
        if progress:
            progress({"completed": index, "total": len(places), "entry": entry})
    return {"path": str(output_path), "payload": _payload(world, world_path, aliases, len(places), maps)}


def verify_query_aliases(
    world_path: str | Path,
    queries: list[str],
    *,
    client: GoogleMapsClient | None = None,
) -> dict[str, Any]:
    """Freeze query aliases only when Google resolves them to an existing POI ID."""
    world_path = Path(world_path)
    world = read_json(world_path)
    output_path = world_path.parent / "place_alias_cache.json"
    world_hash = sha256_file(world_path)
    payload = read_json(output_path) if output_path.is_file() else _payload(
        world, world_path, [], len(world.get("places", [])), client or GoogleMapsClient()
    )
    if (payload.get("world_binding") or {}).get("map_world_sha256") != world_hash:
        raise ValueError("Existing place alias cache is bound to a different map world")

    maps = client or GoogleMapsClient()
    by_google_id = {
        google_place_id(place): place
        for place in world.get("places", [])
        if isinstance(place, dict) and google_place_id(place)
    }
    city = (world.get("cities") or [{}])[0]
    center = city.get("center") or city.get("location") or {}
    existing = {
        (str(item.get("query") or "").strip().casefold(), str(item.get("google_place_id") or "")): item
        for item in payload.get("verified_query_aliases", []) if isinstance(item, dict)
    }
    records = list(existing.values())
    for query in dict.fromkeys(str(value).strip() for value in queries if str(value or "").strip()):
        results = maps.text_search(
            query,
            lat=center.get("lat"),
            lng=center.get("lng"),
            radius_m=50000 if center else None,
            max_results=3,
            language="en",
            field_mask="places.id,places.displayName,places.formattedAddress",
        )
        first = results[0] if results else {}
        source_id = str(first.get("id") or "")
        place = by_google_id.get(source_id)
        record = {
            "query": query,
            "status": "available" if place else "not_in_frozen_world",
            "place_id": str((place or {}).get("place_id") or ""),
            "google_place_id": source_id,
            "resolved_display_name": english_display_name(first),
            "resolved_address": str(first.get("formattedAddress") or ""),
            "provider": "google_places_new",
            "operation": "text_search",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
        }
        key = (query.casefold(), source_id)
        if key in existing:
            records[records.index(existing[key])] = record
        else:
            records.append(record)
    payload["verified_query_aliases"] = records
    payload["telemetry"] = maps.telemetry_snapshot()
    write_json(output_path, payload)
    return {"path": str(output_path), "payload": payload}


def _payload(
    world: dict[str, Any],
    world_path: Path,
    aliases: list[dict[str, Any]],
    total: int,
    client: GoogleMapsClient,
) -> dict[str, Any]:
    city = (world.get("cities") or [{}])[0]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "world_binding": {
            "city_id": str(city.get("city_id") or ""),
            "map_world_path": str(world_path),
            "map_world_sha256": sha256_file(world_path),
        },
        "source": {
            "provider": "google_places_new",
            "operation": "place_details",
            "field": "displayName",
            "language_code": "en",
        },
        "place_count": total,
        "available_count": sum(item.get("status") == "available" for item in aliases),
        "aliases": aliases,
        "telemetry": client.telemetry_snapshot(),
    }
