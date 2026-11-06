"""Derive stable, non-dynamic service categories from factual Google POI types."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from shared.io import read_json
from shared.place_type_catalog import categories_for_google_place_types, category_for_google_types


_GENERIC_TYPES = {"", "point_of_interest", "establishment"}
FACTUAL_SERVICE_INDEX_SCHEMA = "geoword.factual_service_index.v3"
FACTUAL_SERVICE_INDEX_ARTIFACT_SCHEMA = "geoword.factual_service_index_artifact.v2"


def factual_service_category(place: dict[str, Any]) -> str:
    """Return the catalog category for a factual Google POI.

    ``other`` is deliberate: the POI remains available for factual display and
    audit, but cannot silently enter a category-specific task candidate pool.
    """
    primary = category_for_google_types([place.get("primary_type", "")])
    return primary if primary != "other" else category_for_google_types(_place_types(place))


def factual_service_categories(place: dict[str, Any]) -> list[str]:
    """Return the primary category followed by every other factual capability."""
    primary = factual_service_category(place)
    categories = categories_for_google_place_types(_place_types(place))
    ordered = ([primary] if primary != "other" else []) + categories
    return list(dict.fromkeys(ordered)) or ["other"]


def build_factual_service_index(
    places: list[dict[str, Any]], existing_services: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build factual service handles while retaining historical referenced IDs."""
    previous_by_place: dict[str, list[dict[str, Any]]] = {}
    for service in existing_services or []:
        place_id = str(service.get("place_id") or "")
        service_id = str(service.get("service_id") or "")
        if place_id and service_id:
            previous_by_place.setdefault(place_id, []).append(service)
    services: list[dict[str, Any]] = []
    for place in places:
        place_id = str(place.get("place_id") or "")
        category = factual_service_category(place)
        categories = factual_service_categories(place)
        historical = previous_by_place.get(place_id, [])
        if not place_id:
            continue
        # Old information/task records can reference historical service IDs.
        # Keep all such IDs rather than breaking frozen versions during a
        # derived-index migration; newly built worlds use one deterministic ID.
        sources = historical or [{"service_id": f"service_{place_id}"}]
        for source in sources:
            service_category = category or _legacy_category(source.get("service_type", "")) or "other"
            services.append({
            "service_id": str(source["service_id"]),
            "place_id": place_id,
            "service_type": service_category,
            "primary_service_type": service_category,
            "service_types": categories,
            "affordances": [],
            "state_model": {},
            "constraints": [],
            "oracle_affordances": [],
            "app_projection": {
                "kind": "factual_place_capability",
                "category": service_category,
                "categories": categories,
                "google_primary_type": place.get("primary_type", ""),
                "google_types": sorted(_place_types(place) - _GENERIC_TYPES),
            },
            "event_hooks": [],
            "control_tags": {"real_data": ["google_place_type_projection"]},
            "director_notes": {
                "state_policy": "No availability, reservation, queue, crowding, inventory, or other dynamic state is inferred from place types.",
            },
            "provenance": {
                "source": ["google_maps"],
                "generator": "factual_service_index.v3",
                "quality_flags": [],
            },
            })
    return services


def materialize_factual_services(
    world: dict[str, Any], existing_services: list[dict[str, Any]] | None = None,
    *, indexed_services: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a copy of ``world`` with a factual service index.

    A persisted index is accepted only after its source-place signature was
    checked by :func:`read_factual_service_index`.  The fallback is fully
    deterministic, so older city artifacts remain readable.
    """
    updated = dict(world)
    updated["services"] = list(indexed_services) if indexed_services is not None else build_factual_service_index(
        list(world.get("places") or []),
        existing_services if existing_services is not None else list(world.get("services") or []),
    )
    metadata = dict(updated.get("generation_metadata") or {})
    metadata["factual_service_index"] = {
        "schema_version": FACTUAL_SERVICE_INDEX_SCHEMA,
        "service_count": len(updated["services"]),
        "state_policy": "factual_categories_only",
    }
    updated["generation_metadata"] = metadata
    return updated


def build_factual_service_index_artifact(world: dict[str, Any]) -> dict[str, Any]:
    """Build a version-local, non-dynamic service-index sidecar.

    The artifact is derived solely from the frozen POI type set.  It is not a
    world entity revision and therefore must never cause a city version bump.
    """
    city = (world.get("cities") or [{}])[0]
    places = list(world.get("places") or [])
    services = build_factual_service_index(places, list(world.get("services") or []))
    return {
        "schema_version": FACTUAL_SERVICE_INDEX_ARTIFACT_SCHEMA,
        "index_schema_version": FACTUAL_SERVICE_INDEX_SCHEMA,
        "city_id": str(city.get("city_id") or ""),
        "source_place_count": len(places),
        "source_place_signature": factual_service_place_signature(places),
        "service_count": len(services),
        "state_policy": "factual_categories_only",
        "services": services,
    }


def factual_service_place_signature(places: list[dict[str, Any]]) -> str:
    """Hash the exact POI type inputs that determine a service index."""
    source = [
        {
            "place_id": str(place.get("place_id") or ""),
            "primary_type": str(place.get("primary_type") or ""),
            "types": sorted(_place_types(place)),
        }
        for place in places
    ]
    canonical = json.dumps(sorted(source, key=lambda item: item["place_id"]), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_factual_service_index(path: str | Path, world: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Read a sidecar only when it exactly matches this world's POI types."""
    index_path = Path(path)
    if not index_path.is_file():
        return None
    payload = read_json(index_path)
    places = list(world.get("places") or [])
    city = (world.get("cities") or [{}])[0]
    if payload.get("schema_version") != FACTUAL_SERVICE_INDEX_ARTIFACT_SCHEMA:
        return None
    if payload.get("city_id") != str(city.get("city_id") or ""):
        return None
    if int(payload.get("source_place_count") or -1) != len(places):
        return None
    if payload.get("source_place_signature") != factual_service_place_signature(places):
        return None
    services = payload.get("services")
    if not isinstance(services, list):
        return None
    # A fresh deterministic reconstruction checks that the sidecar has not
    # been edited into a different category projection.
    expected = build_factual_service_index(places, list(world.get("services") or []))
    expected_rows = {
        (item.get("service_id"), item.get("place_id"), item.get("service_type"),
         tuple(item.get("service_types") or []))
        for item in expected
    }
    actual_rows = {
        (item.get("service_id"), item.get("place_id"), item.get("service_type"),
         tuple(item.get("service_types") or []))
        for item in services
    }
    return services if actual_rows == expected_rows else None


def _place_types(place: dict[str, Any]) -> set[str]:
    projection = place.get("map_projection") or {}
    raw = [place.get("primary_type", ""), *(projection.get("types") or [])]
    return {str(value).strip().lower() for value in raw if str(value).strip()}


def _legacy_category(value: Any) -> str:
    return {
        "restaurant_reservation": "restaurant",
        "cafe_reservation": "cafe",
        "hotel_booking": "lodging",
        "attraction_visit": "tourist_attraction",
        "outdoor_visit": "park",
        "retail_visit": "shopping",
        "healthcare_visit": "medical_care",
        "local_visit": "other",
    }.get(str(value or ""), "")
