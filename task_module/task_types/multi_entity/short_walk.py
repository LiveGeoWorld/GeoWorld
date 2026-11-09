"""Task-local short-distance walking estimates, not queried route facts."""
import math

from task_module.task_types.route_planning.sampler import _haversine

# Same WALK speed as route_planning.sampler._estimate_duration.
WALK_SPEED_M_S = 1.4


def short_walk_estimate(person, venue):
    location = venue.get("location") or {}
    coordinates = [person.get("home_lat"), person.get("home_lng"), location.get("lat"), location.get("lng")]
    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in coordinates):
        return None
    distance = _haversine(*coordinates)
    if not 0 <= distance < 100:
        return None
    # Reuse the existing repository walking speed; no waypoint detour.
    seconds = math.ceil(distance / WALK_SPEED_M_S)
    minutes = math.ceil(seconds / 60)
    return {"from": person.get("home_place_name") or person.get("name", ""),
            "to": venue.get("name", ""), "mode": "WALK", "distance_m": distance,
            "duration_s": seconds, "duration_min": minutes,
            "walk_speed_m_s": WALK_SPEED_M_S,
            "source": "short_distance_walk_estimate",
            "method": "coordinate_distance_divided_by_existing_walk_speed_ceiled_to_minutes"}


def fill_short_walks(sandbox):
    """Fill only empty existing route rows in a modern task's candidate scope."""
    routes = sandbox.get("route_data") or {}
    records = []
    for person in sandbox.get("participants", []):
        for venue in sandbox.get("candidate_places", []):
            record = short_walk_estimate(person, venue)
            if record is None:
                continue
            key = f"{record['from']}→{record['to']}"
            if key not in routes or routes[key]:
                continue
            routes[key] = {"WALK": record["duration_min"]}
            records.append(record)
    if records:
        sandbox.setdefault("short_walk_estimates", []).extend(records)
    return records
