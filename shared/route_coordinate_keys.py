"""Coordinate keys shared by route construction and frozen MapApp queries."""

import hashlib
import math
from typing import Any


def _normalized_coordinate(value: Any) -> str | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    number = round(number, 6)
    if number == 0:
        number = 0.0
    return f"{number:.6f}"


def _location_token(place: dict[str, Any] | None) -> str | None:
    location = (place or {}).get("location") or {}
    lat = _normalized_coordinate(location.get("lat"))
    lng = _normalized_coordinate(location.get("lng"))
    if lat is None or lng is None or (lat == "0.000000" and lng == "0.000000"):
        return None
    return f"{lat},{lng}"


def build_coordinate_key(
    origin_place: dict[str, Any], dest_place: dict[str, Any], mode: str,
    waypoint_place: dict[str, Any] | None, snapshot_id: str,
) -> str | None:
    origin = _location_token(origin_place)
    destination = _location_token(dest_place)
    waypoint = _location_token(waypoint_place) if waypoint_place else ""
    if not origin or not destination or (waypoint_place and not waypoint):
        return None
    raw = f"{origin}|{destination}|{mode}|snapshot={snapshot_id}|waypoint={waypoint}"
    return hashlib.md5(raw.encode()).hexdigest()[:20]


def coordinate_key_from_entry(entry: dict[str, Any]) -> str | None:
    origin = {"location": {"lat": entry.get("origin_lat"), "lng": entry.get("origin_lng")}}
    destination = {"location": {"lat": entry.get("dest_lat"), "lng": entry.get("dest_lng")}}
    waypoint = None
    if entry.get("waypoint_place_id"):
        # Legacy entries do not persist waypoint coordinates, so they remain
        # ID-keyed rather than being merged on incomplete endpoint evidence.
        return None
    return build_coordinate_key(
        origin, destination, str(entry.get("mode") or ""), waypoint,
        str(entry.get("snapshot_id") or "static_v1"),
    )

def frozen_coordinate_index(route_index):
    """Index unambiguous static direct routes, without storing new cache entries."""
    index = {}
    for routes in route_index.values():
        for route in routes:
            entry = route.get("_endpoint_record", {})
            if (entry.get("snapshot_id") or "static_v1") != "static_v1":
                continue
            duration = route["duration_s"]
            if route.get("available") is not False and not (isinstance(duration, (int, float)) and duration > 0):
                continue
            key = coordinate_key_from_entry(entry)
            if not key:
                continue
            fact = {field: value for field, value in route.items() if field != "_endpoint_record"}
            if key not in index:
                index[key] = fact
            elif index[key] != fact:
                index[key] = None
    return index
