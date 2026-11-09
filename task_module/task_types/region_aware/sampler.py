"""Region-aware planning sampler — selects city, constraints from KB via keywords,
character, suitable POIs, and prechecks commutes.

Constraints are selected via two-level random sampling on keyword dimensions.
No scene classification — keywords provide combinatorial diversity naturally.
"""

from __future__ import annotations
from shared.person_route_identity import person_route_ids, route_endpoint_matches

import random
from typing import Any

from task_module.task_types.region_aware.kb_manager import CulturalKB, list_available_cities
from task_module.task_types.route_planning.sampler import RouteSampler


class RegionSampler:

    def __init__(self, world: dict[str, Any], characters: dict[str, Any],
                 route_cache: Any, rng: random.Random | None = None,
                 target_city: str = "", admission_mode: str = "prototype_validated",
                 kb: CulturalKB | None = None):
        self.world = world
        self.characters = characters
        self.route_cache = route_cache
        self.rng = rng or random.Random()
        self.target_city = target_city
        self.admission_mode = admission_mode
        self.kb = kb
        self.places = world.get("places", [])
        self.services = world.get("services", [])
        self.districts = world.get("districts", [])
        self.place_dict: dict[str, dict] = {p["place_id"]: p for p in self.places if p.get("place_id")}

    def sample_city(self) -> tuple[str, CulturalKB]:
        if self.target_city:
            return self.target_city, self.kb or CulturalKB(self.target_city)
        cities = list_available_cities()
        city = self.rng.choice(cities)
        return city, CulturalKB(city)

    def sample_constraints(self, kb: CulturalKB, n_keywords: int = 2,
                           n_per_keyword: int = 2) -> tuple[list[dict], list[str], bool]:
        """Two-level random sampling on keyword dimensions.

        Step 1: randomly pick n_keywords from all approved constraint keywords
        Step 2: per keyword, randomly pick n_per_keyword constraints
        Guard: if cultural keyword pool is non-empty and all picked are informational
               (from Understand/Get around/Sleep), replace one with a cultural keyword.

        Returns: (constraints, chosen_keywords, pool_is_small)
        """
        approved = [
            c for c in kb.get_task_eligible(self.admission_mode)
            if self._constraint_supported_by_world(c) and _is_formal_planning_constraint(c)
        ]
        if not approved:
            return [], [], True

        # One plan must have one coherent public activity.  Sampling unrelated
        # dining and worship rules together produced prompts that merely asked
        # the Agent to recite both rules instead of making one integrated plan.
        activity_groups: dict[str, list[dict]] = {}
        for constraint in approved:
            activity_groups.setdefault(activity_for_constraint(constraint), []).append(constraint)
        chosen_activity = self.rng.choice(sorted(activity_groups))
        approved = activity_groups[chosen_activity]

        INFO_SECTIONS = {"Understand", "Get around", "Sleep"}

        keyword_pool: dict[str, list[dict]] = {}
        # Track whether each keyword has ANY constraint from a cultural section
        kw_is_cultural: dict[str, bool] = {}

        for c in approved:
            is_info = c.get("section", "") in INFO_SECTIONS
            for kw in c.get("keywords", []):
                keyword_pool.setdefault(kw, []).append(c)
                if kw not in kw_is_cultural:
                    kw_is_cultural[kw] = not is_info
                elif not is_info:
                    kw_is_cultural[kw] = True

        if not keyword_pool:
            return approved, [], False

        cultural_kw = [kw for kw in keyword_pool if kw_is_cultural.get(kw, False)]
        available_kw = list(keyword_pool.keys())
        n_kw = min(n_keywords, len(available_kw))
        chosen_keywords = self.rng.sample(available_kw, n_kw)

        # Guard: don't let ALL chosen keywords be informational-only
        if cultural_kw and all(kw not in cultural_kw for kw in chosen_keywords):
            swap_idx = self.rng.randrange(len(chosen_keywords))
            chosen_keywords[swap_idx] = self.rng.choice(cultural_kw)

        # Step 2: per keyword, pick constraints
        result = []
        seen_ids = set()
        for kw in chosen_keywords:
            pool = keyword_pool[kw]
            n = min(n_per_keyword, len(pool))
            picked = self.rng.sample(pool, n)
            for c in picked:
                if c["id"] not in seen_ids:
                    result.append(c)
                    seen_ids.add(c["id"])

        pool_is_small = len(approved) < 3
        return result, chosen_keywords, pool_is_small

    def sample_character(self) -> dict:
        chars = list(self.characters.get("characters", []))
        if not chars:
            return {}
        c = self.rng.choice(chars)
        mob = c.get("mobility", {})
        prefs = c.get("preferences", {})
        return {
            "character_id": c.get("character_id", ""),
            "name": c.get("name", "?"),
            "age": c.get("age", 0),
            "occupation": c.get("occupation", ""),
            "home_place_id": c.get("character_id", ""),
            "home_place_name": c.get("name", "?"),
            "home_lat": mob.get("lat", 0),
            "home_lng": mob.get("lng", 0),
            "home_district_id": mob.get("home_district_id", ""),
            "preferred_transit": prefs.get("transit_mode", self.rng.choice(["TRANSIT", "DRIVE", "TRANSIT"])),
            "preferences": prefs,
        }

    def build_planning_profile(self, constraints: list[dict]) -> dict[str, Any] | None:
        """Choose the public destination category shared by prompt and solver.

        This carries only a public task condition, such as ``印度教寺庙``.
        It never contains the underlying cultural rule or source evidence.
        A multi-rule task is valid only where all selected rules apply to one
        common city-backed primary place type.
        """
        activity = activity_for_constraints(constraints)
        type_sets = [
            {str(value) for value in ((constraint.get("hidden_rule", {}) or {}).get("required_place_types", []) or [])}
            for constraint in constraints
            if (constraint.get("hidden_rule", {}) or {}).get("required_place_types")
        ]
        available_types = {
            str(place.get("primary_type") or "")
            for service in self.services
            for place in [self.place_dict.get(service.get("place_id", ""), {})]
            if place.get("primary_type")
        }
        if type_sets:
            compatible = set.intersection(*type_sets) & available_types
            if not compatible:
                return None
            target_type = self.rng.choice(sorted(compatible))
            return {
                "activity": activity,
                "target_primary_type": target_type,
                "allowed_primary_types": [target_type],
                "destination_label": _public_place_label(target_type),
            }
        return {
            "activity": activity,
            "target_primary_type": "",
            "allowed_primary_types": [],
            "destination_label": "",
        }

    def sample_pois_for_constraints(self, constraints: list[dict], count: int = 5,
                                    planning_profile: dict[str, Any] | None = None) -> list[dict]:
        """Select world-backed POIs for one public activity.

        ``required_place_types`` describes the activity in which a rule can
        apply; it does not itself disclose the private cultural rule.  The
        solver separately records the action/time/venue effect of the rule.
        """
        required_types = {
            str(item) for constraint in constraints
            for item in (constraint.get("hidden_rule", {}).get("required_place_types", []) or [])
        }
        profile_types = set((planning_profile or {}).get("allowed_primary_types", []) or [])
        if profile_types:
            required_types = profile_types
        activity = str((planning_profile or {}).get("activity") or activity_for_constraints(constraints))
        candidates = []
        for svc in self.services:
            stype = svc.get("service_type", "")
            pid = svc.get("place_id", "")
            place = self.place_dict.get(pid)
            if not place:
                continue
            primary_type = str(place.get("primary_type", ""))
            if stype not in _REGION_CANDIDATE_TYPES and primary_type not in required_types:
                continue
            if required_types and primary_type not in required_types:
                continue
            if activity == "shared_meal" and stype not in {"restaurant", "cafe", "restaurant_reservation"}:
                continue
            loc = place.get("location", {})
            if not loc.get("lat"):
                continue
            candidates.append({
                "service_id": svc.get("service_id", ""),
                "place_id": pid,
                "name": place.get("name", "?"),
                "location": loc,
                "district_id": place.get("district_id", ""),
                "district_name": self._district_name(place.get("district_id", "")),
                "category": _svc_category(stype),
                "primary_type": place.get("primary_type", ""),
                "rating": round(place.get("map_projection", {}).get("rating", 0) or 0, 1),
                "price_level": _price(svc),
                "indoor": svc.get("state_model", {}).get("indoor_available", True),
                "cultural_tags": self._place_tags(place, svc),
            })
        avoid_tags = self._avoid_tags(constraints)
        safe = [poi for poi in candidates if not (set(poi["cultural_tags"]) & avoid_tags)]
        unsafe = [poi for poi in candidates if set(poi["cultural_tags"]) & avoid_tags]
        self.rng.shuffle(safe)
        self.rng.shuffle(unsafe)
        # A venue constraint is meaningful only when the candidate set contains
        # both a compliant alternative and a plausible violating distractor.
        return (safe + unsafe)[:count]

    def sample_distractors(self, main_pois: list[dict], constraints: list[dict], count: int = 2) -> list[dict]:
        """Find POIs that might violate cultural constraints (wrong choices)."""
        main_ids = {p["place_id"] for p in main_pois}
        candidates = []
        for svc in self.services:
            stype = svc.get("service_type", "")
            if stype not in _REGION_DISTRACTOR_TYPES:
                continue
            pid = svc.get("place_id", "")
            if pid in main_ids:
                continue
            place = self.place_dict.get(pid)
            if not place:
                continue
            loc = place.get("location", {})
            if not loc.get("lat"):
                continue
            candidates.append({
                "service_id": svc.get("service_id", ""),
                "place_id": pid,
                "name": place.get("name", "?"),
                "location": loc,
                "district_id": place.get("district_id", ""),
                "category": _svc_category(stype),
                "primary_type": place.get("primary_type", ""),
                "cultural_tags": self._place_tags(place, svc),
            })
        avoid_tags = self._avoid_tags(constraints)
        violating = [poi for poi in candidates if set(poi["cultural_tags"]) & avoid_tags]
        neutral = [poi for poi in candidates if poi not in violating]
        self.rng.shuffle(violating)
        self.rng.shuffle(neutral)
        return (violating + neutral)[:count]

    def precheck_commutes(self, main_pois: list[dict], distractors: list[dict],
                            character: dict, maps_client: Any = None) -> tuple[dict, list]:
        """Load only the forward actor-to-candidate routes required by the oracle.

        Region feasibility evaluates whether the selected traveler can reach a
        candidate place. It does not use candidate-to-candidate or reverse
        routes, so constructing a complete graph wastes API quota and turns an
        unrelated cache miss into a task-generation failure. Each forward edge
        uses the shared distance-based mode rule.
        """
        actor = {
            "place_id": f"character:{character.get('character_id', '')}",
            "name": character.get("name", "traveler"),
            "location": {"lat": character.get("home_lat", 0), "lng": character.get("home_lng", 0)},
        }
        all_pois = main_pois + distractors
        route_data: dict[str, dict] = {}
        query_log: list[dict] = []
        oid = actor.get("place_id", "")
        oname = actor.get("name", "?")[:15]
        for dest in all_pois:
            did = dest.get("place_id", "")
            if not oid or not did:
                continue
            dname = dest.get("name", "?")[:15]
            key = f"{oid}->{did}"
            route_data[key] = {}
            op = {"place_id": oid, "name": oname, "location": actor.get("location", {})}
            dp = {"place_id": did, "name": dname, "location": dest.get("location", {})}
            distance_m = RouteSampler.distance(op, dp)
            for mode in RouteSampler._modes_for_distance(distance_m):
                if maps_client:
                    entry = self.route_cache.get_or_compute(op, dp, mode, maps_client)
                else:
                    ck = self.route_cache._make_key(op, dp, mode)
                    entry = self.route_cache._cache.get(ck, {})
                    if not entry:
                        bare, _ = person_route_ids(oid)
                        alternate = self.route_cache._cache.get(
                            self.route_cache._make_key({**op, "place_id": bare}, dp, mode), {})
                        if alternate and route_endpoint_matches(
                                alternate, "origin", actor["location"], actor["name"]):
                            entry = self.route_cache._resolved_entry(alternate)
                rt = entry.get("route", {})
                dist = rt.get("distance_m", 0)
                dur = rt.get("duration_s", 0)
                if dist > 0 and rt.get("available") is not False:
                    route_data[key][mode] = {
                        "duration_min": max(1, dur // 60),
                        "distance_m": dist,
                        "route_id": entry.get("route_id", ""),
                    }
                    query_log.append({"from": oname, "to": dname, "mode": mode,
                                      "distance_m": dist, "duration_s": dur,
                                      "source": "cache" if not maps_client else "api_or_cache",
                                      "route_id": entry.get("route_id", "")})
                elif rt.get("api_error"):
                    query_log.append({"from": oname, "to": dname, "mode": mode, "source": "api_error"})
        return route_data, query_log

    def build_dynamic_source_graph(self, constraints: list[dict], character: dict,
                                   baseline_pois: list[dict], limit: int = 32) -> tuple[list[dict], dict[str, dict]]:
        """Build a private, read-only successor graph from the frozen city asset.

        The graph is deliberately wider than one static task's authored POI
        sample.  It reads only the selected world version and its route cache;
        it never performs an API backfill and is never projected to the tested
        Agent.  Baseline POIs stay first so P0 remains the static control.
        """
        required_types = {
            str(item) for constraint in constraints
            for item in ((constraint.get("hidden_rule", {}) or {}).get("required_place_types", []) or [])
        }
        activity = activity_for_constraints(constraints)
        seen = {str(poi.get("place_id") or "") for poi in baseline_pois}
        graph = [dict(poi) for poi in baseline_pois if poi.get("place_id")]
        extras: list[dict] = []
        for service in self.services:
            place_id = str(service.get("place_id") or "")
            if not place_id or place_id in seen:
                continue
            place = self.place_dict.get(place_id)
            if not place:
                continue
            service_type = str(service.get("service_type") or "")
            primary_type = str(place.get("primary_type") or "")
            if required_types and primary_type not in required_types:
                continue
            if activity == "shared_meal" and service_type not in {"restaurant", "cafe", "restaurant_reservation"}:
                continue
            if not required_types and activity != "shared_meal" and service_type not in _REGION_CANDIDATE_TYPES:
                continue
            location = dict(place.get("location") or {})
            if location.get("lat") is None or location.get("lng") is None:
                continue
            extras.append({
                "service_id": service.get("service_id", ""), "place_id": place_id,
                "name": place.get("name", "?"), "location": location,
                "district_id": place.get("district_id", ""),
                "district_name": self._district_name(place.get("district_id", "")),
                "category": _svc_category(service_type), "primary_type": primary_type,
                "rating": round(place.get("map_projection", {}).get("rating", 0) or 0, 1),
                "price_level": _price(service),
                "indoor": service.get("state_model", {}).get("indoor_available", True),
                "cultural_tags": self._place_tags(place, service),
            })
        extras.sort(key=lambda item: (str(item.get("name") or ""), str(item.get("place_id") or "")))
        graph.extend(extras[:max(0, limit - len(graph))])
        routes, _queries = self.precheck_commutes(graph, [], character, maps_client=None)
        return graph, routes

    def _district_name(self, did: str) -> str:
        for d in self.districts:
            if d.get("district_id") == did:
                return d.get("name", did)
        return did

    def _constraint_supported_by_world(self, constraint: dict[str, Any]) -> bool:
        """Do not create a POI-verification task without matching world fields."""
        rule = constraint.get("hidden_rule", {})
        if rule.get("forbidden_actions") or rule.get("forbidden_time_ranges"):
            return True
        required_tags = set(rule.get("avoid_place_tags", []))
        nested = rule.get("formal_business_venue_filter", {})
        required_tags.update(nested.get("exclude_venue_type", []))
        required_types = set(rule.get("required_place_types", []))
        if not required_tags and not required_types:
            return False
        for service in self.services:
            place = self.place_dict.get(service.get("place_id", ""), {})
            tags = set(self._place_tags(place, service))
            if required_tags & tags or required_types & tags:
                return True
        return False

    @staticmethod
    def _avoid_tags(constraints: list[dict]) -> set[str]:
        tags: set[str] = set()
        for constraint in constraints:
            rule = constraint.get("hidden_rule", {})
            tags.update(rule.get("avoid_place_tags", []))
            tags.update(rule.get("formal_business_venue_filter", {}).get("exclude_venue_type", []))
        return tags

    @staticmethod
    def _place_tags(place: dict[str, Any], service: dict[str, Any]) -> list[str]:
        tags = set(place.get("control_tags", {}).get("semantic_tags", []))
        tags.update(place.get("director_notes", {}).get("llm_semantic", {}).get("semantic_tags", []))
        tags.add(place.get("primary_type", ""))
        tags.add(service.get("service_type", ""))
        return sorted(tag for tag in tags if tag)


def activity_for_constraint(constraint: dict[str, Any]) -> str:
    """Return the public planning activity without exposing a rule text."""
    hidden = constraint.get("hidden_rule", {}) or {}
    effect = hidden.get("decision_effect", {}) or {}
    activity = str(effect.get("activity") or "")
    if activity:
        return activity
    if hidden.get("required_place_types"):
        return "worship_visit"
    words = " ".join(str(item).lower() for item in constraint.get("keywords", []) or [])
    actions = " ".join(str(item).lower() for item in hidden.get("forbidden_actions", []) or [])
    if "dining" in words or "chopsticks" in words or "eat_" in actions:
        return "shared_meal"
    return "public_outing"


def _is_formal_planning_constraint(constraint: dict[str, Any]) -> bool:
    """Exclude etiquette-only rules from formal Region planning construction.

    A source may remain reviewable cultural evidence while lacking the frozen
    facts needed to change a location, schedule, or itinerary decision.
    """
    effect = (constraint.get("hidden_rule", {}) or {}).get("decision_effect", {}) or {}
    dimension = str(effect.get("dimension") or "")
    if dimension == "schedule":
        # A private forbidden-time range (for example a hard-coded "after
        # sunset" minute) is not a Region ground truth.  It is merely a
        # construction-side cultural inference unless a future temporal
        # decision kernel makes its public facts and accepted decision class
        # explicit.  That kernel does not exist yet, so schedule-only rules
        # cannot enter a formal batch.
        return False
    return dimension in {
        "selected_places", "schedule", "itinerary_structure", "route", "travel_mode",
        "duration", "participants", "service_assignment",
    }


def activity_for_constraints(constraints: list[dict[str, Any]]) -> str:
    activities = {activity_for_constraint(constraint) for constraint in constraints}
    return next(iter(activities)) if len(activities) == 1 else "public_outing"


def _public_place_label(primary_type: str) -> str:
    labels = {
        "hindu_temple": "印度教寺庙",
        "buddhist_temple": "佛寺",
        "temple": "寺庙",
        "mosque": "清真寺",
        "church": "教堂",
    }
    return labels.get(primary_type, primary_type.replace("_", " "))


def _svc_category(stype: str) -> str:
    m = {
        "restaurant": "restaurant", "cafe": "cafe", "bar": "bar", "shopping": "shopping",
        "lodging": "lodging", "tourist_attraction": "tourist_attraction", "museum": "museum",
        "movie_theater": "movie_theater", "park": "park", "transit_station": "transit_station",
        "hospital": "hospital", "medical_care": "medical_care", "pharmacy": "pharmacy",
        "university": "university",
        # Historical broad IDs remain readable by frozen task versions.
        "attraction": "attraction", "retail": "retail", "hotel": "hotel", "healthcare": "healthcare",
        "restaurant_reservation": "restaurant", "attraction_visit": "attraction",
        "local_visit": "cafe", "retail_visit": "retail", "outdoor_visit": "park",
        "hotel_booking": "lodging",
    }
    return m.get(stype, "other")


_REGION_CANDIDATE_TYPES = {
    "restaurant", "cafe", "bar", "shopping", "lodging", "tourist_attraction", "museum",
    "movie_theater", "park", "hospital", "medical_care", "pharmacy", "university",
    "attraction", "retail", "hotel", "healthcare", "restaurant_reservation", "attraction_visit",
    "local_visit", "retail_visit", "outdoor_visit", "hotel_booking",
}
_REGION_DISTRACTOR_TYPES = {
    "restaurant", "cafe", "bar", "tourist_attraction", "museum", "movie_theater", "park",
    "attraction", "restaurant_reservation", "attraction_visit", "local_visit", "outdoor_visit",
}


def _price(svc: dict) -> str:
    band = svc.get("state_model", {}).get("price_band", "")
    mapping = {"low": "INEXPENSIVE", "medium": "MODERATE", "medium_high": "EXPENSIVE", "high": "VERY_EXPENSIVE"}
    return mapping.get(band, "MODERATE")
