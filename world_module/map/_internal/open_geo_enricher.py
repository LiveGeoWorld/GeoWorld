"""OpenStreetMap enrichment fallback for v7 city worlds.

This provider avoids paid API keys. It uses Nominatim for geocoding and
Overpass for POI discovery, then emits the same enriched shape consumed by
`substrate_builder`.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Any

import requests

from world_module.map._internal.google_geo_enricher import (
    ANCHOR_TYPES,
    KNOWN_PLACE_TYPES,
    PLACE_TYPE_ALIASES,
    _corridor_default_types,
    _candidate_collection_report,
    _dedupe_places,
    _eligible_places,
    _normalize_place_type,
    _sample_corridor_nodes,
)
from world_module.map.seed import CitySeed


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = [
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "GeoAgentBench-v7/0.1 (research prototype)"


class OpenStreetMapGeoEnricher:
    def __init__(self, *, max_workers: int = 4):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.max_workers = max(1, min(max_workers, 6))
        self._nominatim_lock = Lock()
        self._last_nominatim_at = 0.0

    def enrich(self, seed: CitySeed, discovery: dict[str, Any]) -> dict[str, Any]:
        districts = self._enrich_districts(seed, discovery.get("district_candidates", []))
        corridors = self._enrich_corridors(seed, discovery.get("corridor_candidates", []))
        places, candidate_collection = self._enrich_places(seed, discovery.get("place_search_plan", []), districts, corridors)
        return {
            "seed": seed.__dict__,
            "discovery": discovery,
            "geo_provider": "osm",
            "districts": districts,
            "corridors": corridors,
            "places": places,
            "poi_candidate_collection": candidate_collection,
        }

    def _enrich_districts(self, seed: CitySeed, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _parallel_map(
            candidates,
            lambda candidate: self._enrich_one_district(seed, candidate),
            max_workers=self.max_workers,
        )

    def _enrich_one_district(self, seed: CitySeed, candidate: dict[str, Any]) -> dict[str, Any]:
        name = candidate.get("name", "")
        approx = candidate.get("approx_center") or {}
        if approx.get("lat") is not None and approx.get("lng") is not None:
            geocode = {
                **_empty_geocode(),
                "formatted_address": f"{name}, {seed.city_name}, {seed.country}",
                "location": {"latitude": float(approx["lat"]), "longitude": float(approx["lng"])},
            }
        else:
            geocode = self._geocode(seed, f"{name}, {seed.city_name}, {seed.country}")
        loc = _location_or_candidate(seed, geocode, candidate)
        anchors: list[dict[str, Any]] = []
        for ptype in ANCHOR_TYPES:
            anchors.extend(
                self._overpass_places(
                    loc["lat"],
                    loc["lng"],
                    radius_m=min(seed.search_radius_m, 2200),
                    place_type=ptype,
                    max_results=3,
                )
            )
        anchors = _dedupe_places(anchors)[:18]
        type_counts = Counter(_primary_type(p) for p in anchors if _primary_type(p))
        return {
            **candidate,
            "google_geocode": geocode,
            "osm_geocode": geocode,
            "center": loc,
            "anchor_places": [_place_summary(p) for p in anchors],
            "poi_type_counts": dict(type_counts),
            "evidence": {
                "llm_evidence_urls": candidate.get("evidence_urls", []),
                "geo_provider": "osm",
                "google_geocode_place_id": "",
                "osm_place_id": geocode.get("place_id", ""),
                "google_anchor_count": len(anchors),
                "osm_anchor_count": len(anchors),
            },
        }

    def _enrich_corridors(self, seed: CitySeed, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _parallel_map(
            candidates,
            lambda candidate: self._enrich_one_corridor(seed, candidate),
            max_workers=self.max_workers,
        )

    def _enrich_one_corridor(self, seed: CitySeed, candidate: dict[str, Any]) -> dict[str, Any]:
        nodes = []
        for node_name in _sample_names(candidate.get("node_names", []), limit=4):
            geocode = self._geocode(seed, f"{node_name}, {seed.city_name}, {seed.country}")
            loc = _location_from_geocode(geocode)
            if loc:
                nodes.append(
                    {
                        "name": node_name,
                        "location": loc,
                        "google_place_id": "",
                        "osm_place_id": geocode.get("place_id", ""),
                        "formatted_address": geocode.get("formatted_address", ""),
                    }
                )
        route = _route_summary(nodes, candidate.get("corridor_type", ""))
        corridor_class = (candidate.get("corridor_class") or "transport_corridor").strip()
        mode = (candidate.get("mode") or _osm_default_mode_for(candidate.get("corridor_type", ""))).strip()
        route_geometry = {
            "provider": "osm_geocoded_nodes",
            "travel_mode": mode,
            "is_route_verified": False,
            "polyline_points": [n["location"] for n in nodes],
            "segment_count": max(0, len(nodes) - 1),
            "verified_segment_count": 0,
            "total_distance_m": route.get("distance_m", 0),
            "total_duration_s": route.get("duration_s", 0),
            "segments": [],
            "note": "OSM mode does not call Google Routes; polyline straight-connects geocoded nodes.",
        }
        return {
            **candidate,
            "corridor_class": corridor_class,
            "mode": mode,
            "nodes": nodes,
            "route_summary": route,
            "route_geometry": route_geometry,
            "evidence": {
                "llm_evidence_urls": candidate.get("evidence_urls", []),
                "geo_provider": "osm",
                "google_node_count": len(nodes),
                "osm_node_count": len(nodes),
                "google_route_distance_m": route.get("distance_m", 0),
                "google_route_verified_segments": 0,
                "google_route_total_segments": max(0, len(nodes) - 1),
            },
        }

    def _enrich_places(
        self,
        seed: CitySeed,
        search_plan: list[dict[str, Any]],
        districts: list[dict[str, Any]],
        corridors: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        by_name = {d.get("name"): d for d in districts}
        district_jobs = []
        for plan in search_plan:
            district = by_name.get(plan.get("district_name"))
            if district:
                district_jobs.append((plan, district))
        budget = seed.poi_candidate_budget
        has_corridor_jobs = any(corridor.get("nodes") for corridor in corridors)
        if not has_corridor_jobs:
            corridor_budget = 0
        elif not district_jobs:
            corridor_budget = budget
        else:
            corridor_budget = min(budget // 4, max(0, budget - len(district_jobs)))
        district_budget = budget - corridor_budget
        district_collected: list[dict[str, Any]] = []
        raw_candidates = 0
        for index, (plan, district) in enumerate(district_jobs):
            remaining_jobs = len(district_jobs) - index
            current = len(_eligible_places(seed, district_collected))
            quota = max(1, (max(0, district_budget - current) + remaining_jobs - 1) // remaining_jobs)
            result = self._search_district_places(seed, plan, district, candidate_limit=quota)
            raw_candidates += len(result)
            district_collected.extend(result)
        corridor_collected = self._enrich_corridor_places(seed, corridors, candidate_limit=corridor_budget)
        raw_candidates += len(corridor_collected)
        eligible = _eligible_places(seed, [*district_collected, *corridor_collected])
        report = _candidate_collection_report(
            seed,
            budget=budget,
            district_budget=district_budget,
            corridor_budget=corridor_budget,
            raw_candidates=raw_candidates,
            eligible_candidates=len(eligible),
        )
        print(
            f"[poi-candidates] target={report['formal_target']} budget={budget} "
            f"eligible={len(eligible)} raw={raw_candidates}",
            flush=True,
        )
        return [_place_summary(p) for p in eligible], report

    def _search_district_places(
        self,
        seed: CitySeed,
        plan: dict[str, Any],
        district: dict[str, Any],
        *,
        candidate_limit: int,
    ) -> list[dict[str, Any]]:
        center = district["center"]
        place_types = [_normalize_place_type(ptype) for ptype in plan.get("included_types", seed.place_types)[:6]]
        place_types = [ptype for ptype in place_types if ptype]
        places = self._overpass_places_for_types(
            center["lat"],
            center["lng"],
            radius_m=seed.search_radius_m,
            place_types=place_types,
            max_results=max(1, candidate_limit),
        )
        if places:
            return places
        collected = []
        for query in plan.get("text_queries", [])[:4]:
            remaining = candidate_limit - len(_eligible_places(seed, collected))
            if remaining <= 0:
                break
            collected.extend(self._nominatim_place_search(seed, query, max_results=max(1, min(4, remaining))))
        for place_type in place_types[:3]:
            remaining = candidate_limit - len(_eligible_places(seed, collected))
            if remaining <= 0:
                break
            collected.extend(self._nominatim_place_search(seed, f"{place_type} near {district.get('name')}, {seed.city_name}", max_results=max(1, min(4, remaining)), fallback_type=place_type))
        return _eligible_places(seed, collected)

    def _enrich_corridor_places(
        self,
        seed: CitySeed,
        corridors: list[dict[str, Any]],
        *,
        candidate_limit: int,
    ) -> list[dict[str, Any]]:
        if candidate_limit <= 0:
            return []
        collected: list[dict[str, Any]] = []
        jobs = []
        for corridor in corridors:
            nodes = corridor.get("nodes", [])
            if not nodes:
                continue
            service_bands = corridor.get("corridor_service_bands") or []
            fallback_types = _corridor_default_types(corridor.get("corridor_type", ""))
            sampled_nodes = _sample_corridor_nodes(nodes, limit=seed.corridor_node_sample_limit)
            for node in sampled_nodes:
                loc = node["location"]
                place_types = []
                for band in service_bands[: min(seed.corridor_band_limit, 2)] or [{"place_types": fallback_types}]:
                    for ptype in (band.get("place_types") or fallback_types)[: seed.corridor_place_types_per_band]:
                        normalized_type = _normalize_place_type(ptype)
                        if normalized_type and normalized_type not in place_types:
                            place_types.append(normalized_type)
                jobs.append((loc, place_types[:6]))
        for index, (loc, place_types) in enumerate(jobs):
            remaining_jobs = len(jobs) - index
            current = len(_eligible_places(seed, collected))
            if current >= candidate_limit:
                break
            quota = max(1, (max(0, candidate_limit - current) + remaining_jobs - 1) // remaining_jobs)
            collected.extend(
                self._overpass_places_for_types(
                    loc["lat"],
                    loc["lng"],
                    radius_m=min(seed.search_radius_m, seed.corridor_place_radius_m),
                    place_types=place_types,
                    max_results=quota,
                )
            )
        return collected

    def _geocode(self, seed: CitySeed, query: str) -> dict[str, Any]:
        self._respect_nominatim_rate_limit()
        params: dict[str, Any] = {
            "q": query,
            "format": "jsonv2",
            "limit": 1,
            "addressdetails": 1,
            "accept-language": seed.language,
        }
        if seed.bbox:
            params["viewbox"] = f"{seed.bbox['west']},{seed.bbox['north']},{seed.bbox['east']},{seed.bbox['south']}"
            params["bounded"] = 1
        rows = []
        for attempt in range(3):
            try:
                resp = self.session.get(NOMINATIM_URL, params=params, timeout=20)
                resp.raise_for_status()
                rows = resp.json()
                break
            except Exception:
                if attempt == 2:
                    return _empty_geocode()
                time.sleep(1.5 * (attempt + 1))
        if not rows:
            return _empty_geocode()
        row = rows[0]
        return {
            "formatted_address": row.get("display_name", ""),
            "address_components": row.get("address", {}),
            "place_id": f"osm:{row.get('osm_type')}:{row.get('osm_id')}",
            "types": [row.get("category", ""), row.get("type", "")],
            "location": {"latitude": float(row.get("lat")), "longitude": float(row.get("lon"))},
            "viewport": {},
        }

    def _nominatim_place_search(
        self,
        seed: CitySeed,
        query: str,
        *,
        max_results: int,
        fallback_type: str = "point_of_interest",
    ) -> list[dict[str, Any]]:
        self._respect_nominatim_rate_limit()
        params: dict[str, Any] = {
            "q": f"{query}, {seed.city_name}, {seed.country}",
            "format": "jsonv2",
            "limit": max_results,
            "addressdetails": 1,
            "accept-language": seed.language,
        }
        if seed.bbox:
            params["viewbox"] = f"{seed.bbox['west']},{seed.bbox['north']},{seed.bbox['east']},{seed.bbox['south']}"
            params["bounded"] = 1
        rows = []
        for attempt in range(3):
            try:
                resp = self.session.get(NOMINATIM_URL, params=params, timeout=20)
                resp.raise_for_status()
                rows = resp.json()
                break
            except Exception:
                if attempt == 2:
                    return []
                time.sleep(1.5 * (attempt + 1))
        places = []
        for row in rows:
            place = _nominatim_row_to_place(row, fallback_type)
            if place:
                places.append(place)
        return places

    def _respect_nominatim_rate_limit(self) -> None:
        with self._nominatim_lock:
            elapsed = time.time() - self._last_nominatim_at
            if elapsed < 1.05:
                time.sleep(1.05 - elapsed)
            self._last_nominatim_at = time.time()

    def _overpass_places(
        self,
        lat: float,
        lng: float,
        *,
        radius_m: int,
        place_type: str,
        max_results: int,
    ) -> list[dict[str, Any]]:
        filters = _osm_filters(place_type)
        if not filters:
            return []
        clauses = []
        for key, value in filters:
            selector = f'["{key}"]' if value == "*" else f'["{key}"="{value}"]'
            clauses.append(f"nwr(around:{int(radius_m)},{lat},{lng}){selector};")
        query = "[out:json][timeout:25];(" + "".join(clauses) + ");out center tags 80;"
        data = self._overpass(query)
        if not data:
            return []
        places = []
        for element in data.get("elements", []):
            place = _element_to_place(element, place_type)
            if place:
                places.append(place)
            if len(places) >= max_results:
                break
        return places

    def _overpass_places_for_types(
        self,
        lat: float,
        lng: float,
        *,
        radius_m: int,
        place_types: list[str],
        max_results: int,
    ) -> list[dict[str, Any]]:
        clauses = []
        requested_by_filter = {}
        for place_type in place_types:
            for key, value in _osm_filters(place_type):
                selector = f'["{key}"]' if value == "*" else f'["{key}"="{value}"]'
                clause = f"nwr(around:{int(radius_m)},{lat},{lng}){selector};"
                clauses.append(clause)
                requested_by_filter[(key, value)] = place_type
        if not clauses:
            return []
        query = "[out:json][timeout:25];(" + "".join(clauses) + ");out center tags 120;"
        data = self._overpass(query)
        if not data:
            return []
        places = []
        for element in data.get("elements", []):
            requested = _requested_type_for_element(element.get("tags") or {}, requested_by_filter) or "point_of_interest"
            place = _element_to_place(element, requested)
            if place:
                places.append(place)
            if len(places) >= max_results:
                break
        return places

    def _overpass(self, query: str) -> dict[str, Any]:
        for url in OVERPASS_URLS:
            try:
                resp = self.session.post(url, data={"data": query}, timeout=45)
                resp.raise_for_status()
                return resp.json()
            except Exception:
                continue
        return {}


def _osm_filters(place_type: str) -> list[tuple[str, str]]:
    value = PLACE_TYPE_ALIASES.get(place_type, place_type)
    mapping = {
        "restaurant": [("amenity", "restaurant"), ("amenity", "fast_food")],
        "cafe": [("amenity", "cafe")],
        "coffee_shop": [("amenity", "cafe")],
        "bar": [("amenity", "bar"), ("amenity", "pub")],
        "shopping_mall": [("shop", "mall"), ("building", "retail"), ("amenity", "marketplace")],
        "department_store": [("shop", "department_store")],
        "store": [("shop", "*")],
        "hotel": [("tourism", "hotel")],
        "lodging": [("tourism", "hotel"), ("tourism", "guest_house")],
        "tourist_attraction": [("tourism", "attraction")],
        "museum": [("tourism", "museum")],
        "movie_theater": [("amenity", "cinema")],
        "park": [("leisure", "park"), ("leisure", "garden")],
        "train_station": [("railway", "station")],
        "subway_station": [("railway", "station"), ("station", "subway")],
        "bus_station": [("amenity", "bus_station"), ("highway", "bus_stop")],
        "hospital": [("amenity", "hospital")],
        "medical_clinic": [("amenity", "clinic")],
        "doctor": [("amenity", "doctors")],
        "pharmacy": [("amenity", "pharmacy")],
        "university": [("amenity", "university")],
    }
    return mapping.get(value, []) if value in KNOWN_PLACE_TYPES else []


def _requested_type_for_element(tags: dict[str, Any], requested_by_filter: dict[tuple[str, str], str]) -> str | None:
    for key, value in requested_by_filter:
        tag_value = tags.get(key)
        if tag_value is None:
            continue
        if value == "*" or str(tag_value) == value:
            return requested_by_filter[(key, value)]
    return None


def _element_to_place(element: dict[str, Any], requested_type: str) -> dict[str, Any] | None:
    tags = element.get("tags") or {}
    name = tags.get("name") or tags.get("name:en") or tags.get("brand")
    if not name:
        return None
    center = element.get("center") or {}
    lat = element.get("lat") or center.get("lat")
    lng = element.get("lon") or center.get("lon")
    if lat is None or lng is None:
        return None
    primary_type = _primary_type_from_tags(tags, requested_type)
    osm_id = f"osm:{element.get('type')}:{element.get('id')}"
    return {
        "id": osm_id,
        "osm_id": osm_id,
        "displayName": {"text": name},
        "formattedAddress": tags.get("addr:full") or _address_from_tags(tags),
        "location": {"latitude": float(lat), "longitude": float(lng)},
        "primaryType": primary_type,
        "types": sorted({primary_type, requested_type, *(_types_from_tags(tags))}),
        "businessStatus": "OPERATIONAL",
        "rating": None,
        "userRatingCount": None,
    }


def _nominatim_row_to_place(row: dict[str, Any], fallback_type: str) -> dict[str, Any] | None:
    name = row.get("name") or row.get("display_name", "").split(",")[0]
    if not name or row.get("lat") is None or row.get("lon") is None:
        return None
    tags = {"amenity": row.get("type") if row.get("category") == "amenity" else None}
    primary_type = _primary_type_from_nominatim(row, fallback_type)
    osm_id = f"osm:{row.get('osm_type')}:{row.get('osm_id')}"
    return {
        "id": osm_id,
        "osm_id": osm_id,
        "displayName": {"text": name},
        "formattedAddress": row.get("display_name", ""),
        "location": {"latitude": float(row.get("lat")), "longitude": float(row.get("lon"))},
        "primaryType": primary_type,
        "types": sorted({primary_type, fallback_type, str(row.get("category", "")), str(row.get("type", ""))}),
        "businessStatus": "OPERATIONAL",
        "rating": None,
        "userRatingCount": None,
    }


def _primary_type_from_nominatim(row: dict[str, Any], fallback_type: str) -> str:
    category = str(row.get("category", ""))
    typ = str(row.get("type", ""))
    if category == "amenity":
        return _primary_type_from_tags({"amenity": typ}, fallback_type)
    if category == "tourism":
        return _primary_type_from_tags({"tourism": typ}, fallback_type)
    if category == "leisure":
        return _primary_type_from_tags({"leisure": typ}, fallback_type)
    if category == "shop":
        return _primary_type_from_tags({"shop": typ}, fallback_type)
    if category == "railway":
        return "train_station"
    return fallback_type if fallback_type in KNOWN_PLACE_TYPES else "point_of_interest"


def _primary_type_from_tags(tags: dict[str, Any], fallback: str) -> str:
    amenity = tags.get("amenity")
    tourism = tags.get("tourism")
    leisure = tags.get("leisure")
    shop = tags.get("shop")
    railway = tags.get("railway")
    if amenity == "fast_food":
        return "fast_food_restaurant"
    if amenity == "pub":
        return "bar"
    if amenity == "cinema":
        return "movie_theater"
    if amenity == "clinic":
        return "medical_clinic"
    if amenity == "doctors":
        return "doctor"
    if amenity in {"restaurant", "cafe", "bar", "hospital", "pharmacy", "university"}:
        return amenity
    if tourism == "hotel":
        return "hotel"
    if tourism == "guest_house":
        return "lodging"
    if tourism in {"museum", "attraction"}:
        return "museum" if tourism == "museum" else "tourist_attraction"
    if leisure in {"park", "garden"}:
        return "park"
    if railway == "station":
        return "train_station"
    if shop == "department_store":
        return "department_store"
    if shop == "mall":
        return "shopping_mall"
    if shop:
        return "store"
    return fallback


def _types_from_tags(tags: dict[str, Any]) -> list[str]:
    out = []
    for key in ("amenity", "tourism", "leisure", "shop", "railway", "station"):
        if tags.get(key):
            out.append(str(tags[key]))
    return out


def _address_from_tags(tags: dict[str, Any]) -> str:
    parts = [tags.get(k) for k in ("addr:housenumber", "addr:street", "addr:city")]
    return " ".join(str(p) for p in parts if p)


def _location_or_candidate(seed: CitySeed, geocode: dict[str, Any], candidate: dict[str, Any]) -> dict[str, float]:
    loc = _location_from_geocode(geocode)
    if loc:
        return loc
    approx = candidate.get("approx_center") or {}
    if approx.get("lat") is not None and approx.get("lng") is not None:
        return {"lat": float(approx["lat"]), "lng": float(approx["lng"])}
    return {"lat": seed.lat, "lng": seed.lng}


def _location_from_geocode(geocode: dict[str, Any]) -> dict[str, float] | None:
    loc = geocode.get("location") or {}
    if loc.get("latitude") is None or loc.get("longitude") is None:
        return None
    return {"lat": float(loc["latitude"]), "lng": float(loc["longitude"])}


_OSM_CORRIDOR_TYPE_TO_MODE = {
    "rail_corridor": "metro",
    "bus_corridor": "bus",
    "road_corridor": "drive",
    "pedestrian_corridor": "walk",
    "commercial_axis": "walk",
    "waterfront_axis": "walk",
    "airport_access": "drive",
    "intermodal_corridor": "mixed",
}


def _osm_default_mode_for(corridor_type: str) -> str:
    return _OSM_CORRIDOR_TYPE_TO_MODE.get(corridor_type, "mixed")


def _route_summary(nodes: list[dict[str, Any]], corridor_type: str) -> dict[str, Any]:
    if len(nodes) < 2:
        return {}
    distance = 0.0
    for a, b in zip(nodes, nodes[1:]):
        distance += _haversine_m(a["location"], b["location"])
    speed_mps = 10.0 if any(token in corridor_type for token in ["rail", "airport"]) else 6.0
    if any(token in corridor_type for token in ["pedestrian", "commercial"]):
        speed_mps = 1.3
    return {
        "distance_m": int(distance),
        "duration_s": int(distance / speed_mps) if speed_mps else 0,
        "summary": "OSM approximate route summary from geocoded corridor nodes",
        "steps": [],
    }


def _haversine_m(a: dict[str, float], b: dict[str, float]) -> float:
    r = 6371000.0
    lat1, lng1 = math.radians(float(a["lat"])), math.radians(float(a["lng"]))
    lat2, lng2 = math.radians(float(b["lat"])), math.radians(float(b["lng"]))
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    x = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


def _empty_geocode() -> dict[str, Any]:
    return {"formatted_address": "", "address_components": {}, "place_id": "", "types": [], "location": {}, "viewport": {}}


def _parallel_map(items: list[Any], fn: Any, max_workers: int) -> list[Any]:
    if not items:
        return []
    workers = max(1, min(max_workers, len(items)))
    if workers == 1:
        return [fn(item) for item in items]
    results: list[Any] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_index = {executor.submit(fn, item): index for index, item in enumerate(items)}
        for future in as_completed(future_to_index):
            results[future_to_index[future]] = future.result()
    return results


def _sample_names(names: list[str], limit: int) -> list[str]:
    names = [name for name in names if name]
    if len(names) <= limit:
        return names
    indexes = sorted({0, len(names) - 1, len(names) // 2, len(names) // 3})[:limit]
    return [names[i] for i in indexes]


def _primary_type(place: dict[str, Any]) -> str:
    return place.get("primaryType") or (place.get("types") or [""])[0]


def _place_summary(place: dict[str, Any]) -> dict[str, Any]:
    loc = place.get("location") or {}
    name = place.get("displayName") or {}
    osm_id = place.get("osm_id") or place.get("id", "")
    return {
        "google_place_id": "",
        "source_place_id": osm_id,
        "osm_id": osm_id,
        "name": name.get("text", "") if isinstance(name, dict) else str(name),
        "address": place.get("formattedAddress", ""),
        "location": {
            "lat": float(loc.get("latitude", 0.0)),
            "lng": float(loc.get("longitude", 0.0)),
        },
        "primary_type": _primary_type(place),
        "types": place.get("types", []),
        "business_status": place.get("businessStatus", ""),
        "rating": place.get("rating"),
        "user_rating_count": place.get("userRatingCount"),
        "map_provider": "osm",
    }
