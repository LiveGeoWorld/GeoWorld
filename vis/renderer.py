"""Convert world.json entities to GeoJSON for Leaflet rendering."""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Any

from shared.constants import (
    PLACE_COLOR, ACTOR_COLOR, DISTRICT_COLOR, CORRIDOR_COLOR,
    TYPE_TO_CATEGORY, CATEGORY_LABELS_ZH, CATEGORY_LABELS_EN,
)


def world_to_geojson_summary(
    world: dict[str, Any],
    characters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a summary dict with counts and metadata."""
    actor_ids = {actor.get("actor_id") for actor in world.get("actors", []) if actor.get("actor_id")}
    character_count = sum(
        1 for character in (characters or [])
        if character.get("character_id") and character.get("character_id") not in actor_ids
    )
    return {
        "city": world.get("cities", [{}])[0] if world.get("cities") else {},
        "counts": {
            "districts": len(world.get("districts", [])),
            "corridors": len(world.get("corridors", [])),
            "places": len(world.get("places", [])),
            "services": len(world.get("services", [])),
            "actors": len(world.get("actors", [])) + character_count,
            "information_items": len(world.get("information_items", [])),
            "event_templates": len(world.get("event_templates", [])),
        },
        "categories": CATEGORY_LABELS_ZH,
        "colors": {
            "place": PLACE_COLOR,
            "actor": ACTOR_COLOR,
            "district": DISTRICT_COLOR,
            "corridor": CORRIDOR_COLOR,
        },
    }


def districts_to_geojson(
    districts: list[dict[str, Any]],
    places: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert districts to GeoJSON FeatureCollection.

    Current map worlds store districts as representative center points rather
    than administrative polygons. For visualization, estimate a display radius
    from places assigned to each district so the layer reads as an area.
    """
    places_by_district: dict[str, list[dict[str, Any]]] = {}
    for place in places or []:
        did = place.get("district_id")
        if did:
            places_by_district.setdefault(did, []).append(place)

    features = []
    for d in districts:
        geom = d.get("geometry", {})
        data = geom.get("data", {})
        lat = data.get("lat")
        lng = data.get("lng")
        if lat is None or lng is None:
            continue
        district_places = places_by_district.get(d.get("district_id"), [])
        radius_m = _district_radius_m(lat, lng, district_places)
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [lng, lat],
            },
            "properties": {
                "id": d.get("district_id"),
                "name": d.get("name"),
                "role_tags": d.get("role_tags", []),
                "dominant_flows": d.get("dominant_flows", []),
                "risk_profile": d.get("risk_profile", {}),
                "anchor_places": d.get("director_notes", {}).get("anchor_places", [])[:5],
                "evidence_summary": d.get("director_notes", {}).get("evidence_summary", ""),
                "place_count": len(district_places),
                "radius_m": radius_m,
                "entity_type": "district",
            },
        })
    return {"type": "FeatureCollection", "features": features}


def _district_radius_m(center_lat: float, center_lng: float, places: list[dict[str, Any]]) -> int:
    """Estimate display radius from assigned place spread, clamped for readability."""
    distances = []
    for place in places:
        loc = place.get("location", {})
        lat = loc.get("lat")
        lng = loc.get("lng")
        if lat is None or lng is None:
            continue
        distances.append(_haversine_m(center_lat, center_lng, lat, lng))
    if not distances:
        return 900
    distances.sort()
    # Use a high percentile instead of max so one remote POI does not dominate.
    idx = min(len(distances) - 1, max(0, int(len(distances) * 0.85)))
    return int(max(700, min(2400, distances[idx] * 1.15)))


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6371000.0
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * radius * asin(sqrt(a))


def corridors_to_geojson(
    corridors: list[dict[str, Any]],
    *,
    districts: list[dict[str, Any]] | None = None,
    places: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert corridors to GeoJSON FeatureCollection (polylines)."""
    district_names = {
        d.get("district_id"): d.get("name", d.get("district_id"))
        for d in (districts or [])
        if d.get("district_id")
    }
    place_counts: dict[str, int] = {}
    for place in places or []:
        for cid in place.get("near_corridor_ids", []) or []:
            place_counts[cid] = place_counts.get(cid, 0) + 1

    features = []
    for c in corridors:
        mobility = c.get("base_mobility_model", {})
        route_geometry = c.get("route_geometry") or mobility.get("route_geometry", {})
        route_polyline = c.get("route_polyline") or mobility.get("route_polyline", {})
        geom = c.get("geometry", {})
        points = (
            route_geometry.get("polyline_points")
            or route_polyline.get("points")
            or geom.get("data", {}).get("points", [])
        )
        if not points or len(points) < 2:
            continue
        coords = [[p["lng"], p["lat"]] for p in points if p.get("lat") and p.get("lng")]
        if len(coords) < 2:
            continue
        served_district_ids = c.get("served_district_ids") or [
            r.get("target_id") for r in c.get("relations", []) if r.get("relation_type") == "serves"
        ]
        semantic_corridor = mobility.get("semantic_corridor", {})
        node_names = (
            route_polyline.get("node_names")
            or c.get("director_notes", {}).get("node_names")
            or c.get("node_ids", [])
        )
        # Logical corridor nodes (stations / intersections) — NOT every Routes API
        # polyline vertex. geometry.data.points holds the geocoded node locations.
        node_points = geom.get("data", {}).get("points", [])
        is_route_verified = bool(
            route_geometry.get("is_route_verified") or route_polyline.get("is_route_verified")
        )
        corridor_class = c.get("corridor_class") or semantic_corridor.get("corridor_class", "transport_corridor")
        mode = c.get("mode") or semantic_corridor.get("mode", "mixed")
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coords,
            },
            "properties": {
                "id": c.get("corridor_id"),
                "name": c.get("name"),
                "corridor_type": c.get("corridor_type"),
                "corridor_class": corridor_class,
                "mode": mode,
                "is_route_verified": is_route_verified,
                "route_provider": route_geometry.get("provider") or route_polyline.get("provider", ""),
                "verified_segment_count": route_geometry.get("verified_segment_count", 0),
                "segment_count": route_geometry.get("segment_count", 0),
                "node_ids": c.get("node_ids", []),
                "node_names": node_names,
                "node_points": node_points,
                "mobility": mobility,
                "risk_hints": mobility.get("risk_hints", []),
                "service_bands": semantic_corridor.get("service_bands", []),
                "served_district_ids": served_district_ids,
                "served_districts": [
                    {"id": did, "name": district_names.get(did, did)}
                    for did in served_district_ids
                ],
                "route_polyline": route_polyline,
                "route_geometry": route_geometry,
                "route_runtime_ref": c.get("route_runtime_ref") or mobility.get("route_runtime_ref", {}),
                "is_full_routing_graph": bool(route_polyline.get("is_full_routing_graph")),
                "route_distance_m": route_geometry.get("total_distance_m") or mobility.get("route_distance_m", 0),
                "route_duration_s": route_geometry.get("total_duration_s") or mobility.get("route_duration_s", 0),
                "impact_model": c.get("impact_model") or mobility.get("impact_model", {}),
                "place_count": place_counts.get(c.get("corridor_id"), 0),
                "entity_type": "corridor",
            },
        })
    return {"type": "FeatureCollection", "features": features}


def corridor_detail(
    corridor: dict[str, Any],
    districts: list[dict[str, Any]],
    places: list[dict[str, Any]],
    information_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return detailed corridor information for the right-hand panel."""
    corridor_id = corridor.get("corridor_id", "")
    district_names = {
        d.get("district_id"): d.get("name", d.get("district_id"))
        for d in districts
        if d.get("district_id")
    }
    served_district_ids = corridor.get("served_district_ids") or [
        r.get("target_id") for r in corridor.get("relations", []) if r.get("relation_type") == "serves"
    ]
    linked_places = [
        p for p in places
        if corridor_id in (p.get("near_corridor_ids") or [])
    ]
    linked_info = [
        i for i in information_items
        if (i.get("about") or {}).get("corridor_id") == corridor_id
    ]
    mobility = corridor.get("base_mobility_model", {})
    route_polyline = corridor.get("route_polyline") or mobility.get("route_polyline", {})
    route_geometry = corridor.get("route_geometry") or mobility.get("route_geometry", {})
    semantic_corridor = mobility.get("semantic_corridor", {})
    served_evidence = corridor.get("served_district_evidence") or mobility.get("served_district_evidence", [])
    return {
        "corridor_id": corridor_id,
        "name": corridor.get("name"),
        "corridor_type": corridor.get("corridor_type"),
        "corridor_class": corridor.get("corridor_class") or semantic_corridor.get("corridor_class", "transport_corridor"),
        "mode": corridor.get("mode") or semantic_corridor.get("mode", "mixed"),
        "semantic_role": corridor.get("semantic_role"),
        "is_route_verified": bool(route_geometry.get("is_route_verified") or route_polyline.get("is_route_verified")),
        "route_provider": route_geometry.get("provider") or route_polyline.get("provider", ""),
        "verified_segment_count": route_geometry.get("verified_segment_count", 0),
        "segment_count": route_geometry.get("segment_count", 0),
        "served_districts": [
            {"id": did, "name": district_names.get(did, did)}
            for did in served_district_ids
        ],
        "served_district_evidence": [
            {
                "name_claimed": ev.get("name_claimed"),
                "district_id": ev.get("district_id"),
                "district_name": district_names.get(ev.get("district_id"), ev.get("district_id")),
                "match_method": ev.get("match_method"),
                "min_distance_m": ev.get("min_distance_m"),
                "verified": bool(ev.get("verified")),
            }
            for ev in served_evidence
        ],
        "impact_model": corridor.get("impact_model") or mobility.get("impact_model", {}),
        "node_ids": corridor.get("node_ids", []),
        "node_names": route_polyline.get("node_names") or corridor.get("director_notes", {}).get("node_names", []),
        "route_polyline": route_polyline,
        "route_geometry": route_geometry,
        "route_runtime_ref": corridor.get("route_runtime_ref") or mobility.get("route_runtime_ref", {}),
        "route_distance_m": route_geometry.get("total_distance_m") or mobility.get("route_distance_m", 0),
        "route_duration_s": route_geometry.get("total_duration_s") or mobility.get("route_duration_s", 0),
        "risk_hints": mobility.get("risk_hints", []),
        "service_bands": semantic_corridor.get("service_bands", []),
        "nearby_places": [
            {
                "place_id": p.get("place_id"),
                "name": p.get("name"),
                "primary_type": p.get("primary_type"),
                "district_id": p.get("district_id"),
                "district_name": district_names.get(p.get("district_id"), p.get("district_id")),
                "rating": (p.get("map_projection") or {}).get("rating"),
            }
            for p in linked_places[:20]
        ],
        "nearby_place_count": len(linked_places),
        "information_items": [
            {
                "info_id": i.get("info_id"),
                "content_type": i.get("content_type"),
                "content": i.get("content"),
                "observability": i.get("observability"),
            }
            for i in linked_info[:8]
        ],
    }


def places_to_geojson(
    places: list[dict[str, Any]],
    *,
    q: str | None = None,
    category: str | None = None,
    min_rating: float | None = None,
    district_id: str | None = None,
) -> dict[str, Any]:
    """Convert places to GeoJSON FeatureCollection with optional filters."""
    features = []
    for p in places:
        # Filters
        name = p.get("name", "")
        loc = p.get("location", {})
        lat = loc.get("lat")
        lng = loc.get("lng")
        if lat is None or lng is None:
            continue

        primary_type = p.get("primary_type", "")
        cat = TYPE_TO_CATEGORY.get(primary_type, "other")
        rating = (p.get("map_projection") or {}).get("rating")

        if q and q.lower() not in name.lower():
            continue
        if category and cat != category:
            continue
        if min_rating is not None and (rating is None or rating < min_rating):
            continue
        if district_id and p.get("district_id") != district_id:
            continue

        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [lng, lat],
            },
            "properties": {
                "id": p.get("place_id"),
                "name": name,
                "primary_type": primary_type,
                "category": cat,
                "category_label": CATEGORY_LABELS_ZH.get(cat, cat),
                "address": p.get("address", ""),
                "district_id": p.get("district_id"),
                "near_corridor_ids": p.get("near_corridor_ids", []),
                "rating": rating,
                "user_rating_count": (p.get("map_projection") or {}).get("user_rating_count"),
                "business_status": (p.get("map_projection") or {}).get("business_status", ""),
                "google_place_id": (p.get("map_projection") or {}).get("google_place_id", ""),
                "entity_type": "place",
            },
        })
    return {"type": "FeatureCollection", "features": features}


def place_detail(
    place: dict[str, Any],
    services: list[dict[str, Any]],
    information_items: list[dict[str, Any]],
    *,
    districts: list[dict[str, Any]] | None = None,
    corridors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return detailed info for a single place, with linked services and information."""
    from shared.opening_hours import effective_opening_hours
    place_id = place.get("place_id", "")
    pid = place_id
    district_names = {
        d.get("district_id"): d.get("name", d.get("district_id"))
        for d in (districts or [])
        if d.get("district_id")
    }
    corridor_names = {
        c.get("corridor_id"): c.get("name", c.get("corridor_id"))
        for c in (corridors or [])
        if c.get("corridor_id")
    }

    # Find linked service
    linked_service = None
    for s in services:
        if s.get("place_id") == pid:
            linked_service = s
            break

    # Find linked information items
    linked_info = []
    for info in information_items:
        about = info.get("about", {})
        if about.get("place_id") == pid or about.get("service_id") == (linked_service or {}).get("service_id"):
            linked_info.append(info)

    return {
        "place_id": place.get("place_id"),
        "name": place.get("name"),
        "primary_type": place.get("primary_type"),
        "category": TYPE_TO_CATEGORY.get(place.get("primary_type", ""), "other"),
        "address": place.get("address"),
        "location": place.get("location"),
        "district_id": place.get("district_id"),
        "district_name": district_names.get(place.get("district_id"), place.get("district_id")),
        "near_corridors": [
            {"id": cid, "name": corridor_names.get(cid, cid)}
            for cid in (place.get("near_corridor_ids") or [])
        ],
        "map_projection": place.get("map_projection"),
        "opening_hours": effective_opening_hours(place),
        "service": {
            "service_id": linked_service.get("service_id"),
            "service_type": linked_service.get("service_type"),
            "affordances": linked_service.get("affordances", []),
            "state_model": linked_service.get("state_model", {}),
        } if linked_service else None,
        "information_items": [
            {
                "info_id": i.get("info_id"),
                "content_type": i.get("content_type"),
                "content": i.get("content"),
                "observability": i.get("observability"),
            }
            for i in linked_info[:5]
        ],
    }


def actors_to_geojson(
    actors: list[dict[str, Any]],
    characters: list[dict[str, Any]] | None = None,
    *,
    district_map: dict[str, dict[str, Any]] | None = None,
    relationships: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Convert actors to GeoJSON — placed at their current district center.

    If characters.json data is provided, it is merged with world.json actors.
    """
    district_map = district_map or {}
    relationships = relationships or []
    features = []
    character_names = {
        character.get("character_id"): character.get("name", character.get("character_id"))
        for character in (characters or [])
        if character.get("character_id")
    }
    relationship_summary = _relationship_summary_by_actor(relationships, character_names)

    # Build actor lookup from world.json
    world_actors = {a.get("actor_id"): a for a in actors}

    # Merge with character data if available
    all_actors = list(actors)
    if characters:
        for ch in characters:
            ch_id = ch.get("character_id")
            if ch_id and ch_id not in world_actors:
                all_actors.append({
                    "actor_id": ch_id,
                    "name": ch.get("name", ch_id),
                    "relationship_to_user": ch.get("role", ""),
                    "occupation": ch.get("occupation", ""),
                    "source": ch.get("source", ""),
                    "preferences": ch.get("preferences", {}),
                    "mobility": ch.get("mobility", {}),
                    "availability": ch.get("availability", {}),
                    "communication": ch.get("communication", {}),
                    "constraints": ch.get("constraints", {}),
                    "cultural_context": ch.get("cultural_context", {}),
                    "is_character": True,
                })

    for a in all_actors:
        mob = a.get("mobility", {})
        did = mob.get("current_district_id") or mob.get("home_district_id")
        district = district_map.get(did, {}) if did else {}
        lat = mob.get("lat")
        lng = mob.get("lng")
        if lat is None or lng is None:
            d_geom = district.get("geometry", {})
            d_data = d_geom.get("data", {})
            lat = d_data.get("lat")
            lng = d_data.get("lng")
        if lat is None or lng is None:
            continue

        is_char = a.get("is_character", False)
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [lng, lat],
            },
            "properties": {
                "id": a.get("actor_id"),
                "name": a.get("name"),
                "relationship": (
                    a.get("relationship_to_user")
                    or relationship_summary.get(a.get("actor_id"), {}).get("label", "")
                ),
                "relationship_count": relationship_summary.get(a.get("actor_id"), {}).get("count", 0),
                "role": a.get("role_in_scenario") or a.get("role") or a.get("occupation"),
                "occupation": a.get("occupation", ""),
                "source": a.get("source", ""),
                "preferences": a.get("preferences", {}),
                "mobility": a.get("mobility", {}),
                "availability": a.get("availability", {}),
                "communication": a.get("communication", {}),
                "constraints": a.get("constraints", []),
                "cultural_context": a.get("cultural_context", {}),
                "current_district": district.get("name", ""),
                "entity_type": "actor",
                "is_character": is_char,
            },
        })
    return {"type": "FeatureCollection", "features": features}


def _relationship_summary_by_actor(
    relationships: list[dict[str, Any]],
    character_names: dict[str, str],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    for rel in relationships:
        cid = rel.get("character_id")
        rid = rel.get("related_id")
        rtype = rel.get("type", "")
        if cid and rid:
            grouped.setdefault(cid, []).append(f"{rtype}: {character_names.get(rid, rid)}")
            grouped.setdefault(rid, []).append(f"{rtype}: {character_names.get(cid, cid)}")
    return {
        actor_id: {
            "count": len(items),
            "label": "；".join(items[:3]) + (" ..." if len(items) > 3 else ""),
        }
        for actor_id, items in grouped.items()
    }


def character_detail(
    actor: dict[str, Any],
    characters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return detailed info for a single actor/character."""
    aid = actor.get("actor_id", "")
    if characters:
        for ch in characters:
            if ch.get("character_id") == aid:
                return {**actor, "character_data": ch}
    return actor


def stats(world: dict[str, Any]) -> dict[str, Any]:
    """Compute statistics from world.json."""
    places = world.get("places", [])
    from collections import Counter

    type_counter = Counter()
    cat_counter = Counter()
    ratings = []
    for p in places:
        pt = p.get("primary_type", "other")
        cat = TYPE_TO_CATEGORY.get(pt, "other")
        type_counter[pt] += 1
        cat_counter[cat] += 1
        rating = (p.get("map_projection") or {}).get("rating")
        if rating is not None:
            ratings.append(rating)

    # Rating distribution
    rating_bins = {"0-1": 0, "1-2": 0, "2-3": 0, "3-4": 0, "4-5": 0}
    for r in ratings:
        if r < 1:
            rating_bins["0-1"] += 1
        elif r < 2:
            rating_bins["1-2"] += 1
        elif r < 3:
            rating_bins["2-3"] += 1
        elif r < 4:
            rating_bins["3-4"] += 1
        else:
            rating_bins["4-5"] += 1

    # Places per district
    district_place_counts = Counter(p.get("district_id", "?") for p in places)

    # Service types
    service_counter = Counter(s.get("service_type", "?") for s in world.get("services", []))

    return {
        "total_places": len(places),
        "type_distribution": dict(type_counter.most_common(20)),
        "category_distribution": dict(cat_counter.most_common()),
        "rating_distribution": rating_bins,
        "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else 0,
        "service_types": dict(service_counter.most_common()),
        "district_place_counts": dict(district_place_counts.most_common()),
    }
