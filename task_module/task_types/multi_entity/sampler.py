"""Multi-entity participant, POI, and character-route sampling."""

from __future__ import annotations

import math
import random
from typing import Any

from shared.place_type_catalog import category_for_google_types
from task_module.task_types.route_planning.sampler import RouteSampler
from .short_walk import short_walk_estimate


_ACTIVITY_BY_CATEGORY = {
    "restaurant": "shared_meal",
    "cafe": "shared_meal",
    "park": "outdoor_visit",
    "museum": "museum_visit",
    "tourist_attraction": "attraction_visit",
    "shopping": "shopping_visit",
    "movie_theater": "movie_visit",
}


class MultiEntitySampler:
    """Samples participants, restaurants, and commutes for multi-entity tasks."""

    def __init__(self, world: dict[str, Any], characters: dict[str, Any],
                 route_cache: Any, rng: random.Random | None = None):
        self.world = world
        self.characters = characters
        self.route_cache = route_cache
        self.rng = rng or random.Random()

        self.places = world.get("places", [])
        self.services = world.get("services", [])
        self.districts = world.get("districts", [])

        # Place → district mapping
        self.place_district: dict[str, str] = {}
        self.district_places: dict[str, list[dict]] = {}
        for p in self.places:
            did = p.get("district_id", "")
            pid = p.get("place_id", "")
            if did and pid:
                self.place_district[pid] = did
                self.district_places.setdefault(did, []).append(p)

        self.route_sampler = RouteSampler(world, characters, self.rng)

    def sample_participants(self, count: int = 3) -> list[dict]:
        """Select 2-4 characters, preferring relationship-neighbors of the seed."""
        chars = list(self.characters.get("characters", []))
        rels = self.characters.get("relationships", [])

        # Build relationship adjacency
        rel_graph: dict[str, set[str]] = {}
        for r in rels:
            src = r.get("character_id", "")
            tgt = r.get("related_id", "")
            if src and tgt:
                rel_graph.setdefault(src, set()).add(tgt)
                rel_graph.setdefault(tgt, set()).add(src)

        # Build relationship adjacency
        ...
        if not chars:
            return []

        selected: list[dict] = []
        seen_ids: set[str] = set()

        # Pick seed character randomly from all characters
        seed = self.rng.choice(chars)
        selected.append(seed)
        seen_ids.add(seed.get("character_id", ""))

        # Expand via relationships
        connected = sorted(rel_graph.get(seed.get("character_id", ""), set()))
        self.rng.shuffle(connected)
        for nid in connected:
            if len(selected) >= count:
                break
            c = next((c for c in chars if c.get("character_id") == nid), None)
            if c and nid not in seen_ids:
                selected.append(c)
                seen_ids.add(nid)

        # Fill remaining with random
        remaining = [c for c in chars if c.get("character_id") not in seen_ids]
        self.rng.shuffle(remaining)
        for c in remaining:
            if len(selected) >= count:
                break
            selected.append(c)

        # Build participant records
        participants = []
        for c in selected:
            mob = c.get("mobility", {})
            prefs = c.get("preferences", {})
            consts = [ct for ct in c.get("constraints", [])]

            home_place_id = c.get("character_id", "")
            participants.append({
                "character_id": home_place_id,
                "name": c.get("name", "?"),
                "age": c.get("age", 0),
                "occupation": c.get("occupation", ""),
                "home_place_id": home_place_id,
                "home_place_name": c.get("name", "?"),
                "home_lat": mob.get("lat", 0),
                "home_lng": mob.get("lng", 0),
                "home_district_id": mob.get("home_district_id", ""),
                "available_from": self.rng.choice(["17:30", "18:00", "18:30", "19:00"]),
                "must_leave_by": self.rng.choice(["20:30", "21:00", "21:30", "22:00"]),
                "has_car": self.rng.random() < 0.4,
                "dietary": prefs.get("dietary", "") or self._random_dietary(),
                "budget": prefs.get("budget", "") or self.rng.choice(["low", "medium", "high"]),
                "personality": self.rng.choice(["守时", "随和", "挑剔", "健谈"]),
                "can_drive_others": self.rng.random() < 0.3,
                "preferences": prefs,
                "constraints": [{"content": ct.get("content", "")} for ct in consts[:2]],
            })
        return participants

    def sample_activity_places(
        self, routing_places: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Choose one GT and its nearby private coverage POIs."""
        if not routing_places:
            return None
        branch = self.rng.choices(["reuse", "diffusion"], weights=[90, 10], k=1)[0]
        pool_ids = {str(place.get("place_id") or "") for place in routing_places}

        if branch == "reuse":
            eligible = [place for place in routing_places if self._activity_category(place)]
            if not eligible:
                return None
            target = self.rng.choice(eligible)
            neighbors = self._nearby_places(target, routing_places, exclude={target.get("place_id")}, count=4)
            if len(neighbors) < 3:
                return None
            return {
                "branch": branch, "anchor": None, "target": self._venue(target),
                "neighbors": [self._venue(place) for place in neighbors],
            }

        anchors = list(routing_places)
        self.rng.shuffle(anchors)
        all_unused = [place for place in self.places if str(place.get("place_id") or "") not in pool_ids]
        for radius in (3000, 5000):
            for anchor in anchors:
                targets = [
                    place for place in all_unused
                    if self._activity_category(place)
                    and 1000 <= self.route_sampler.distance(anchor, place) <= radius
                    and not self._same_address(anchor, place)
                ]
                if not targets:
                    continue
                target = self.rng.choice(targets)
                neighbors = self._nearby_places(
                    target, all_unused, exclude={target.get("place_id")}, count=4,
                )
                return {
                    "branch": branch, "anchor": self._venue(anchor), "target": self._venue(target),
                    "neighbors": [self._venue(place) for place in neighbors[:4]],
                }
        return None

    def materialize_feasible_activity(
        self, participants: list[dict], target: dict[str, Any],
        route_data: dict[str, dict[str, int]], *, earliest_departure: int = 16 * 60,
    ) -> dict[str, Any] | None:
        """Reverse-construct one factual time window around a route-complete GT."""
        commutes = [route_data.get(f"{p['home_place_name']}→{target['name']}", {}) for p in participants]
        if not all(commutes):
            return None
        meeting_duration = 60
        meeting_start = 19 * 60
        if 'activity_opening_intervals' in target:
            earliest = math.ceil(earliest_departure + max(min(modes.values()) for modes in commutes))
            starts = [
                min(max(meeting_start, max(opened, earliest)), min(closed - meeting_duration, 19 * 60 + 30))
                for opened, closed in target['activity_opening_intervals'] or []
                if max(opened, earliest) <= min(closed - meeting_duration, 19 * 60 + 30)
            ]
            if not starts:
                return None
            meeting_start = min(starts, key=lambda start: (abs(start - 19 * 60), start))
        for person, modes in zip(participants, commutes):
            person["available_from"] = self._format_clock(max(earliest_departure, math.floor(meeting_start - min(modes.values()))))
            person["must_leave_by"] = "20:30"
            person.pop("dietary", None)
            person.pop("budget", None)
        return {
            "family": "independent_meeting",
            "activity_type": target["activity_type"],
            "required_primary_type": target["primary_type"],
            "meeting_duration_min": meeting_duration,
            "allowed_travel_modes": ["WALK", "TRANSIT", "DRIVE"],
            "relations": ["joint_activity", "individual_time_windows"],
            "resources": [],
        }

    def precheck_character_routes(
        self, participants: list[dict], venues: list[dict], maps_client: Any = None,
    ) -> tuple[dict, list]:
        """Materialize only selected-character to selected-POI route facts."""
        route_data: dict[str, dict] = {}
        query_log: list[dict] = []
        for person in participants:
            origin = {
                "place_id": person.get("home_place_id", ""),
                "name": person.get("home_place_name") or person.get("name", ""),
                "location": {"lat": person.get("home_lat", 0), "lng": person.get("home_lng", 0)},
            }
            for venue in venues:
                destination = {
                    "place_id": venue.get("place_id", ""), "name": venue.get("name", ""),
                    "location": venue.get("location", {}),
                }
                key = f"{origin['name']}→{destination['name']}"
                route_data[key] = {}
                distance_m = self.route_sampler.distance(origin, destination)
                if distance_m < 100:
                    estimate = short_walk_estimate(person, venue)
                    if estimate is not None:
                        route_data[key]["WALK"] = estimate["duration_min"]
                        query_log.append(estimate)
                    continue
                for mode in self.route_sampler._modes_for_distance(distance_m):
                    entry = (
                        self.route_cache.get_or_compute(origin, destination, mode, maps_client)
                        if maps_client else self.route_cache.get(origin, destination, mode) or {}
                    )
                    route = entry.get("route", {})
                    if route.get("available") is not False and route.get("duration_s", 0) > 0:
                        route_data[key][mode] = route["duration_s"] / 60
                        query_log.append({
                            "from": origin["name"], "to": destination["name"], "mode": mode,
                            "distance_m": route.get("distance_m", 0), "duration_s": route.get("duration_s", 0),
                            "route_id": entry.get("route_id", ""),
                            "source": "api_or_cache" if maps_client else "cache",
                        })
        return route_data, query_log

    def _nearby_places(
        self, center: dict, candidates: list[dict], *, exclude: set[Any], count: int,
    ) -> list[dict]:
        valid = [
            place for place in candidates
            if place.get("place_id") not in exclude
            and 100 <= self.route_sampler.distance(center, place) <= 5000
            and not self._same_address(center, place)
        ]
        preferred = [place for place in valid if self.route_sampler.distance(center, place) <= 2000]
        fallback = [place for place in valid if place not in preferred]
        self.rng.shuffle(preferred)
        self.rng.shuffle(fallback)
        return (preferred + fallback)[:count]

    @staticmethod
    def _same_address(left: dict, right: dict) -> bool:
        a = str(left.get("address") or "").strip().casefold()
        b = str(right.get("address") or "").strip().casefold()
        return bool(a and b and a == b)

    @staticmethod
    def _activity_category(place: dict) -> str:
        types = [place.get("primary_type"), *(place.get("types") or [])]
        category = category_for_google_types(value for value in types if value)
        return category if category in _ACTIVITY_BY_CATEGORY else ""

    def _venue(self, place: dict) -> dict[str, Any]:
        category = self._activity_category(place)
        return {
            "place_id": str(place.get("place_id") or ""),
            "name": str(place.get("name") or ""),
            "place_name": str(place.get("name") or ""),
            "location": place.get("location") or {},
            "address": str(place.get("address") or ""),
            "district": str(place.get("district_id") or ""),
            "primary_type": str(place.get("primary_type") or category),
            "types": list((place.get("map_projection") or {}).get("types") or place.get("types") or []),
            "activity_type": _ACTIVITY_BY_CATEGORY.get(category, "shared_activity"),
        }

    def sample_restaurants(self, participants: list[dict], count: int = 5) -> list[dict]:
        """Select candidate restaurants across different districts."""
        home_districts = {p.get("home_district_id", "") for p in participants}

        candidates = []
        for svc in self.services:
            stype = svc.get("service_type", "")
            if not category_matches("restaurant", stype):
                continue
            pid = svc.get("place_id", "")
            place = next((p for p in self.places if p.get("place_id") == pid), None)
            if not place:
                continue
            did = place.get("district_id", "")
            # Prefer districts near participants
            is_near = did in home_districts
            candidates.append({
                "service_id": svc.get("service_id", ""),
                "place_id": pid,
                "name": place.get("name", "?"),
                "place_name": place.get("name", ""),
                "location": place.get("location", {}),
                "cuisine": self._guess_cuisine(place),
                "price_pp": self.rng.choice([50, 80, 100, 120, 150, 200]),
                "rating": round(self.rng.uniform(3.5, 4.8), 1),
                "district": did,
                "dietary_options": self._dietary_options(place),
                "open_until": "22:00" if self.rng.random() < 0.7 else "21:00",
                "max_party": self.rng.choice([6, 8, 10, 12]),
                "is_near": is_near,
            })

        # Sort: near districts first, then random
        candidates.sort(key=lambda x: (not x["is_near"], self.rng.random()))
        selected = candidates[:count]

        # Ensure at least 2 different districts
        dists = {r.get("district") for r in selected}
        if len(dists) < 2 and len(candidates) > count:
            for c in candidates[count:]:
                if c.get("district") not in dists:
                    selected[-1] = c
                    dists.add(c.get("district"))
                    if len(dists) >= 2:
                        break

        return selected

    def discover_scenario_families(self, participants: list[dict], restaurants: list[dict]) -> list[str]:
        """Return only coordination families supported by this city/world slice."""
        families = []
        if restaurants:
            families.append("independent_meeting")
        drivers = [p for p in participants if p.get("has_car") and p.get("can_drive_others")]
        if restaurants and drivers:
            families.extend(["shared_vehicle_meeting", "pickup_sequence_meeting"])
        return families

    def sample_scenario(self, participants: list[dict], restaurants: list[dict]) -> dict[str, Any] | None:
        """Build a factual relation/resource specification before prompt writing."""
        families = self.discover_scenario_families(participants, restaurants)
        if not families:
            return None
        family = self.rng.choice(families)
        drivers = [p for p in participants if p.get("has_car") and p.get("can_drive_others")]
        scenario = {
            "family": family,
            "relations": ["joint_meeting", "individual_time_windows"],
            "resources": [],
        }
        if family in {"shared_vehicle_meeting", "pickup_sequence_meeting"}:
            driver = self.rng.choice(drivers)
            scenario["relations"].append("shared_vehicle")
            if family == "pickup_sequence_meeting":
                scenario["relations"].append("ordered_pickup")
            scenario["resources"].append({
                "resource_type": "private_vehicle",
                "owner_character_id": driver.get("character_id", ""),
                "capacity": min(4, len(participants)),
            })
        return scenario

    def select_route_complete_target(
        self,
        participants: list[dict],
        restaurants: list[dict],
        route_data: dict[str, dict[str, int]],
    ) -> dict[str, Any] | None:
        """Choose an answer venue only when every participant can reach it.

        This is deliberately before scenario constraints are materialized.  A
        feasible-only coordination task is authored around a real POI and its
        verified routes, not by hoping a random set of requirements happens to
        match a random restaurant list.
        """
        eligible = [
            restaurant for restaurant in restaurants
            if all(self._route_minutes(person, restaurant, route_data) is not None for person in participants)
        ]
        return self.rng.choice(eligible) if eligible else None

    def materialize_feasible_independent_scenario(
        self,
        participants: list[dict],
        restaurants: list[dict],
        target: dict[str, Any],
        route_data: dict[str, dict[str, int]],
    ) -> dict[str, Any] | None:
        """Create a factual, feasible static coordination scenario around ``target``.

        Character identity/home locations and restaurant POIs remain frozen
        world facts.  The task owns the meeting's static service profile and
        participant availability/preferences.  Those profiles are made
        deliberately consistent with the selected answer; distractors each
        fail a visible constraint.  They are not inferred from Google POI
        types and therefore carry an explicit task-static provenance label.
        """
        if not participants or not target:
            return None
        commute_minutes = [self._route_minutes(person, target, route_data) for person in participants]
        if any(minutes is None for minutes in commute_minutes):
            return None

        party_size = len(participants)
        meeting_start = 19 * 60
        meeting_duration = 60
        meeting_end = meeting_start + meeting_duration
        target_price = int(target.get("price_pp", 0) or 0)
        if target_price <= 0:
            return None

        # One canonical dietary requirement is enough to make the answer
        # discriminative without promoting arbitrary free-text preferences to
        # hard constraints.  The selected venue is the factual target for this
        # task-static service profile; every other venue becomes a documented
        # distractor through one visible mismatch.
        target["dietary_options"] = ["regular", "vegetarian"]
        target["max_party"] = max(int(target.get("max_party", 0) or 0), party_size)
        target["constraint_role"] = "answer_candidate"
        target["profile_source"] = "task_static_service_profile.v1"

        budget = self._budget_for_price(target_price)
        for index, (person, commute) in enumerate(zip(participants, commute_minutes)):
            person["dietary"] = "vegetarian" if index == 0 else "none"
            person["budget"] = budget
            person["available_from"] = self._format_clock(max(16 * 60, meeting_start - int(commute) - 15))
            person["must_leave_by"] = self._format_clock(meeting_end + 30)

        distractors = [restaurant for restaurant in restaurants if restaurant is not target]
        for index, restaurant in enumerate(distractors):
            restaurant["constraint_role"] = "distractor"
            restaurant["profile_source"] = "task_static_service_profile.v1"
            mismatch = index % 3
            if mismatch == 0:
                restaurant["dietary_options"] = ["regular"]
                restaurant["max_party"] = max(int(restaurant.get("max_party", 0) or 0), party_size)
                restaurant["price_pp"] = min(int(restaurant.get("price_pp", target_price) or target_price), target_price)
            elif mismatch == 1:
                # A high-budget answer has no meaningful upper price ceiling
                # in the current solver, so use a capacity distractor instead.
                # Low/medium answers receive a price above their actual cap.
                if budget == "high":
                    restaurant["dietary_options"] = ["regular", "vegetarian"]
                    restaurant["max_party"] = max(1, party_size - 1)
                    restaurant["price_pp"] = target_price
                else:
                    restaurant["dietary_options"] = ["regular", "vegetarian"]
                    restaurant["max_party"] = max(int(restaurant.get("max_party", 0) or 0), party_size)
                    restaurant["price_pp"] = self._budget_cap(budget) + 20
            else:
                restaurant["dietary_options"] = ["regular", "vegetarian"]
                restaurant["max_party"] = max(1, party_size - 1)
                restaurant["price_pp"] = min(int(restaurant.get("price_pp", target_price) or target_price), target_price)

        return {
            "family": "independent_meeting",
            "relations": ["joint_meeting", "individual_time_windows"],
            "resources": [],
            "meeting_duration_min": meeting_duration,
            "task_static_service_profile": "task_static_service_profile.v1",
            "target_service_id": target.get("service_id", ""),
        }

    @staticmethod
    def _budget_for_price(price_pp: int) -> str:
        if price_pp <= 80:
            return "low"
        if price_pp <= 130:
            return "medium"
        return "high"

    @staticmethod
    def _budget_cap(budget: str) -> int:
        return {"low": 80, "medium": 130}.get(budget, 10_000)

    @staticmethod
    def _format_clock(minutes: int) -> str:
        hours, remainder = divmod(max(0, minutes), 60)
        return f"{hours:02d}:{remainder:02d}"

    @staticmethod
    def _route_minutes(origin: dict[str, Any], destination: dict[str, Any], route_data: dict[str, dict[str, int]]) -> int | None:
        origin_name = str(origin.get("home_place_name", origin.get("name", "")))[:15]
        destination_name = str(destination.get("place_name", destination.get("name", "")))[:15]
        modes = next(
            (
                value for key, value in route_data.items()
                if origin_name and destination_name and origin_name in key and destination_name in key
            ),
            {},
        )
        values = [minutes for minutes in modes.values() if isinstance(minutes, (int, float))]
        return int(min(values)) if values else None

    def precheck_commutes(self, participants: list[dict],
                          restaurants: list[dict],
                          maps_client: Any = None) -> tuple[dict, list]:
        """Query route cache (or API) for participant→restaurant commutes.

        Returns (route_data_dict, query_log_list).
        query_log: [{from, to, mode, distance_m, duration_s, walking_m, steps, source, error}]
        """
        route_data: dict[str, dict] = {}
        query_log: list[dict] = []

        for p in participants:
            pid = p.get("home_place_id", "")
            if not pid:
                continue
            for r in restaurants:
                rid = r.get("place_id", "")
                if not rid:
                    continue
                pname = p.get("home_place_name") or p.get("name", pid)[:15]
                rname = r.get("place_name") or r.get("name", rid)[:15]
                key = f"{pname}→{rname}"
                route_data[key] = {}

                for mode in ("DRIVE", "WALK", "TRANSIT"):
                    origin_p = {
                        "place_id": pid, "name": pname,
                        "location": {"lat": p.get("home_lat", 0), "lng": p.get("home_lng", 0)},
                    }
                    dest_p = {
                        "place_id": rid, "name": rname,
                        "location": r.get("location", {}),
                    }

                    if maps_client:
                        entry = self.route_cache.get_or_compute(origin_p, dest_p, mode, maps_client)
                    else:
                        cache_key = self.route_cache._make_key(origin_p, dest_p, mode)
                        entry = self.route_cache._cache.get(cache_key, {})

                    rt = entry.get("route", {})
                    dist = rt.get("distance_m", 0)
                    dur = rt.get("duration_s", 0)
                    avail = rt.get("available")
                    err = rt.get("api_error", False)

                    if dist > 0 and avail is not False:
                        route_data[key][mode] = dur // 60
                        query_log.append({
                            "from": pname, "to": rname, "mode": mode,
                            "distance_m": dist, "duration_s": dur,
                            "walking_m": rt.get("walking_distance_m", 0),
                            "steps": len(rt.get("steps", [])),
                            "source": "cache" if not maps_client else "api_or_cache",
                            "route_id": entry.get("route_id", ""),
                        })
                    elif err:
                        query_log.append({
                            "from": pname, "to": rname, "mode": mode,
                            "source": "api_error", "error": "API failed",
                        })

        # Vehicle scenarios need factual legs between participants as well as
        # participant-to-venue legs.  Cache reuse keeps this affordable.
        for origin in participants:
            for destination in participants:
                if origin.get("character_id") == destination.get("character_id"):
                    continue
                oname = origin.get("home_place_name") or origin.get("name", "")[:15]
                dname = destination.get("home_place_name") or destination.get("name", "")[:15]
                key = f"{oname}__TO__{dname}"
                if key in route_data:
                    continue
                route_data[key] = {}
                op = {"place_id": origin.get("home_place_id", ""), "name": oname,
                      "location": {"lat": origin.get("home_lat", 0), "lng": origin.get("home_lng", 0)}}
                dp = {"place_id": destination.get("home_place_id", ""), "name": dname,
                      "location": {"lat": destination.get("home_lat", 0), "lng": destination.get("home_lng", 0)}}
                for mode in ("DRIVE", "TRANSIT"):
                    entry = self.route_cache.get_or_compute(op, dp, mode, maps_client) if maps_client else \
                        self.route_cache._cache.get(self.route_cache._make_key(op, dp, mode), {})
                    route = entry.get("route", {})
                    if route.get("available") is True and route.get("duration_s", 0) > 0:
                        route_data[key][mode] = route["duration_s"] // 60
                        query_log.append({"from": oname, "to": dname, "mode": mode,
                                          "duration_s": route["duration_s"],
                                          "route_id": entry.get("route_id", ""),
                                          "source": "api_or_cache" if maps_client else "cache"})

        return route_data, query_log

    def _find_place_in_district(self, district_id: str) -> dict | None:
        places = self.district_places.get(district_id, [])
        return self.rng.choice(places) if places else None

    def _random_dietary(self) -> str:
        return self.rng.choice(["无", "素食", "清真", "无辣", "无"])

    def _guess_cuisine(self, place: dict) -> str:
        name = place.get("name", "")
        for kw, cuisine in [("火锅", "火锅"), ("日本", "日料"), ("寿司", "日料"),
                            ("川", "川菜"), ("粤", "粤菜"), ("湘", "湘菜"),
                            ("pizza", "西餐"), ("coffee", "咖啡"), ("steak", "西餐"),
                            ("面", "面食"), ("汤包", "小吃"), ("hotel", "酒店")]:
            if kw.lower() in name.lower():
                return cuisine
        return self.rng.choice(["中餐", "浙菜", "粤菜", "西餐", "日料", "火锅", "小吃"])

    def _dietary_options(self, place: dict) -> list[str]:
        options = ["常规"]
        if self.rng.random() < 0.5:
            options.append("素食")
        if self.rng.random() < 0.3:
            options.append("清真")
        if self.rng.random() < 0.4:
            options.append("无辣")
        return options
