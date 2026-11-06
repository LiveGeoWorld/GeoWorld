"""Build v7 world objects from enriched real city data."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from math import asin, cos, radians, sin, sqrt
from typing import Any

from shared.io import to_plain
from shared.schema import (
    City,
    Corridor,
    District,
    Geometry,
    Place,
    Provenance,
    Relation,
)
from world_module.map._internal.live_overlay_builder import add_live_overlay
from world_module.map._internal.factual_service_index import build_factual_service_index
from world_module.map.seed import CitySeed


MAX_DISTRICT_ASSIGNMENT_M = 3500
MAX_NEAR_CORRIDOR_M = 1800


def build_world(
    seed: CitySeed,
    enriched: dict[str, Any],
    *,
    model: str = "gpt-5.5",
    endpoint_name: str = "cliproxy_gpt55_pro",
    config_path: str | None = None,
) -> dict[str, Any]:
    built_at = datetime.now(timezone.utc).isoformat()
    geo_provider = enriched.get("geo_provider", "google")
    provenance = Provenance(
        source=["yunwu_web_search", f"{geo_provider}_maps" if geo_provider != "osm" else "openstreetmap"],
        built_at=built_at,
        generator="pipeline_v2.map_module",
    )
    city = City(
        city_id=seed.city_id,
        name=seed.city_name,
        timezone=seed.timezone,
        geometry=Geometry(kind="point", data={"lat": seed.lat, "lng": seed.lng}),
        supported_apps=[
            "UserInterfaceApp",
            "MapApp",
            "LocalServiceApp",
            "ReviewSocialApp",
            "WeatherCityEventApp",
        ],
        provenance=provenance,
    )
    districts = [_build_district(seed, item, built_at) for item in enriched.get("districts", [])]
    district_by_name = {d.name: d for d in districts}

    corridors = [_build_corridor(seed, item, district_by_name, built_at) for item in enriched.get("corridors", [])]
    places = [_build_place(seed, item, districts, corridors, built_at) for item in enriched.get("places", [])]
    # A base world exposes factual POI categories for search and task sampling.
    # Runtime availability or merchant state is never inferred at this stage.
    services = build_factual_service_index(to_plain(places))

    world = to_plain(
        {
            "schema_version": "v7.world.v1",
            "cities": [city],
            "districts": districts,
            "corridors": corridors,
            "places": places,
            "services": services,
            "information_items": [],
            "event_templates": [],
            "provenance": provenance,
            "generation_metadata": {
                "seed": seed.__dict__,
                "city_summary": enriched.get("discovery", {}).get("city_summary", {}),
                "discovery_policy": enriched.get("discovery", {}).get("discovery_policy", {}),
                "source_notes": enriched.get("discovery", {}).get("source_notes", []),
                "place_quality": enriched.get("place_quality", {}),
            },
        }
    )
    return add_live_overlay(
        world, seed, enriched.get("discovery", {}), model=model,
        endpoint_name=endpoint_name, config_path=config_path,
    )


def _build_district(seed: CitySeed, item: dict[str, Any], built_at: str) -> District:
    did = f"district_{seed.city_id}_{slug(item.get('name', 'area'))}"
    counts = item.get("poi_type_counts", {})
    role_tags = sorted(set(item.get("role_hints", []) + _infer_role_tags(counts)))
    return District(
        district_id=did,
        city_id=seed.city_id,
        name=item.get("name", did),
        geometry=Geometry(kind="point", data=item.get("center", seed.center)),
        role_tags=role_tags,
        dominant_flows=_infer_flows(role_tags),
        risk_profile={"evidence_risk": _evidence_risk(item.get("evidence", {}))},
        control_tags={
            "sampler_axes": role_tags,
            "real_data": ["google_geocoded", "web_evidenced"],
        },
        director_notes={
            "keywords": item.get("keywords", []),
            "anchor_places": item.get("anchor_places", []),
            "evidence_summary": item.get("evidence_summary", ""),
        },
        provenance=_prov(item, built_at),
    )


def _build_corridor(
    seed: CitySeed,
    item: dict[str, Any],
    district_by_name: dict[str, District],
    built_at: str,
) -> Corridor:
    cid = f"corridor_{seed.city_id}_{slug(item.get('name', 'axis'))}"
    nodes = item.get("nodes", [])
    geometry_points = [node["location"] for node in nodes]
    geometry = Geometry(
        kind="polyline",
        data={"points": geometry_points},
    )

    corridor_class = (item.get("corridor_class") or "transport_corridor").strip()
    mode = (item.get("mode") or _default_mode_for_type(item.get("corridor_type", ""))).strip()
    corridor_type = item.get("corridor_type", "urban_corridor")

    enriched_route = item.get("route_geometry") or {}
    visual_polyline = enriched_route.get("polyline_points") or geometry_points
    is_route_verified = bool(enriched_route.get("is_route_verified"))

    # ── Geometric verification of served districts ──────────────────────────
    llm_areas = item.get("served_areas", [])
    served_evidence: list[dict[str, Any]] = []
    served_district_ids: list[str] = []
    seen_district_ids: set[str] = set()

    polyline_for_check = visual_polyline if visual_polyline else geometry_points
    for area in llm_areas:
        district = district_by_name.get(area)
        if not district:
            served_evidence.append({
                "name_claimed": area,
                "district_id": None,
                "match_method": "llm_only",
                "min_distance_m": None,
                "verified": False,
            })
            continue
        center = district.geometry.data
        min_dist = (
            min(_distance_m(center, p) for p in polyline_for_check)
            if polyline_for_check
            else float("inf")
        )
        verified = min_dist <= MAX_NEAR_CORRIDOR_M
        served_evidence.append({
            "name_claimed": area,
            "district_id": district.district_id,
            "match_method": "llm_route_geometry" if visual_polyline else "llm_only",
            "min_distance_m": int(min_dist) if min_dist != float("inf") else None,
            "verified": verified,
        })
        if district.district_id not in seen_district_ids:
            seen_district_ids.add(district.district_id)
            served_district_ids.append(district.district_id)

    # If LLM did not name the served district but the geometry passes through
    # one, attach it as a geometry-only match.
    for district in district_by_name.values():
        if district.district_id in seen_district_ids:
            continue
        center = district.geometry.data
        if not polyline_for_check:
            continue
        min_dist = min(_distance_m(center, p) for p in polyline_for_check)
        if min_dist <= MAX_NEAR_CORRIDOR_M:
            served_evidence.append({
                "name_claimed": None,
                "district_id": district.district_id,
                "match_method": "geometry_only",
                "min_distance_m": int(min_dist),
                "verified": True,
            })
            seen_district_ids.add(district.district_id)
            served_district_ids.append(district.district_id)

    relations = [
        Relation("district", did, "serves", weight=0.9 if any(
            ev["district_id"] == did and ev["verified"] for ev in served_evidence
        ) else 0.6)
        for did in served_district_ids
    ]

    # ── Compose route_polyline (legacy visualization slot) and route_geometry ──
    semantic_corridor = {
        "name": item.get("name", cid),
        "corridor_type": corridor_type,
        "corridor_class": corridor_class,
        "mode": mode,
        "role_hints": item.get("role_hints", []),
        "service_bands": item.get("corridor_service_bands", []),
        "evidence_summary": item.get("evidence_summary", ""),
        "is_semantic_corridor": corridor_class == "service_axis",
    }
    route_polyline = {
        "kind": "verified_route_polyline" if is_route_verified else "geocoded_node_polyline",
        "points": visual_polyline,
        "node_names": [node.get("name", "") for node in nodes],
        "provider": enriched_route.get("provider") or item.get("evidence", {}).get("geo_provider", "google"),
        "travel_mode": enriched_route.get("travel_mode", mode),
        "is_route_verified": is_route_verified,
        "is_full_routing_graph": False,
        "note": (
            "Polyline returned by Google Routes API for adjacent corridor nodes."
            if is_route_verified
            else "Polyline straight-connects geocoded corridor nodes; routing not verified."
        ),
    }
    route_geometry = {
        "provider": enriched_route.get("provider") or "geocoded_node_polyline",
        "travel_mode": enriched_route.get("travel_mode", mode),
        "is_route_verified": is_route_verified,
        "polyline_points": visual_polyline,
        "segment_count": enriched_route.get("segment_count", max(0, len(nodes) - 1)),
        "verified_segment_count": enriched_route.get("verified_segment_count", 0),
        "total_distance_m": enriched_route.get("total_distance_m") or item.get("route_summary", {}).get("distance_m", 0),
        "total_duration_s": enriched_route.get("total_duration_s") or item.get("route_summary", {}).get("duration_s", 0),
        "segments": enriched_route.get("segments", []),
    }
    route_runtime_ref = {
        "status": "verified" if is_route_verified else "not_materialized",
        "recommended_provider": "google_routes_or_osrm",
        "origin_node_id": slug(nodes[0]["name"]) if nodes else "",
        "destination_node_id": slug(nodes[-1]["name"]) if nodes else "",
        "note": (
            "Route geometry verified via Google Routes API; downstream runtime can still compute Place-to-Place ETAs."
            if is_route_verified
            else "Route geometry not verified; downstream route runtime should compute exact paths, ETAs, and transfers."
        ),
    }
    impact_model = _build_impact_model(item, corridor_class, mode, route_geometry)
    risk_hints = item.get("time_varying_risk_hints", [])

    return Corridor(
        corridor_id=cid,
        city_id=seed.city_id,
        name=item.get("name", cid),
        corridor_type=corridor_type,
        geometry=geometry,
        node_ids=[slug(node["name"]) for node in nodes],
        corridor_class=corridor_class,
        mode=mode,
        base_mobility_model={
            "semantic_corridor": semantic_corridor,
            "route_polyline": route_polyline,
            "route_geometry": route_geometry,
            "served_district_ids": served_district_ids,
            "served_district_evidence": served_evidence,
            "route_runtime_ref": route_runtime_ref,
            "route_distance_m": route_geometry["total_distance_m"],
            "route_duration_s": route_geometry["total_duration_s"],
            "risk_hints": risk_hints,
            "impact_model": impact_model,
        },
        relations=relations,
        control_tags={
            "sampler_axes": item.get("role_hints", []),
            "real_data": (
                ["google_nodes", "google_routes", "web_evidenced"]
                if is_route_verified
                else ["google_nodes", "web_evidenced"]
            ),
            "corridor_class": [corridor_class],
            "mode": [mode],
        },
        director_notes={
            "node_names": item.get("node_names", []),
            "served_areas_claimed": llm_areas,
            "served_district_ids": served_district_ids,
            "served_district_evidence": served_evidence,
            "corridor_class": corridor_class,
            "corridor_boundary": (
                "verified_route_polyline_not_full_routing_graph"
                if is_route_verified
                else "semantic_corridor_not_full_routing_graph"
            ),
            "evidence_summary": item.get("evidence_summary", ""),
        },
        semantic_role=(
            "real_transport_artery" if corridor_class == "transport_corridor" else "service_bearing_city_axis"
        ),
        served_district_ids=served_district_ids,
        served_district_evidence=served_evidence,
        route_polyline=route_polyline,
        route_geometry=route_geometry,
        route_runtime_ref=route_runtime_ref,
        impact_model=impact_model,
        provenance=_prov(item, built_at),
    )


_CORRIDOR_TYPE_TO_MODE = {
    "rail_corridor": "metro",
    "bus_corridor": "bus",
    "road_corridor": "drive",
    "pedestrian_corridor": "walk",
    "commercial_axis": "walk",
    "waterfront_axis": "walk",
    "airport_access": "drive",
    "intermodal_corridor": "mixed",
}


def _default_mode_for_type(corridor_type: str) -> str:
    return _CORRIDOR_TYPE_TO_MODE.get(corridor_type, "mixed")


def _build_impact_model(
    item: dict[str, Any],
    corridor_class: str,
    mode: str,
    route_geometry: dict[str, Any],
) -> dict[str, Any]:
    """Derive a Bench-task-friendly impact model for the corridor.

    Service axes have no transport impact; transport corridors carry
    peak sensitivity, transfer complexity and disruption scope, partly
    sourced from the LLM ``impact_hints`` and partly from corridor type.
    """
    if corridor_class != "transport_corridor":
        return {
            "kind": "service_axis_impact",
            "peak_sensitivity": "low",
            "transfer_complexity": "low",
            "disruption_scope": ["local_walkability"],
            "task_relevance": "background_density_only",
        }
    hints = item.get("impact_hints") or {}
    type_default_peak = {
        "rail_corridor": "high",
        "airport_access": "high",
        "road_corridor": "medium",
        "bus_corridor": "medium",
        "pedestrian_corridor": "low",
        "intermodal_corridor": "high",
    }.get(item.get("corridor_type", ""), "medium")
    type_default_complexity = {
        "intermodal_corridor": "high",
        "rail_corridor": "medium",
        "airport_access": "medium",
    }.get(item.get("corridor_type", ""), "low")
    return {
        "kind": "transport_corridor_impact",
        "mode": mode,
        "peak_sensitivity": str(hints.get("peak_sensitivity", type_default_peak)),
        "transfer_complexity": str(hints.get("transfer_complexity", type_default_complexity)),
        "disruption_scope": list(hints.get("disruption_scope", []) or [
            "arrival_delay",
            "reroute_required",
        ]),
        "is_route_verified": bool(route_geometry.get("is_route_verified")),
        "task_relevance": (
            "first_class_for_routing_and_disruption_tasks"
            if route_geometry.get("is_route_verified")
            else "semantic_only_until_route_verified"
        ),
    }


def _build_place(seed: CitySeed, item: dict[str, Any], districts: list[District], corridors: list[Corridor], built_at: str) -> Place:
    source_place_id = item.get("source_place_id") or item.get("google_place_id") or item.get("osm_id") or item.get("name", "poi")
    provider = item.get("map_provider") or ("osm" if item.get("osm_id") else "google")
    pid = f"place_{seed.city_id}_{slug(source_place_id)}"
    location = item.get("location", {})
    district = _nearest_district(location, districts)
    near_corridors = _near_corridors(location, corridors)
    return Place(
        place_id=pid,
        city_id=seed.city_id,
        name=item.get("name", pid),
        location=location,
        primary_type=item.get("primary_type", "point_of_interest"),
        address=item.get("address", ""),
        district_id=district.district_id if district else None,
        near_corridor_ids=near_corridors,
        map_projection={
            "provider": provider,
            "google_place_id": item.get("google_place_id", ""),
            "source_place_id": source_place_id,
            "osm_id": item.get("osm_id", ""),
            "rating": item.get("rating"),
            "user_rating_count": item.get("user_rating_count"),
            "business_status": item.get("business_status", ""),
            "types": item.get("types", []),
            "google_place_details": item.get("google_place_details", {}),
        },
        control_tags={"real_data": [f"{provider}_place"]},
        director_notes={
            "factual_details_policy": "google_place_details_when_available",
            "density_context": "candidate_place",
        },
        provenance=Provenance(
            source=["openstreetmap"] if provider == "osm" else ["google_maps"],
            built_at=built_at,
            generator="pipeline_v2.map_module",
            quality_flags=list(item.get("quality_flags", [])),
        ),
    )


def slug(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_")[:72] or "item"


def _prov(item: dict[str, Any], built_at: str) -> Provenance:
    evidence = item.get("evidence", {})
    provider = evidence.get("geo_provider", "google")
    sources = ["yunwu_web_search", "openstreetmap" if provider == "osm" else "google_maps"]
    flags = []
    if not evidence.get("llm_evidence_urls"):
        flags.append("missing_web_url")
    geocode_missing = not evidence.get("google_geocode_place_id") and not evidence.get("osm_place_id")
    node_count = evidence.get("google_node_count", evidence.get("osm_node_count", 1))
    if geocode_missing and node_count == 0:
        flags.append("weak_google_resolution")
    return Provenance(source=sources, built_at=built_at, generator="pipeline_v2.map_module", quality_flags=flags)


def _infer_role_tags(counts: dict[str, int]) -> list[str]:
    tags = []
    if counts.get("train_station", 0) + counts.get("subway_station", 0) >= 2:
        tags.append("transport_hub")
    if counts.get("shopping_mall", 0) >= 1:
        tags.append("commercial")
    if counts.get("tourist_attraction", 0) + counts.get("park", 0) >= 2:
        tags.append("tourism")
    if counts.get("hospital", 0) >= 1:
        tags.append("healthcare_access")
    return tags


def _infer_flows(role_tags: list[str]) -> list[str]:
    flows = []
    if "transport_hub" in role_tags or "business" in role_tags:
        flows.append("commute_peak")
    if "tourism" in role_tags or "commercial" in role_tags:
        flows.append("weekend_leisure")
    if "nightlife" in role_tags:
        flows.append("late_evening")
    return flows or ["local_daily_life"]


def _evidence_risk(evidence: dict[str, Any]) -> str:
    if evidence.get("google_anchor_count", 0) < 3:
        return "medium"
    return "low"


def _nearest_district(location: dict[str, float], districts: list[District]) -> District | None:
    if not location or not districts:
        return None
    nearest = min(districts, key=lambda d: _distance_m(location, d.geometry.data))
    if _distance_m(location, nearest.geometry.data) > MAX_DISTRICT_ASSIGNMENT_M:
        return None
    return nearest


def _near_corridors(location: dict[str, float], corridors: list[Corridor]) -> list[str]:
    if not location:
        return []
    scored = []
    for corridor in corridors:
        # Prefer the verified routing polyline for distance checks; fall back
        # to the geocoded node polyline.
        points = (corridor.route_geometry or {}).get("polyline_points") or corridor.geometry.data.get("points", [])
        if points:
            dist_m = min(_distance_m(location, p) for p in points)
            if dist_m <= MAX_NEAR_CORRIDOR_M:
                scored.append((dist_m, corridor.corridor_id))
    return [cid for _, cid in sorted(scored)[:3]]


def _distance2(a: dict[str, float], b: dict[str, float]) -> float:
    return (float(a.get("lat", 0)) - float(b.get("lat", 0))) ** 2 + (
        float(a.get("lng", 0)) - float(b.get("lng", 0))
    ) ** 2


def _distance_m(a: dict[str, float], b: dict[str, float]) -> float:
    radius = 6371000.0
    lat1 = radians(float(a.get("lat", 0)))
    lat2 = radians(float(b.get("lat", 0)))
    dlat = lat2 - lat1
    dlng = radians(float(b.get("lng", 0)) - float(a.get("lng", 0)))
    hav = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return 2 * radius * asin(sqrt(hav))
