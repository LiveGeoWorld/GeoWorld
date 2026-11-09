"""Project searchable locations from one frozen world without changing its POIs."""
from math import isfinite


def source_place_id(place):
    projection = place.get("map_projection") or {}
    return next((value for value in (
        place.get("source_place_id"), place.get("google_place_id"), place.get("osm_place_id"),
        projection.get("source_place_id"), projection.get("google_place_id"), projection.get("osm_id"),
    ) if value), "")


def map_index_places(world):
    """Keep formal records first and add one search record per sourced anchor."""
    places = list(world.get("places") or [])
    seen = {(place.get("city_id"), source_place_id(place)) for place in places if isinstance(place, dict)}
    for district in world.get("districts") or []:
        city_id = district.get("city_id")
        for anchor in (district.get("director_notes") or {}).get("anchor_places") or []:
            source_id = source_place_id(anchor)
            identity = (city_id, source_id)
            location = anchor.get("location") or {}
            if not city_id or not source_id or identity in seen or not anchor.get("name"):
                continue
            if not all(type(location.get(key)) in (int, float) and isfinite(location[key])
                       for key in ("lat", "lng")):
                continue
            seen.add(identity)
            places.append({
                "place_id": f"district_anchor:{city_id}:{source_id}",
                "city_id": city_id,
                "name": anchor["name"],
                "address": anchor.get("address") or anchor.get("formatted_address") or "",
                "location": dict(location),
                "primary_type": anchor.get("primary_type") or anchor.get("type") or "",
                "map_projection": {**anchor, "source_place_id": source_id},
            })
    return places
