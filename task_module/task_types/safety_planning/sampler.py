"""Evidence-first sampler for static safety-aware planning.

Static safety tasks must be grounded in observable base-world state.  Dynamic
weather, closures, and traffic disruptions belong to Persistent Dynamic
Replanning and are never invented here.
"""

from __future__ import annotations

import math
import random
from typing import Any

from task_module.task_types.safety_planning.static_app_state import eligible_risk_families
from task_module.task_types.route_planning.sampler import RouteSampler


SERVICE_CATEGORY = {
    "attraction_visit": "attraction", "outdoor_visit": "attraction",
    "restaurant_reservation": "restaurant", "local_visit": "cafe",
    "retail_visit": "personal_errand", "healthcare_visit": "appointment",
}


class SafetySampler:
    def __init__(self, world: dict[str, Any], characters: dict[str, Any], route_cache: Any,
                 rng: random.Random | None = None,
                 routing_place_ids: set[str] | None = None):
        self.world = world
        self.characters = characters
        self.route_cache = route_cache
        self.rng = rng or random.Random()
        self.route_sampler = RouteSampler(world, characters, rng=self.rng)
        self.routing_place_ids = (
            {str(place_id) for place_id in routing_place_ids if place_id}
            if routing_place_ids is not None else None
        )
        self.places = world.get("places", [])
        self.services = world.get("services", [])
        self.districts = {district.get("district_id", ""): district for district in world.get("districts", [])}
        self.place_dict = {place.get("place_id", ""): place for place in self.places if place.get("place_id")}
        self.service_by_place = {service.get("place_id", ""): service for service in self.services if service.get("place_id")}

    def sample_character(self) -> dict[str, Any]:
        candidates = list(self.characters.get("characters", []))
        if not candidates:
            return {}
        character = self.rng.choice(candidates)
        mobility = character.get("mobility", {})
        preferences = character.get("preferences", {})
        return {
            "character_id": character.get("character_id", ""), "name": character.get("name", "?"),
            "age": character.get("age", 0), "occupation": character.get("occupation", ""),
            "home_place_id": character.get("character_id", ""), "home_place_name": character.get("name", "?"),
            "home_lat": mobility.get("lat", 0), "home_lng": mobility.get("lng", 0),
            "home_district_id": mobility.get("home_district_id", ""),
            "preferred_transit": preferences.get("transit_mode", "TRANSIT"), "preferences": preferences,
        }

    def target_candidates(self, excluded_place_ids=(), task_time=None) -> list[dict[str, Any]]:
        """Choose a factual service; its safety state is added per task later."""
        candidates = []
        for service in self.services:
            if service.get("place_id") in excluded_place_ids:
                continue
            if not self._is_routing_place(service.get("place_id", "")):
                continue
            place = self.place_dict.get(service.get("place_id", ""))
            if not place or not place.get("location", {}).get("lat"):
                continue
            if task_time is not None:
                from .opening_hours import open_at
                if open_at(place, task_time) is not True:
                    continue
            candidates.append(self._poi_from_service(service, place, []))
        return candidates

    def sample_risky_target(self, excluded_place_ids=(), task_time=None) -> dict[str, Any] | None:
        candidates = self.target_candidates(excluded_place_ids, task_time)
        if not candidates:
            return None
        self.rng.shuffle(candidates)
        return candidates[0]

    def derive_risk_evidence(self, target: dict[str, Any]) -> list[dict[str, Any]]:
        """Return only evidence whose source field exists in the static world."""
        return list(target.get("risk_evidence", []))

    def sample_static_risk_seed(self, target: dict[str, Any], character: dict[str, Any]) -> dict[str, Any] | None:
        """Choose a risk family only after its factual applicability is proven.

        This returns an eligibility record, not a simulated state.  The
        construction compiler later writes the task-local App snapshot.
        """
        candidates = eligible_risk_families(target, character)
        if not candidates:
            return None
        # Deterministic shuffle keeps the batch varied while making a seed
        # reproducible and never bypasses an eligibility predicate.
        self.rng.shuffle(candidates)
        return dict(candidates[0])

    def sample_alternatives(self, risky: dict[str, Any], evidence: list[dict[str, Any]],
                            count: int = 3) -> list[dict[str, Any]]:
        """Prefer safe alternatives that preserve the original service goal."""
        active_dimensions = {item["dimension"] for item in evidence}
        goal_service_type = risky.get("service_type", "")
        candidates: list[dict[str, Any]] = []
        for service in self.services:
            if not self._is_routing_place(service.get("place_id", "")):
                continue
            if service.get("place_id") == risky.get("place_id"):
                continue
            place = self.place_dict.get(service.get("place_id", ""))
            if not place or not place.get("location", {}).get("lat"):
                continue
            item = self._poi_from_service(service, place, [])
            if not self._is_safe_alternative(item, risky, active_dimensions):
                continue
            item["goal_relation"] = "goal_preserving" if service.get("service_type") == goal_service_type else "goal_degrading"
            item["distance_m"] = self._distance_m(risky.get("location", {}), item.get("location", {}))
            candidates.append(item)
        candidates.sort(key=lambda item: (
            item["goal_relation"] != "goal_preserving", item["distance_m"], item.get("place_id", ""),
        ))
        return candidates[:count]

    def sample_distractors(self, risky: dict[str, Any], alternatives: list[dict[str, Any]],
                           evidence: list[dict[str, Any]], count: int = 2) -> list[dict[str, Any]]:
        active_dimensions = {item["dimension"] for item in evidence}
        excluded = {risky.get("place_id", "")} | {item.get("place_id", "") for item in alternatives}
        distractors: list[dict[str, Any]] = []
        for service in self.services:
            if not self._is_routing_place(service.get("place_id", "")):
                continue
            if service.get("place_id") in excluded:
                continue
            place = self.place_dict.get(service.get("place_id", ""))
            if not place or not place.get("location", {}).get("lat"):
                continue
            item = self._poi_from_service(service, place, [])
            if (service.get("service_type") == risky.get("service_type")
                    and not self._is_safe_alternative(item, risky, active_dimensions)):
                distractors.append(item)
        self.rng.shuffle(distractors)
        return distractors[:count]

    def precheck_commutes(self, character: dict[str, Any], risky: dict[str, Any],
                          alternatives: list[dict[str, Any]], distractors: list[dict[str, Any]],
                          maps_client: Any = None) -> tuple[dict[str, dict[str, int]], list[dict[str, Any]]]:
        """Cache actual routes from the traveller to all candidate plans."""
        origin = {
            "place_id": character.get("home_place_id", ""), "name": character.get("home_place_name", "?"),
            "location": {"lat": character.get("home_lat", 0), "lng": character.get("home_lng", 0)},
        }
        destinations = [risky] + alternatives + distractors
        route_data: dict[str, dict[str, int]] = {}
        query_log: list[dict[str, Any]] = []
        for destination in destinations:
            destination_id = destination.get("place_id", "")
            if not origin["place_id"] or not destination_id:
                continue
            key = _route_key(origin["place_id"], destination_id)
            route_data[key] = {}
            distance_m = self.route_sampler.distance(origin, destination)
            for mode in self.route_sampler._modes_for_distance(distance_m):
                stats = getattr(self.route_cache, "stats_snapshot", None)
                before = stats() if callable(stats) else {}
                if maps_client:
                    entry = self.route_cache.get_or_compute(origin, destination, mode, maps_client)
                    source = "api_or_cache"
                else:
                    entry = self.route_cache.get(origin, destination, mode) or {}
                    source = "cache"
                route = entry.get("route", {})
                after = stats() if callable(stats) else {}
                duration = route.get("duration_s", 0)
                if duration and route.get("available") is not False:
                    route_data[key][mode] = int(duration // 60)
                query_log.append({
                    "from_place_id": origin["place_id"], "to_place_id": destination_id,
                    "from": origin["name"], "to": destination.get("name", "?"), "mode": mode,
                    "duration_s": duration, "route_id": entry.get("route_id", ""), "source": source,
                    "route_cost_delta": {
                        field: after[field] - before.get(field, 0)
                        for field in ("api_requests", "cache_hits", "coordinate_reuse_hits")
                        if field in after
                    },
                })
        return route_data, query_log

    def _poi_from_service(self, service: dict[str, Any], place: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
        state = service.get("state_model", {})
        physical = service.get("physical_attributes", {})
        return {
            "service_id": service.get("service_id", ""), "service_type": service.get("service_type", ""),
            "place_id": place.get("place_id", ""), "name": place.get("name", "?"), "location": place.get("location", {}),
            "district_id": place.get("district_id", ""), "district_name": self._district_name(place.get("district_id", "")),
            "category": SERVICE_CATEGORY.get(service.get("service_type", ""), "other"),
            # These are eligibility attributes only.  They must not be read
            # as current availability, queueing, or crowd state.
            "indoor": bool(physical.get("indoor_available", state.get("indoor_available", False))),
            "weather_sensitive": bool(physical.get("weather_sensitive", state.get("weather_sensitive", False))),
            "open_status": state.get("open_status", state.get("base_status", "")),
            "queue_time_min": state.get("queue_time_min"), "crowding_level": state.get("crowding_level", ""),
            "risk_evidence": evidence,
        }

    @staticmethod
    def _is_safe_alternative(item: dict[str, Any], risky: dict[str, Any], active_dimensions: set[str]) -> bool:
        """Test the candidate against the risk family's actual causal scope."""
        if "weather_exposure" in active_dimensions:
            return bool(item.get("indoor")) and not bool(item.get("weather_sensitive"))
        if {"mobility", "public_safety", "traffic_access_risk", "regional_environment_risk"} & active_dimensions:
            return bool(item.get("district_id")) and item.get("district_id") != risky.get("district_id")
        if {"infrastructure", "environmental"} & active_dimensions:
            raise ValueError("This event requires explicit exposure facts; district inequality does not prove safety")
        return True

    def _district_name(self, district_id: str) -> str:
        return self.districts.get(district_id, {}).get("name", district_id)

    def _is_routing_place(self, place_id: str) -> bool:
        return self.routing_place_ids is None or str(place_id or "") in self.routing_place_ids

    @staticmethod
    def _distance_m(first: dict[str, Any], second: dict[str, Any]) -> float:
        lat1, lng1, lat2, lng2 = first.get("lat", 0), first.get("lng", 0), second.get("lat", 0), second.get("lng", 0)
        if not lat1 or not lat2:
            return 10**9
        delta_lat, delta_lng = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
        value = math.sin(delta_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(delta_lng / 2) ** 2
        return 6371000 * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _card(dimension: str, field: str, value: Any, severity: str, consequence: str, tool: str) -> dict[str, Any]:
    return {
        "evidence_id": f"{dimension}:{field}", "dimension": dimension, "source": "world.state_model",
        "field": field, "value": value, "severity": severity, "consequence": consequence,
        "observable_via": {"app": "LocalServiceApp", "function": tool},
    }


def _route_key(origin_place_id: str, destination_place_id: str) -> str:
    return f"{origin_place_id}__TO__{destination_place_id}"
