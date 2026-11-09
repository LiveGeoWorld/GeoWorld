"""Project frozen geographic evidence without inventing impact membership."""

from __future__ import annotations

import copy
from typing import Any


def geographic_evidence(world: dict[str, Any], route_entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Retain exact source paths; a named representative point is not an area.

Route instructions are evidence for construction reasoning, not a parsed road
network. In particular, 'toward X' must not be interpreted here as 'on X'.
"""
    districts = []
    for index, district in enumerate(world.get("districts") or []):
        geometry = district.get("geometry")
        districts.append({
            "source_ref": f"world#/districts/{index}",
            "district_id": district.get("district_id"), "name": district.get("name"),
            "geometry": copy.deepcopy(geometry),
            "has_area_geometry": isinstance(geometry, dict) and str(geometry.get("kind", "")).lower()
            in {"polygon", "multipolygon"},
        })
    places = [{
        "source_ref": f"world#/places/{index}", "place_id": place.get("place_id"),
        "name": place.get("name"), "location": copy.deepcopy(place.get("location")),
        "district_id": place.get("district_id"),
    } for index, place in enumerate(world.get("places") or [])]
    routes = []
    for index, entry in enumerate(route_entries):
        route = entry.get("route") or {}
        if route.get("available") is not True or entry.get("snapshot_id", "static_v1") != "static_v1":
            continue
        row = {key: copy.deepcopy(entry.get(key)) for key in (
            "origin_place_id", "dest_place_id", "origin_lat", "origin_lng", "dest_lat", "dest_lng", "mode")}
        row["source_ref"] = f"route_cache#/cache/{index}"
        row["route"] = {key: copy.deepcopy(route[key]) for key in (
            "summary", "duration_s", "distance_m", "encoded_polyline", "polyline_points") if key in route}
        row["steps"] = [{
            "source_ref": f"route_cache#/cache/{index}/route/steps/{step_index}",
            "facts": {key: copy.deepcopy(step[key]) for key in (
                "instruction", "travelMode", "startLocation", "endLocation", "polyline",
                "transitDetails", "duration_s", "distance_m") if key in step},
        } for step_index, step in enumerate(route.get("steps") or [])]
        routes.append(row)
    return {"districts": districts, "places": places, "routes": routes}
