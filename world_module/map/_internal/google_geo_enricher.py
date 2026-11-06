"""Google Maps enrichment for discovered city skeleton candidates."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import ceil
from typing import Any

import requests

from shared.config_loader import PipelineConfig
from shared.maps_client import GoogleMapsClient
from world_module.map.seed import CitySeed


ANCHOR_TYPES = [
    "train_station",
    "subway_station",
    "shopping_mall",
    "tourist_attraction",
    "park",
    "university",
    "hospital",
]

PLACE_TYPE_ALIASES = {
    "clinic": "medical_clinic",
    "clinics": "medical_clinic",
    "medical": "medical_clinic",
    "retail": "store",
    "retail_pickup": "store",
    "pickup": "store",
    "shopping": "shopping_mall",
    "mall": "shopping_mall",
    "coffee": "coffee_shop",
    "station": "train_station",
    "rail_station": "train_station",
    "subway": "subway_station",
    "tourism": "tourist_attraction",
    "attraction": "tourist_attraction",
    "theater": "movie_theater",
    "nightlife": "bar",
}

KNOWN_PLACE_TYPES = {
    "restaurant", "cafe", "coffee_shop", "bar", "shopping_mall",
    "department_store", "store", "hotel", "lodging", "tourist_attraction",
    "museum", "movie_theater", "park", "train_station", "subway_station",
    "bus_station", "hospital", "medical_clinic", "doctor", "pharmacy",
    "university",
}


class GoogleGeoEnricher:
    def __init__(self, config: PipelineConfig | None = None):
        self.maps = GoogleMapsClient(config=config)
        self._place_details_report: dict[str, Any] = {
            "status": "not_run",
            "policy": "all_retained_google_place_ids",
            "requested_fields": list(_PLACE_DETAILS_FIELDS),
            "expected_place_ids": 0,
            "status_counts": {},
        }

    def enrich(self, seed: CitySeed, discovery: dict[str, Any], *, attach_place_details: bool = True) -> dict[str, Any]:
        districts = self._enrich_districts(seed, discovery.get("district_candidates", []))
        corridors = self._enrich_corridors(seed, discovery.get("corridor_candidates", []))
        places, candidate_collection = self._enrich_places(
            seed,
            discovery.get("place_search_plan", []),
            districts,
            corridors,
            attach_place_details=attach_place_details,
        )
        return {
            "seed": seed.__dict__,
            "discovery": discovery,
            "districts": districts,
            "corridors": corridors,
            "places": places,
            "poi_candidate_collection": candidate_collection,
            "google_place_details_report": self.place_details_report(),
            "external_service_telemetry": self.maps.telemetry_snapshot(),
        }

    def place_details_report(self) -> dict[str, Any]:
        return {**self._place_details_report, "status_counts": dict(self._place_details_report.get("status_counts", {}))}

    def _enrich_districts(self, seed: CitySeed, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _parallel_map(
            candidates,
            lambda candidate: self._enrich_one_district(seed, candidate),
            max_workers=seed.google_max_workers,
        )

    def _enrich_one_district(self, seed: CitySeed, candidate: dict[str, Any]) -> dict[str, Any]:
            name = candidate.get("name", "")
            geocode = self.maps.geocode(
                f"{name}, {seed.city_name}, {seed.country}",
                language=seed.language,
                bounds=seed.bbox,
            )
            loc = _location_or_candidate(seed, geocode, candidate)
            anchors: list[dict[str, Any]] = []
            for ptype in ANCHOR_TYPES:
                anchors.extend(
                    self._nearby_search(
                        loc["lat"],
                        loc["lng"],
                        radius_m=min(seed.search_radius_m, 2200),
                        included_types=[ptype],
                        max_results=3,
                        language=seed.language,
                    )
                )
            anchors = _dedupe_places(anchors)[:18]
            type_counts = Counter(_primary_type(p) for p in anchors if _primary_type(p))
            return {
                **candidate,
                "google_geocode": geocode,
                "center": loc,
                "anchor_places": [_place_summary(p) for p in anchors],
                "poi_type_counts": dict(type_counts),
                "evidence": {
                    "llm_evidence_urls": candidate.get("evidence_urls", []),
                    "google_geocode_place_id": geocode.get("place_id", ""),
                    "google_anchor_count": len(anchors),
                },
            }

    def _enrich_corridors(self, seed: CitySeed, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _parallel_map(
            candidates,
            lambda candidate: self._enrich_one_corridor(seed, candidate),
            max_workers=seed.google_max_workers,
        )

    def _enrich_one_corridor(self, seed: CitySeed, candidate: dict[str, Any]) -> dict[str, Any]:
            nodes = []
            for node_name in candidate.get("node_names", [])[:10]:
                geocode = self.maps.geocode(
                    f"{node_name}, {seed.city_name}, {seed.country}",
                    language=seed.language,
                    bounds=seed.bbox,
                )
                loc = _location_from_geocode(geocode)
                if loc:
                    nodes.append(
                        {
                            "name": node_name,
                            "location": loc,
                            "google_place_id": geocode.get("place_id", ""),
                            "formatted_address": geocode.get("formatted_address", ""),
                        }
                    )
            nodes = _filter_nodes_to_bbox(seed, nodes)

            corridor_class = (candidate.get("corridor_class") or "transport_corridor").strip()
            travel_mode = _routes_travel_mode(candidate)

            # Per-segment Routes API calls give us a real polyline.
            segment_routes: list[dict[str, Any]] = []
            verified_polyline: list[dict[str, float]] = []
            total_distance_m = 0
            total_duration_s = 0
            verified_segments = 0
            for i in range(len(nodes) - 1):
                a = nodes[i]["location"]
                b = nodes[i + 1]["location"]
                segment = self._compute_segment(a, b, travel_mode, candidate.get("name", ""))
                segment_summary = {
                    "from_node": nodes[i]["name"],
                    "to_node": nodes[i + 1]["name"],
                    "travel_mode": travel_mode,
                    "distance_m": segment.get("distance_m", 0),
                    "duration_s": segment.get("duration_s", 0),
                    "is_verified": bool(segment.get("polyline_points")),
                }
                segment_routes.append(segment_summary)
                seg_points = segment.get("polyline_points") or []
                if seg_points:
                    verified_segments += 1
                    if verified_polyline and seg_points:
                        # Avoid duplicating shared endpoints between segments.
                        last = verified_polyline[-1]
                        if (
                            abs(last.get("lat", 0) - seg_points[0].get("lat", 0)) < 1e-6
                            and abs(last.get("lng", 0) - seg_points[0].get("lng", 0)) < 1e-6
                        ):
                            seg_points = seg_points[1:]
                    verified_polyline.extend(seg_points)
                total_distance_m += int(segment.get("distance_m", 0) or 0)
                total_duration_s += int(segment.get("duration_s", 0) or 0)

            is_route_verified = (
                len(nodes) >= 2
                and verified_segments == len(nodes) - 1
                and bool(verified_polyline)
            )

            route_geometry = {
                "provider": "google_routes" if verified_polyline else "geocoded_node_polyline",
                "travel_mode": travel_mode,
                "is_route_verified": is_route_verified,
                "polyline_points": verified_polyline if verified_polyline else [n["location"] for n in nodes],
                "segment_count": len(segment_routes),
                "verified_segment_count": verified_segments,
                "total_distance_m": total_distance_m,
                "total_duration_s": total_duration_s,
                "segments": segment_routes,
            }

            # route_summary kept for backward compatibility with old consumers.
            route_summary = {
                "distance_m": total_distance_m,
                "duration_s": total_duration_s,
                "polyline_points": route_geometry["polyline_points"],
            }

            return {
                **candidate,
                "corridor_class": corridor_class,
                "mode": (candidate.get("mode") or _default_mode_for(candidate.get("corridor_type", ""))).strip(),
                "nodes": nodes,
                "route_summary": route_summary,
                "route_geometry": route_geometry,
                "evidence": {
                    "llm_evidence_urls": candidate.get("evidence_urls", []),
                    "google_node_count": len(nodes),
                    "google_route_distance_m": total_distance_m,
                    "google_route_duration_s": total_duration_s,
                    "google_route_verified_segments": verified_segments,
                    "google_route_total_segments": len(segment_routes),
                },
            }

    def _compute_segment(
        self,
        a: dict[str, float],
        b: dict[str, float],
        travel_mode: str,
        corridor_name: str,
    ) -> dict[str, Any]:
        """Best-effort Routes API call with TRANSIT->DRIVE fallback."""
        try:
            result = self.maps.compute_route(
                a["lat"], a["lng"], b["lat"], b["lng"], travel_mode=travel_mode,
            ) or {}
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in {401, 403}:
                raise
            result = {}
        except Exception:
            result = {}
        if not result.get("polyline_points") and travel_mode != "DRIVE":
            try:
                result = self.maps.compute_route(
                    a["lat"], a["lng"], b["lat"], b["lng"], travel_mode="DRIVE",
                ) or {}
                if result.get("polyline_points"):
                    result["fallback_mode"] = "DRIVE"
            except Exception:
                pass
        return result or {}

    def _enrich_places(
        self,
        seed: CitySeed,
        search_plan: list[dict[str, Any]],
        districts: list[dict[str, Any]],
        corridors: list[dict[str, Any]],
        *,
        attach_place_details: bool,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Collect a bounded, geographically distributed provisional POI pool.

        Google search results are already geocoded, so "qualified" at this
        point means a non-empty provider id inside the requested bbox and
        de-duplicated against earlier search results.  The later quality gate
        remains the authority for stricter address/geometry checks.
        """
        by_name = {d.get("name"): d for d in districts}
        district_jobs = []
        for plan in search_plan:
            district = by_name.get(plan.get("district_name"))
            if not district:
                continue
            district_jobs.append((plan, district))
        budget = seed.poi_candidate_budget
        # Reserve a small lane for corridor service bands.  This keeps the map
        # from being filled entirely by district-centre hits before corridors
        # receive a chance to contribute their required POIs.
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

        corridor_collected = self._enrich_corridor_places(
            seed,
            corridors,
            candidate_limit=corridor_budget,
        )
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
        summaries = [_place_summary(p) for p in eligible]
        summaries = self._attach_place_details(seed, summaries) if attach_place_details else summaries
        return summaries, report

    def attach_place_details(self, seed: CitySeed, places: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Enrich the final retained POI set, never the raw candidate pool."""
        return self._attach_place_details(seed, places)

    def collect_targeted_place_supplement(
        self,
        seed: CitySeed,
        districts: list[dict[str, Any]],
        corridors: list[dict[str, Any]],
        accepted_places: list[dict[str, Any]],
        *,
        attempted_types: set[str],
        round_number: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Collect one bounded, Google-only POI top-up batch.

        This is deliberately separate from the normal 1.20--1.25 candidate
        collection. It is used only after the factual quality filter proves a
        formal POI target is short. Results are still filtered and deduplicated
        by the caller before they can become part of a candidate world.
        """
        target = seed.formal_place_target
        accepted_ids = {_source_place_identity(place) for place in accepted_places}
        accepted_ids.discard("")
        missing = max(0, target - len(accepted_places))
        if not missing:
            return [], {"round": round_number, "status": "not_needed", "missing_before": 0}

        normalized_attempted = {_normalize_place_type(value) for value in attempted_types}
        untried_types = [
            normalized for raw in seed.place_types
            if (normalized := _normalize_place_type(raw)) and normalized not in normalized_attempted
        ]
        # Discovery plans can exhaust every requested type. A second targeted
        # pass may revisit them at different anchors, but never broadens the
        # user-selected type policy.
        reused_types = False
        if not untried_types:
            untried_types = [
                normalized for raw in seed.place_types
                if (normalized := _normalize_place_type(raw))
            ]
            reused_types = True
        untried_types = list(dict.fromkeys(untried_types))
        if not untried_types:
            return [], {
                "round": round_number,
                "status": "no_searchable_types",
                "missing_before": missing,
                "accepted_source_ids": len(accepted_ids),
            }

        anchors = [district.get("center") for district in districts if district.get("center")]
        for corridor in corridors:
            for node in _sample_corridor_nodes(corridor.get("nodes", []), limit=1):
                if node.get("location"):
                    anchors.append(node["location"])
        if not anchors:
            anchors = [seed.center]

        # A top-up only asks for the remaining deficit plus the established
        # bounded buffer. It is not a second unbounded discovery pass.
        request_budget = max(missing, ceil(missing * 1.25))
        raw: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        jobs = [(anchor, ptype) for anchor in anchors for ptype in untried_types]
        for index, (anchor, ptype) in enumerate(jobs):
            eligible = _eligible_places(seed, raw)
            novel_count = len([
                place for place in eligible if _source_place_identity(place) not in accepted_ids
            ])
            if novel_count >= request_budget:
                break
            remaining_jobs = len(jobs) - index
            quota = max(1, (request_budget - novel_count + remaining_jobs - 1) // remaining_jobs)
            results = self._nearby_search(
                anchor["lat"],
                anchor["lng"],
                radius_m=seed.search_radius_m,
                included_types=[ptype],
                max_results=max(1, min(8, quota)),
                language=seed.language,
            )
            raw.extend(results)
            calls.append({"type": ptype, "anchor": {"lat": anchor["lat"], "lng": anchor["lng"]}, "requested": quota, "returned": len(results)})

        eligible = _eligible_places(seed, raw)
        supplemental = [place for place in eligible if _source_place_identity(place) not in accepted_ids]
        return [_place_summary(place) for place in supplemental], {
            "round": round_number,
            "status": "completed",
            "missing_before": missing,
            "request_budget": request_budget,
            "attempted_types": untried_types,
            "reused_types_at_new_anchors": reused_types,
            "accepted_source_ids": len(accepted_ids),
            "raw_candidates_returned": len(raw),
            "eligible_new_candidates": len(supplemental),
            "search_calls": calls,
        }

    def _search_district_places(
        self,
        seed: CitySeed,
        plan: dict[str, Any],
        district: dict[str, Any],
        *,
        candidate_limit: int,
    ) -> list[dict[str, Any]]:
        center = district["center"]
        collected: list[dict[str, Any]] = []
        for ptype in plan.get("included_types", seed.place_types)[:10]:
            remaining = candidate_limit - len(_eligible_places(seed, collected))
            if remaining <= 0:
                break
            normalized_type = _normalize_place_type(ptype)
            if not normalized_type:
                continue
            collected.extend(
                self._nearby_search(
                    center["lat"],
                    center["lng"],
                    radius_m=seed.search_radius_m,
                    included_types=[normalized_type],
                    max_results=max(1, min(max(2, seed.places_per_district // 6), remaining)),
                    language=seed.language,
                )
            )
        for query in plan.get("text_queries", [])[:5]:
            remaining = candidate_limit - len(_eligible_places(seed, collected))
            if remaining <= 0:
                break
            collected.extend(
                self._text_search(
                    query,
                    lat=center["lat"],
                    lng=center["lng"],
                    radius_m=seed.search_radius_m * 2,
                    max_results=max(1, min(4, remaining)),
                    language=seed.language,
                )
            )
        return collected

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
            service_bands = corridor.get("corridor_service_bands") or []
            nodes = corridor.get("nodes", [])
            if not nodes:
                continue
            sampled_nodes = _sample_corridor_nodes(nodes, limit=seed.corridor_node_sample_limit)
            fallback_types = _corridor_default_types(corridor.get("corridor_type", ""))
            jobs.extend(
                (corridor, node, band, fallback_types)
                for node in sampled_nodes
                for band in (service_bands[:seed.corridor_band_limit] or [{"place_types": fallback_types, "text_queries": []}])
            )
        for index, job in enumerate(jobs):
            remaining_jobs = len(jobs) - index
            current = len(_eligible_places(seed, collected))
            quota = max(1, (max(0, candidate_limit - current) + remaining_jobs - 1) // remaining_jobs)
            if current >= candidate_limit:
                break
            collected.extend(self._search_corridor_band_places(seed, *job, candidate_limit=quota))
        return collected

    def _search_corridor_band_places(
        self,
        seed: CitySeed,
        corridor: dict[str, Any],
        node: dict[str, Any],
        band: dict[str, Any],
        fallback_types: list[str],
        *,
        candidate_limit: int,
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        loc = node["location"]
        density = band.get("density_expectation", "medium")
        max_results = {"sparse": 2, "medium": 3, "dense": 4}.get(density, 3)
        for ptype in (band.get("place_types") or fallback_types)[: seed.corridor_place_types_per_band]:
            remaining = candidate_limit - len(_eligible_places(seed, collected))
            if remaining <= 0:
                break
            normalized_type = _normalize_place_type(ptype)
            if not normalized_type:
                continue
            collected.extend(
                self._nearby_search(
                    loc["lat"],
                    loc["lng"],
                    radius_m=min(seed.search_radius_m, seed.corridor_place_radius_m),
                    included_types=[normalized_type],
                    max_results=max(1, min(max_results, remaining)),
                    language=seed.language,
                )
            )
        for query in band.get("text_queries", [])[: seed.corridor_text_queries_per_band]:
            remaining = candidate_limit - len(_eligible_places(seed, collected))
            if remaining <= 0:
                break
            collected.extend(
                self._text_search(
                    f"{query} near {node['name']}, {seed.city_name}",
                    lat=loc["lat"],
                    lng=loc["lng"],
                    radius_m=min(seed.search_radius_m * 2, seed.corridor_place_radius_m * 2),
                    max_results=max(1, min(max_results, remaining)),
                    language=seed.language,
                )
            )
        return collected

    def _attach_place_details(self, seed: CitySeed, places: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach the same factual Google Details field set to every retained POI."""
        reviews_per_place = max(0, int(getattr(seed, "google_reviews_per_place", 3) or 0))
        candidates = [place for place in places if place.get("google_place_id")]
        results = _parallel_map(
            candidates,
            lambda place: self._place_details_result(place.get("google_place_id", ""), seed.language),
            max_workers=seed.google_max_workers,
        )
        details_by_id = {result["place_id"]: result for result in results if result.get("place_id")}
        status_counts: dict[str, int] = {}
        for result in results:
            status = str(result.get("status") or "fetch_failed")
            status_counts[status] = status_counts.get(status, 0) + 1
        self._place_details_report = {
            "status": "completed",
            "policy": "all_retained_google_place_ids",
            "requested_fields": list(_PLACE_DETAILS_FIELDS),
            "expected_place_ids": len(candidates),
            "status_counts": status_counts,
            "legacy_google_place_details_limit_ignored": int(getattr(seed, "google_place_details_limit", 0) or 0),
            "reviews_per_place": reviews_per_place,
        }

        enriched = []
        for place in places:
            place_id = place.get("google_place_id", "")
            if not place_id:
                enriched.append(_with_details_status(place, "no_google_place_id"))
                continue
            result = details_by_id.get(place_id, {"status": "fetch_failed", "place_id": place_id})
            if result.get("status") != "queried":
                enriched.append(_with_details_status(place, str(result.get("status") or "fetch_failed")))
                continue
            enriched.append(_merge_place_details(place, result.get("detail") or {}, reviews_per_place))
        return enriched

    def _place_details_result(self, place_id: str, language: str) -> dict[str, Any]:
        if not place_id:
            return {"place_id": "", "status": "no_google_place_id"}
        try:
            detail = self.maps.place_details(
                place_id,
                fields=_PLACE_DETAILS_FIELDS,
                language=language,
            )
            return {"place_id": place_id, "status": "queried", "detail": detail}
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in {401, 403}:
                raise
            return {"place_id": place_id, "status": "fetch_failed", "error_type": type(exc).__name__}
        except Exception as exc:
            return {"place_id": place_id, "status": "fetch_failed", "error_type": type(exc).__name__}

    def _nearby_search(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        try:
            return self.maps.nearby_search(*args, **kwargs)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in {401, 403}:
                raise
            return []
        except Exception:
            return []

    def _text_search(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        try:
            return self.maps.text_search(*args, **kwargs)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in {401, 403}:
                raise
            return []
        except Exception:
            return []


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


_MODE_TO_ROUTES_API = {
    "metro": "TRANSIT",
    "rail": "TRANSIT",
    "bus": "TRANSIT",
    "ferry": "TRANSIT",
    "walk": "WALK",
    "drive": "DRIVE",
    "mixed": "DRIVE",
}

_CORRIDOR_TYPE_DEFAULT_MODE = {
    "rail_corridor": "metro",
    "bus_corridor": "bus",
    "road_corridor": "drive",
    "pedestrian_corridor": "walk",
    "commercial_axis": "walk",
    "waterfront_axis": "walk",
    "airport_access": "drive",
    "intermodal_corridor": "mixed",
}


def _default_mode_for(corridor_type: str) -> str:
    return _CORRIDOR_TYPE_DEFAULT_MODE.get(corridor_type, "mixed")


def _routes_travel_mode(candidate: dict[str, Any]) -> str:
    """Pick the Google Routes travelMode from a corridor candidate."""
    mode = (candidate.get("mode") or _default_mode_for(candidate.get("corridor_type", ""))).strip().lower()
    return _MODE_TO_ROUTES_API.get(mode, "DRIVE")


def _dedupe_places(places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for place in places:
        pid = place.get("id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        out.append(place)
    return out


def _eligible_places(seed: CitySeed, places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Provider-qualified candidates used to stop additional POI searches."""
    excluded = {str(value).strip().casefold() for value in seed.excluded_place_ids if str(value).strip()}
    eligible = _dedupe_places(_filter_places_to_bbox(seed, places))
    if not excluded:
        return eligible
    return [place for place in eligible if _source_place_identity(place) not in excluded]


def _source_place_identity(place: dict[str, Any]) -> str:
    return str(
        place.get("id")
        or place.get("google_place_id")
        or place.get("provider_place_id")
        or place.get("source_place_id")
        or place.get("osm_id")
        or ""
    ).strip().casefold()


def _candidate_collection_report(
    seed: CitySeed,
    *,
    budget: int,
    district_budget: int,
    corridor_budget: int,
    raw_candidates: int,
    eligible_candidates: int,
) -> dict[str, Any]:
    return {
        "formal_target": seed.formal_place_target,
        "candidate_multiplier": min(1.25, max(1.20, float(seed.poi_candidate_multiplier or 1.25))),
        "candidate_budget": budget,
        "district_candidate_budget": district_budget,
        "corridor_candidate_budget": corridor_budget,
        "raw_candidates_returned": raw_candidates,
        "eligible_deduplicated_candidates": eligible_candidates,
        "stopped_at_candidate_budget": eligible_candidates >= budget,
        "in_flight_overshoot": max(0, eligible_candidates - budget),
        "excluded_existing_source_ids": len(seed.excluded_place_ids),
        "qualification_boundary": "provider id, in seed bbox, and de-duplicated; later place-quality gate remains authoritative",
    }


def _merge_place_details(place: dict[str, Any], detail: dict[str, Any], reviews_per_place: int) -> dict[str, Any]:
    merged = {**place}
    name = detail.get("displayName")
    loc = detail.get("location") or {}
    if name:
        merged["name"] = _localized_text(name)
    if detail.get("formattedAddress"):
        merged["address"] = detail["formattedAddress"]
    if loc.get("latitude") is not None and loc.get("longitude") is not None:
        merged["location"] = {"lat": float(loc["latitude"]), "lng": float(loc["longitude"])}
    if detail.get("primaryType"):
        merged["primary_type"] = detail["primaryType"]
    if detail.get("types"):
        merged["types"] = detail["types"]
    if detail.get("businessStatus"):
        merged["business_status"] = detail["businessStatus"]
    if detail.get("rating") is not None:
        merged["rating"] = detail["rating"]
    if detail.get("userRatingCount") is not None:
        merged["user_rating_count"] = detail["userRatingCount"]

    merged["google_place_details"] = {
        "details_enriched": True,
        "details_status": "queried",
        "website_uri": detail.get("websiteUri", ""),
        "national_phone_number": detail.get("nationalPhoneNumber", ""),
        "current_opening_hours": detail.get("currentOpeningHours", {}),
        "regular_opening_hours": detail.get("regularOpeningHours", {}),
        "price_level": detail.get("priceLevel"),
        "price_range": detail.get("priceRange", {}),
        "reservable": detail.get("reservable"),
        "dine_in": detail.get("dineIn"),
        "takeout": detail.get("takeout"),
        "delivery": detail.get("delivery"),
        "curbside_pickup": detail.get("curbsidePickup"),
        "field_status": {
            "website_uri": _field_status(detail, "websiteUri"),
            "national_phone_number": _field_status(detail, "nationalPhoneNumber"),
            "current_opening_hours": _field_status(detail, "currentOpeningHours"),
            "regular_opening_hours": _field_status(detail, "regularOpeningHours"),
            "price_level": _field_status(detail, "priceLevel"),
            "price_range": _field_status(detail, "priceRange"),
            "reservable": _field_status(detail, "reservable"),
            "dine_in": _field_status(detail, "dineIn"),
            "takeout": _field_status(detail, "takeout"),
            "delivery": _field_status(detail, "delivery"),
            "curbside_pickup": _field_status(detail, "curbsidePickup"),
        },
        "reviews": _review_summaries(detail.get("reviews", []), reviews_per_place),
    }
    return merged


_PLACE_DETAILS_FIELDS = [
    "id", "displayName", "formattedAddress", "location", "types", "primaryType",
    "businessStatus", "rating", "userRatingCount", "currentOpeningHours",
    "regularOpeningHours", "websiteUri", "nationalPhoneNumber", "priceLevel",
    "priceRange", "reservable", "dineIn", "takeout", "delivery", "curbsidePickup", "reviews",
]


def _with_details_status(place: dict[str, Any], status: str) -> dict[str, Any]:
    merged = {**place}
    merged["google_place_details"] = {
        "details_enriched": False,
        "details_status": status,
        "field_status": {},
        "reviews": [],
    }
    return merged


def _field_status(detail: dict[str, Any], field: str) -> str:
    return "provided" if field in detail else "not_provided"


def _review_summaries(reviews: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    out = []
    for index, review in enumerate(reviews[:limit]):
        author = review.get("authorAttribution") or {}
        text_obj = review.get("text") or review.get("originalText") or {}
        out.append(
            {
                "review_id": review.get("name") or f"review_{index + 1}",
                "text": _localized_text(text_obj),
                "original_text": _localized_text(review.get("originalText") or {}),
                "language_code": text_obj.get("languageCode") if isinstance(text_obj, dict) else "",
                "rating": review.get("rating"),
                "relative_publish_time": review.get("relativePublishTimeDescription", ""),
                "publish_time": review.get("publishTime", ""),
                "author_display_name": author.get("displayName", ""),
                "author_uri": author.get("uri", ""),
            }
        )
    return [review for review in out if review.get("text") or review.get("original_text")]


def _localized_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text", ""))
    return str(value or "")


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


def _sample_corridor_nodes(nodes: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(nodes) <= limit:
        return nodes
    indexes = sorted({0, len(nodes) - 1, len(nodes) // 2, len(nodes) // 3, (len(nodes) * 2) // 3})
    return [nodes[i] for i in indexes[:limit]]


def _filter_places_to_bbox(seed: CitySeed, places: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Clip POIs to the seed bbox when one is provided.

    Google bounds bias search but do not strictly limit it, so corridor searches
    can otherwise pull airport/rail clusters outside a central-city seed.
    """
    if not seed.bbox:
        return places
    return [place for place in places if _location_in_bbox(_google_location(place), seed.bbox)]


def _filter_nodes_to_bbox(seed: CitySeed, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not seed.bbox:
        return nodes
    nodes = [node for node in nodes if _location_in_bbox(node.get("location") or {}, seed.bbox)]
    return _filter_outlier_nodes(nodes)


def _filter_outlier_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove nodes that are geometrically inconsistent with their neighbors.

    Strategy: first compute the "expected segment length" as the total
    first-last distance divided by (n-1). A middle node is dropped if its
    distance from BOTH neighbors exceeds 3x the expected segment length AND
    is more than 4 km. This catches LLM hallucinated node_names that get
    geocoded to a far-away location.
    """
    if len(nodes) <= 2:
        return nodes
    from shared.maps_client import GoogleMapsClient
    first = nodes[0].get("location") or {}
    last = nodes[-1].get("location") or {}
    total_span = GoogleMapsClient.haversine_distance(
        first.get("lat", 0), first.get("lng", 0),
        last.get("lat", 0), last.get("lng", 0),
    )
    expected_seg = total_span / max(1, len(nodes) - 1)
    outlier_threshold = max(4000.0, expected_seg * 3)

    kept = [nodes[0]]
    for i in range(1, len(nodes) - 1):
        loc = nodes[i].get("location") or {}
        prev_loc = nodes[i - 1].get("location") or {}
        next_loc = nodes[i + 1].get("location") or {}
        dist_prev = GoogleMapsClient.haversine_distance(
            prev_loc.get("lat", 0), prev_loc.get("lng", 0),
            loc.get("lat", 0), loc.get("lng", 0),
        )
        dist_next = GoogleMapsClient.haversine_distance(
            loc.get("lat", 0), loc.get("lng", 0),
            next_loc.get("lat", 0), next_loc.get("lng", 0),
        )
        if dist_prev > outlier_threshold and dist_next > outlier_threshold:
            continue
        kept.append(nodes[i])
    kept.append(nodes[-1])
    return kept


def _google_location(place: dict[str, Any]) -> dict[str, float]:
    loc = place.get("location") or {}
    if loc.get("lat") is not None and loc.get("lng") is not None:
        return {"lat": float(loc["lat"]), "lng": float(loc["lng"])}
    if loc.get("latitude") is not None and loc.get("longitude") is not None:
        return {"lat": float(loc["latitude"]), "lng": float(loc["longitude"])}
    return {}


def _location_in_bbox(location: dict[str, float], bbox: dict[str, float]) -> bool:
    if location.get("lat") is None or location.get("lng") is None:
        return False
    return (
        float(bbox["south"]) <= float(location["lat"]) <= float(bbox["north"])
        and float(bbox["west"]) <= float(location["lng"]) <= float(bbox["east"])
    )


def _corridor_default_types(corridor_type: str) -> list[str]:
    if corridor_type in {"road_corridor", "commercial_axis", "pedestrian_corridor"}:
        return ["restaurant", "cafe", "shopping_mall", "store", "hotel"]
    if corridor_type in {"waterfront_axis"}:
        return ["tourist_attraction", "restaurant", "cafe", "park", "hotel"]
    if corridor_type in {"airport_access", "intermodal_corridor", "rail_corridor", "bus_corridor"}:
        return ["train_station", "subway_station", "restaurant", "cafe", "hotel", "shopping_mall"]
    return ["restaurant", "cafe", "shopping_mall", "tourist_attraction"]


def _normalize_place_type(place_type: str) -> str | None:
    value = str(place_type or "").lower().strip().replace(" ", "_").replace("-", "_")
    value = PLACE_TYPE_ALIASES.get(value, value)
    return value if value in KNOWN_PLACE_TYPES else None


def _primary_type(place: dict[str, Any]) -> str:
    return place.get("primaryType") or (place.get("types") or [""])[0]


def _place_summary(place: dict[str, Any]) -> dict[str, Any]:
    loc = place.get("location") or {}
    name = place.get("displayName") or {}
    return {
        "google_place_id": place.get("id", ""),
        "source_place_id": place.get("id", ""),
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
        "map_provider": "google",
    }
