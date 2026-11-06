"""Quality checks specific to real-data v7 world generation."""

from __future__ import annotations

from typing import Any

from shared.validator import validate_world


def _is_task_only_information_warning(warning: str) -> bool:
    """Keep dynamic-task tuning signals out of map/world readiness warnings."""
    return warning == "too much information is visible at start"


QUALITY_PROFILES = {"initial_city", "expansion_batch", "poi_supplement"}


def check_worldgen_quality(
    world: dict[str, Any],
    *,
    profile: str,
    expected_districts: int | None,
    expected_corridors: int | None,
    expected_places: int,
) -> dict[str, Any]:
    """Validate one explicit build scope without inventing city-wide floors.

    Entity-level factual and referential checks below apply to every profile.
    Only the count contract varies: an expansion is a delta, while a POI
    supplement deliberately has no district/corridor count contract.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if profile not in QUALITY_PROFILES:
        raise ValueError(f"Unknown map quality profile: {profile}")

    districts = world.get("districts", [])
    corridors = world.get("corridors", [])
    places = world.get("places", [])

    expected_counts = {
        "districts": expected_districts,
        "corridors": expected_corridors,
        "places": expected_places,
    }
    actual_counts = {
        "districts": len(districts),
        "corridors": len(corridors),
        "places": len(places),
    }
    for collection, expected in expected_counts.items():
        if expected is not None and actual_counts[collection] != expected:
            errors.append(
                f"{profile} produced {actual_counts[collection]} {collection}; expected exactly {expected}"
            )

    for district in districts:
        prov = district.get("provenance", {})
        if "weak_google_resolution" in prov.get("quality_flags", []):
            warnings.append(f"District {district.get('district_id')} has weak Google resolution")
    transport_corridor_count = 0
    transport_verified_count = 0
    for corridor in corridors:
        cid = corridor.get("corridor_id")
        points = corridor.get("geometry", {}).get("data", {}).get("points", [])
        if len(points) < 2:
            warnings.append(f"Corridor {cid} has fewer than two geocoded nodes")
        mobility = corridor.get("base_mobility_model", {})
        route_polyline = corridor.get("route_polyline") or mobility.get("route_polyline", {})
        route_geometry = corridor.get("route_geometry") or mobility.get("route_geometry", {})
        corridor_class = corridor.get("corridor_class") or mobility.get("semantic_corridor", {}).get("corridor_class", "transport_corridor")
        served_evidence = corridor.get("served_district_evidence") or mobility.get("served_district_evidence", [])
        served_ids = corridor.get("served_district_ids") or mobility.get("served_district_ids", [])

        if corridor_class == "transport_corridor":
            transport_corridor_count += 1
            if route_geometry.get("is_route_verified"):
                transport_verified_count += 1
            else:
                warnings.append(
                    f"Corridor {cid} is a transport_corridor but route_geometry is not verified by Routes API"
                )
            if not served_ids or len(served_ids) < 2:
                warnings.append(
                    f"Corridor {cid} (transport_corridor) connects fewer than 2 districts"
                )
        else:
            if not served_ids:
                warnings.append(f"Corridor {cid} (service_axis) has no served_district_ids")

        verified_count = sum(1 for ev in served_evidence if ev.get("verified"))
        claimed_count = sum(1 for ev in served_evidence if ev.get("name_claimed"))
        if claimed_count and verified_count == 0:
            warnings.append(
                f"Corridor {cid} has LLM-claimed served_areas but none verified by route geometry"
            )

        if not route_polyline:
            warnings.append(f"Corridor {cid} missing route_polyline metadata")
        elif route_polyline.get("is_full_routing_graph"):
            warnings.append(f"Corridor {cid} unexpectedly claims to be a full routing graph")
        if not (corridor.get("route_runtime_ref") or mobility.get("route_runtime_ref")):
            warnings.append(f"Corridor {cid} missing route_runtime_ref")
        if points and mobility.get("route_distance_m", 0) == 0 and mobility.get("route_duration_s", 0) == 0:
            warnings.append(f"Corridor {cid} has geocoded nodes but no route summary")

    if transport_corridor_count:
        verified_ratio = transport_verified_count / transport_corridor_count
        if verified_ratio < 0.5:
            warnings.append(
                f"Only {transport_verified_count}/{transport_corridor_count} transport corridors have verified Routes API geometry "
                f"({verified_ratio:.2f}); Bench routing tasks should treat unverified ones as semantic only."
            )
    for place in places:
        projection = place.get("map_projection", {})
        if not (projection.get("google_place_id") or projection.get("source_place_id") or projection.get("osm_id")):
            errors.append(f"Place {place.get('place_id')} missing source place id")
        loc = place.get("location", {})
        if not loc.get("lat") or not loc.get("lng"):
            errors.append(f"Place {place.get('place_id')} missing coordinates")

    # Candidate flags and dropped counts are retained in generation_metadata
    # for audit. They are not final-world warnings and must not reach the UI.

    validation = validate_world(world).to_dict()
    errors.extend(validation.get("errors", []))
    warnings.extend(validation.get("warnings", []))

    live_overlay = world.get("generation_metadata", {}).get("live_overlay", {})
    information_quality = live_overlay.get("information_quality", {})
    for error in information_quality.get("errors", []):
        errors.append(f"Information quality: {error}")
    for warning in information_quality.get("warnings", []):
        if not _is_task_only_information_warning(warning):
            warnings.append(f"Information quality: {warning}")
    review_place_ids = {
        item.get("about", {}).get("place_id")
        for item in world.get("information_items", [])
        if item.get("content_type") == "review" and item.get("about", {}).get("place_id")
    }
    google_detail_places = [
        place for place in places
        if place.get("map_projection", {}).get("google_place_details", {}).get("details_enriched")
    ]
    if google_detail_places and len(review_place_ids) / max(1, len(google_detail_places)) < 0.25:
        warnings.append(
            "Real review coverage is low for Google-details-enriched places: "
            f"{len(review_place_ids)}/{len(google_detail_places)}"
        )
    detail_status_counts: dict[str, int] = {}
    expected_google_details = 0
    for place in places:
        projection = place.get("map_projection", {})
        if projection.get("google_place_id"):
            expected_google_details += 1
        status = (projection.get("google_place_details", {}) or {}).get("details_status")
        if status:
            detail_status_counts[status] = detail_status_counts.get(status, 0) + 1
    semantic_quality = live_overlay.get("semantic_shard_quality", {})
    for error in semantic_quality.get("errors", []):
        warnings.append(f"Semantic shard warning: {error}")

    return {
        "ok": not errors,
        "profile": profile,
        "expected_counts": expected_counts,
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "districts": len(districts),
            "corridors": len(corridors),
            "places": len(places),
            "services": len(world.get("services", [])),
            "information_items": len(world.get("information_items", [])),
            "event_templates": len(world.get("event_templates", [])),
            "transport_corridors": transport_corridor_count if corridors else 0,
            "verified_transport_corridors": transport_verified_count if corridors else 0,
            "google_place_details_expected": expected_google_details,
            "google_place_details_status": detail_status_counts,
        },
    }
