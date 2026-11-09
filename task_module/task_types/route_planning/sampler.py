"""Route POI sampler — selects core + distractor POIs for route planning tasks.

Core POI selection:
  - origin, destination, optional waypoint
  - Distance tiers: short(2-5km), medium(5-15km), long(15-30km)
  - Avoid same-district origin/dest
  - 50% chance of waypoint (mid-district between origin and dest)

Distractor POI selection (10-15 total):
  - Same district as origin (3-4)
  - Same district as dest (3-4)
  - Same district as waypoint (2-3, if waypoint exists)
  - Random far district (2-3)
  - Same district but different place type (1-2)

Route pairing:
  - core↔core: all pairs
  - core↔distractor: 4-6 per core (random sample)
  - distractor↔distractor: 5-10 random pairs
  - Each pair gets 1-2 modes (TRANSIT/DRIVE/WALK depending on distance)
"""

from __future__ import annotations

import math
import random
from typing import Any


SEMANTIC_STOP_ACTIONS = (
    ("document_pickup", 10),
    ("parcel_collection", 8),
    ("brief_meeting", 20),
    ("errand", 15),
)


class RouteSampler:
    """Samples POIs and generates route pairs for route planning tasks."""

    DISTANCE_TIERS = {
        "short": (2000, 5000),     # 2-5 km
        "medium": (5000, 15000),   # 5-15 km
        "long": (15000, 30000),    # 15-30 km
    }

    MODE_FOR_DISTANCE = {
        "short": ["TRANSIT", "DRIVE", "WALK"],
        "medium": ["TRANSIT", "DRIVE"],
        "long": ["TRANSIT", "DRIVE"],
    }

    def __init__(self, world: dict[str, Any], characters: dict[str, Any], rng: random.Random | None = None):
        self.world = world
        self.characters = characters
        self.rng = rng or random.Random()
        self.places = world.get("places", [])
        self.districts = world.get("districts", [])

        # Build place → district mapping
        self.place_to_district: dict[str, str] = {}
        self.district_to_places: dict[str, list[dict]] = {}
        for p in self.places:
            did = p.get("district_id", "")
            pid = p.get("place_id", "")
            if did:
                self.place_to_district[pid] = did
                self.district_to_places.setdefault(did, []).append(p)

        # Build district coordinates (for distance calculation)
        self.district_coords: dict[str, tuple[float, float]] = {}
        for p in self.places:
            loc = p.get("location", {})
            did = p.get("district_id", "")
            if did and did not in self.district_coords and loc.get("lat"):
                self.district_coords[did] = (loc["lat"], loc["lng"])

    # ── Core POI selection ──

    def sample_core_pois(
        self, places: list[dict] | None = None, required_place: dict | None = None,
    ) -> dict[str, Any]:
        """Sample origin, dest, and optional waypoint with distance constraint."""
        tier_name = self.rng.choice(list(self.DISTANCE_TIERS.keys()))
        min_d, max_d = self.DISTANCE_TIERS[tier_name]

        candidate_places = places if places is not None else self.places
        origin, dest = self._sample_pair(
            min_d, max_d, max_attempts=200, places=candidate_places,
            required_place=required_place,
        )
        # Fallback: try other tiers if first tier fails
        if not origin or not dest:
            for fallback_tier, (fb_min, fb_max) in self.DISTANCE_TIERS.items():
                if fallback_tier == tier_name:
                    continue
                origin, dest = self._sample_pair(
                    fb_min, fb_max, max_attempts=200, places=candidate_places,
                    required_place=required_place,
                )
                if origin and dest:
                    tier_name = fallback_tier
                    min_d, max_d = fb_min, fb_max
                    break

        if not origin or not dest:
            return {
                "origin": None, "dest": None, "waypoint": None,
                "tier": tier_name, "mode": "", "distance_m": 0,
                "correct_duration_s": 0, "time_limit_s": 0,
                "urgency": "", "has_waypoint": False, "char_count": 1,
            }

        mode = self._pick_mode(tier_name)
        # Waypoints are required user actions, not transit transfers.  Keep a
        # 0/1/2-stop distribution so the atomic task contains genuine
        # point-to-point, one-stop, and multi-stop route planning.
        stop_count = self.rng.choices([0, 1, 2], weights=[35, 45, 20], k=1)[0]
        waypoints = self._sample_waypoints(origin, dest, stop_count, places=candidate_places)
        waypoint = waypoints[0] if waypoints else None  # compatibility field
        has_waypoint = bool(waypoints)

        # Time constraint
        correct_duration = _estimate_duration(
            self._dist(origin, dest), mode, has_waypoint,
        )
        urgency = self.rng.choices(
            ["relaxed", "tight", "urgent"], weights=[50, 30, 20], k=1,
        )[0]
        multiplier = {"relaxed": 1.5, "tight": 1.0, "urgent": 0.8}[urgency]
        time_limit = int(correct_duration * multiplier)

        # Atomic route planning always evaluates exactly one traveler.
        char_count = 1

        return {
            "origin": origin,
            "dest": dest,
            "waypoint": waypoint,
            "waypoints": waypoints,
            "semantic_stop_count": len(waypoints),
            "tier": tier_name,
            "mode": mode,
            "distance_m": self._dist(origin, dest),
            "correct_duration_s": correct_duration,
            "time_limit_s": time_limit,
            "urgency": urgency,
            "has_waypoint": has_waypoint,
            "char_count": char_count,
        }

    def _sample_pair(
        self, min_d: float, max_d: float, max_attempts: int = 100,
        places: list[dict] | None = None, required_place: dict | None = None,
    ) -> tuple[dict | None, dict | None]:
        """Sample origin-dest pair within distance range and different districts.

        Uses district-pair sampling: pick 2 different districts, pick 1 place from each,
        check distance. Falls back to random place-pair if district-pair fails.
        """
        places = places if places is not None else self.places
        if len(places) < 2:
            return (None, None)

        if required_place is not None:
            required_id = required_place.get("place_id")
            partners = [place for place in places if place.get("place_id") != required_id]
            self.rng.shuffle(partners)
            for partner in partners:
                if self.place_to_district.get(partner.get("place_id", ""), "") == self.place_to_district.get(required_id, ""):
                    continue
                if min_d <= self._dist(required_place, partner) <= max_d:
                    return (required_place, partner) if self.rng.random() < 0.5 else (partner, required_place)
            return (None, None)

        # Strategy 1: Pick two different districts with actual places
        allowed_ids = {place.get("place_id") for place in places}
        eligible_by_district = {
            district_id: [
                place for place in district_places
                if place.get("place_id") in allowed_ids
            ]
            for district_id, district_places in self.district_to_places.items()
        }
        dids = [did for did, district_places in eligible_by_district.items() if district_places]
        if len(dids) >= 2:
            for _ in range(max_attempts):
                a_did, b_did = self.rng.sample(dids, 2)
                a = self.rng.choice(eligible_by_district[a_did])
                b = self.rng.choice(eligible_by_district[b_did])
                d = self._dist(a, b)
                if min_d <= d <= max_d:
                    return (a, b)

        # Strategy 2: Random place-pair as fallback
        for _ in range(max_attempts):
            a = self.rng.choice(places)
            b = self.rng.choice(places)
            if a.get("place_id") == b.get("place_id"):
                continue
            a_did = self.place_to_district.get(a.get("place_id", ""), "")
            b_did = self.place_to_district.get(b.get("place_id", ""), "")
            if a_did == b_did:
                continue
            d = self._dist(a, b)
            if min_d <= d <= max_d:
                return (a, b)
        return (None, None)

    def _sample_waypoint(self, origin: dict, dest: dict) -> dict | None:
        """Sample a waypoint in a district between origin and dest."""
        o_loc = origin.get("location", {})
        d_loc = dest.get("location", {})
        if not o_loc.get("lat") or not d_loc.get("lat"):
            return None

        # Find districts near the midpoint
        mid_lat = (o_loc["lat"] + d_loc["lat"]) / 2
        mid_lng = (o_loc["lng"] + d_loc["lng"]) / 2
        o_did = self.place_to_district.get(origin.get("place_id", ""), "")
        d_did = self.place_to_district.get(dest.get("place_id", ""), "")

        candidates = []
        for p in self.places:
            pid = p.get("place_id", "")
            pdid = self.place_to_district.get(pid, "")
            if pdid in (o_did, d_did):
                continue
            loc = p.get("location", {})
            if not loc.get("lat"):
                continue
            dist_from_mid = _haversine(mid_lat, mid_lng, loc["lat"], loc["lng"])
            if dist_from_mid < 5000:  # within 5km of midpoint
                candidates.append(p)

        if candidates:
            return self.rng.choice(candidates)
        return None

    def _sample_waypoints(
        self, origin: dict, dest: dict, count: int, places: list[dict] | None = None,
    ) -> list[dict]:
        """Sample an ordered list of semantic stops near the O-D corridor.

        If an additional stop cannot be sampled, the task falls back to a
        shorter route shape.  It never manufactures a route or a constraint.
        """
        if count <= 0:
            return []
        original_places = self.places
        if places is not None:
            self.places = places
        first = self._sample_waypoint(origin, dest)
        if not first:
            self.places = original_places
            return []
        if count == 1:
            self.places = original_places
            return [self._decorate_semantic_stop(first)]

        excluded = {origin.get("place_id"), dest.get("place_id"), first.get("place_id")}
        origin_loc = origin.get("location", {})
        dest_loc = dest.get("location", {})
        midpoint = (
            (origin_loc.get("lat", 0) + dest_loc.get("lat", 0)) / 2,
            (origin_loc.get("lng", 0) + dest_loc.get("lng", 0)) / 2,
        )
        candidates = []
        for place in self.places:
            if place.get("place_id") in excluded:
                continue
            loc = place.get("location", {})
            if loc.get("lat") and _haversine(midpoint[0], midpoint[1], loc["lat"], loc["lng"]) <= 7000:
                candidates.append(place)
        stops = [first, self.rng.choice(candidates)] if candidates else [first]
        self.places = original_places
        return [self._decorate_semantic_stop(stop) for stop in stops]

    def _decorate_semantic_stop(self, place: dict) -> dict:
        """Attach a required action/dwell time without mutating world data."""
        action, dwell_min = self.rng.choice(SEMANTIC_STOP_ACTIONS)
        stop = dict(place)
        stop["semantic_action"] = action
        stop["dwell_min"] = dwell_min
        return stop

    # ── Distractor POI selection ──

    def sample_distractors(self, core: dict, count: int = 24) -> list[dict]:
        o_did = self.place_to_district.get(core["origin"].get("place_id", ""), "")
        d_did = self.place_to_district.get(core["dest"].get("place_id", ""), "")
        semantic_stops = core.get("waypoints") or ([core["waypoint"]] if core.get("waypoint") else [])
        waypoint_districts = {
            self.place_to_district.get(place.get("place_id", ""), "")
            for place in semantic_stops
        }
        waypoint_districts.discard("")
        exclude_ids = {
            core["origin"].get("place_id"),
            core["dest"].get("place_id"),
        }
        for place in semantic_stops:
            exclude_ids.add(place.get("place_id"))

        selected: list[dict] = []

        # Same district as origin (3-4)
        selected.extend(self._sample_from_district(o_did, 4, exclude_ids))

        # Same district as dest (3-4)
        selected.extend(self._sample_from_district(d_did, 4, exclude_ids))

        # Same district as every semantic stop (up to three each).
        for waypoint_district in waypoint_districts:
            selected.extend(self._sample_from_district(waypoint_district, 3, exclude_ids))

        # Random far district (2-3)
        far_districts = [d for d in self.district_coords if d not in {o_did, d_did, *waypoint_districts}]
        for _ in range(3):
            if far_districts:
                did = self.rng.choice(far_districts)
                pool = self.district_to_places.get(did, [])
                candidates = [p for p in pool if p.get("place_id") not in exclude_ids]
                if candidates:
                    p = self.rng.choice(candidates)
                    selected.append(p)
                    exclude_ids.add(p.get("place_id"))

        # Different place type in same district (1-2)
        if o_did:
            for _ in range(2):
                pool = self.district_to_places.get(o_did, [])
                o_type = core["origin"].get("primary_type", "")
                candidates = [
                    p for p in pool
                    if p.get("place_id") not in exclude_ids
                    and p.get("primary_type") != o_type
                ]
                if candidates:
                    p = self.rng.choice(candidates)
                    selected.append(p)
                    exclude_ids.add(p.get("place_id"))

        return selected[:count]

    def sample_nearby_unused(
        self, center: dict, excluded_ids: set[str], count: int,
        preferred_radius_m: float, fallback_radius_m: float = 5000,
    ) -> list[dict]:
        center_address = str(center.get("address") or "").strip().casefold()
        candidates = [
            place for place in self.places
            if place.get("place_id") not in excluded_ids
            and 100 <= self._dist(center, place) <= fallback_radius_m
            and not (
                center_address
                and str(place.get("address") or "").strip().casefold() == center_address
            )
        ]
        candidates.sort(key=lambda place: self._dist(center, place))
        preferred = [place for place in candidates if self._dist(center, place) <= preferred_radius_m]
        selected = preferred[:count]
        if len(selected) < count:
            selected_ids = {place.get("place_id") for place in selected}
            selected.extend(
                place for place in candidates
                if place.get("place_id") not in selected_ids
            )
        return selected[:count]

    def sample_expansion_core(self, pool_places: list[dict]) -> tuple[dict | None, dict | None]:
        excluded = {place.get("place_id") for place in pool_places}
        anchors = list(pool_places)
        self.rng.shuffle(anchors)
        for radius in (3000, 5000):
            for anchor in anchors:
                candidates = [
                    place for place in self.places
                    if place.get("place_id") not in excluded
                    and 1000 <= self._dist(anchor, place) <= radius
                ]
                if candidates:
                    return anchor, self.rng.choice(candidates)
        return None, None

    def sample_cached_core(self, pool_places: list[dict], route_cache: Any) -> dict[str, Any]:
        """Sample a 2-4 POI directed chain using only available cached routes."""
        by_id = {str(place.get("place_id") or ""): place for place in pool_places}
        adjacency = self.build_cached_adjacency(pool_places, route_cache)
        return self.sample_cached_core_from_adjacency(by_id, adjacency)

    def build_cached_adjacency(
        self, pool_places: list[dict], route_cache: Any,
    ) -> dict[str, dict[str, set[str]]]:
        """Build the available directed-mode graph used by cached-chain sampling."""
        by_id = {str(place.get("place_id") or ""): place for place in pool_places}
        adjacency: dict[str, dict[str, set[str]]] = {}
        for origin_id, origin in by_id.items():
            for destination_id, destination in by_id.items():
                if origin_id == destination_id:
                    continue
                modes = {
                    mode for mode in self._modes_for_distance(self._dist(origin, destination))
                    if (route_cache.get(origin, destination, mode) or {}).get("route", {}).get("available") is True
                }
                if modes:
                    adjacency.setdefault(origin_id, {})[destination_id] = modes
        return adjacency

    def sample_cached_core_from_adjacency(
        self, by_id: dict[str, dict], adjacency: dict[str, dict[str, set[str]]],
    ) -> dict[str, Any]:
        """Sample one cached chain from a previously built available-route graph."""

        stop_count = self.rng.choices([0, 1, 2], weights=[35, 45, 20], k=1)[0]
        chain_length = stop_count + 2
        origins = list(adjacency)
        self.rng.shuffle(origins)
        for origin_id in origins:
            chains = [[origin_id]]
            for _ in range(chain_length - 1):
                expanded = []
                for chain in chains:
                    for next_id, modes in adjacency.get(chain[-1], {}).items():
                        if next_id in chain:
                            continue
                        if modes:
                            expanded.append([*chain, next_id])
                self.rng.shuffle(expanded)
                chains = expanded[:200]
                if not chains:
                    break
            if not chains:
                continue
            chain_ids = self.rng.choice(chains)
            chain = [by_id[place_id] for place_id in chain_ids]
            leg_modes = [
                self.rng.choice(sorted(adjacency[from_id][to_id]))
                for from_id, to_id in zip(chain_ids, chain_ids[1:])
            ]
            origin, destination = chain[0], chain[-1]
            waypoints = [self._decorate_semantic_stop(place) for place in chain[1:-1]]
            distance_m = self._dist(origin, destination)
            tier = "short" if distance_m < 5000 else "medium" if distance_m < 15000 else "long"
            mode = leg_modes[0] if len(set(leg_modes)) == 1 else "MIXED"
            correct_duration = sum(
                _estimate_duration(self._dist(a, b), leg_mode, False)
                for a, b, leg_mode in zip(chain, chain[1:], leg_modes)
            )
            urgency = self.rng.choices(["relaxed", "tight", "urgent"], weights=[50, 30, 20], k=1)[0]
            multiplier = {"relaxed": 1.5, "tight": 1.0, "urgent": 0.8}[urgency]
            return {
                "origin": origin, "dest": destination,
                "waypoint": waypoints[0] if waypoints else None,
                "waypoints": waypoints, "semantic_stop_count": len(waypoints),
                "tier": tier, "mode": mode, "leg_modes": leg_modes,
                "distance_m": distance_m,
                "correct_duration_s": correct_duration,
                "time_limit_s": int(correct_duration * multiplier),
                "urgency": urgency, "has_waypoint": bool(waypoints), "char_count": 1,
            }
        return {"origin": None, "dest": None, "waypoint": None, "waypoints": []}

    def _sample_from_district(
        self, did: str, n: int, exclude_ids: set,
    ) -> list[dict]:
        pool = self.district_to_places.get(did, [])
        candidates = [p for p in pool if p.get("place_id") not in exclude_ids]
        if not candidates:
            return []
        k = min(n, len(candidates))
        chosen = self.rng.sample(candidates, k)
        for p in chosen:
            exclude_ids.add(p.get("place_id"))
        return chosen

    # ── Route pairing ──

    def generate_pairs(
        self, core: dict, distractors: list[dict],
    ) -> list[dict[str, Any]]:
        """Generate route pairs to cache for this task."""
        # Dense directed subgraph: every core/context POI pair receives all
        # distance-compatible modes. RouteCache deduplicates cross-task calls.
        places = [core["origin"], core["dest"]]
        places.extend(core.get("waypoints") or ([core["waypoint"]] if core.get("waypoint") else []))
        places.extend(distractors)
        pairs: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for origin in places:
            for destination in places:
                if origin.get("place_id") == destination.get("place_id"):
                    continue
                for mode in self._modes_for_distance(self._dist(origin, destination)):
                    key = (origin.get("place_id", ""), destination.get("place_id", ""), mode)
                    if key not in seen:
                        seen.add(key)
                        pairs.append({"from": origin, "to": destination, "mode": mode})
        return pairs

    def generate_pool_pairs(self, pool_places: list[dict]) -> list[dict[str, Any]]:
        """Build the complete directed, distance-compatible route matrix."""
        pairs: list[dict[str, Any]] = []
        for origin in pool_places:
            for destination in pool_places:
                if origin.get("place_id") == destination.get("place_id"):
                    continue
                for mode in self._modes_for_distance(self._dist(origin, destination)):
                    pairs.append({"from": origin, "to": destination, "mode": mode})
        return pairs

        pairs: list[dict[str, Any]] = []

        core_list = [core["origin"], core["dest"]]
        if core.get("waypoint"):
            core_list.append(core["waypoint"])

        main_mode = core["mode"]
        all_modes = ("DRIVE", "TRANSIT", "WALK")

        # core ↔ core: request ALL modes for every core pair
        for i in range(len(core_list)):
            for j in range(len(core_list)):
                if i == j:
                    continue
                for m in all_modes:
                    if i == 0 and j == 1 and m == main_mode:
                        continue  # main_mode already cached by caller pre-check
                    pairs.append({"from": core_list[i], "to": core_list[j], "mode": m})

        # core ↔ distractor (6-8 per core point, mixed modes)
        for cp in core_list[:2]:
            sample = self.rng.sample(distractors, min(8, len(distractors)))
            for dp in sample:
                m = self._pick_mode_for_distance(self._dist(cp, dp))
                pairs.append({"from": cp, "to": dp, "mode": m})

        # distractor ↔ distractor (5-10 random, mixed modes)
        if len(distractors) >= 2:
            for _ in range(min(10, len(distractors) * 2 // 3)):
                a, b = self.rng.sample(distractors, 2)
                m = self._pick_mode_for_distance(self._dist(a, b))
                pairs.append({"from": a, "to": b, "mode": m})

        # Include wrong direction + wrong destination distracters
        if distractors:
            wrong_dest = self.rng.choice(distractors)
            pairs.append({"from": core_list[0], "to": wrong_dest, "mode": main_mode})

        return pairs

    # ── Constraint derivation ──

    def derive_constraints(
        self, core: dict, route_cache: Any, rng: random.Random | None = None,
    ) -> dict[str, Any]:
        """Derive constraints from cached routes. Algorithm:

        1. Enumerate all candidate plans (leg combinations)
        2. Randomly assign difficulty and feasibility
        3. Generate constraints from plan properties
        """
        rng = rng or self.rng
        origin = core["origin"]
        dest = core["dest"]
        waypoint = core.get("waypoint")
        stop_duration_min = rng.randint(3, 15) if waypoint else 0

        # Collect all available routes for each leg
        leg_routes: dict[str, list[dict]] = {}
        for leg_name, from_p, to_p in [("O→D", origin, dest)] + \
              ([("O→W", origin, waypoint), ("W→D", waypoint, dest)] if waypoint else []):
            routes = []
            for mode in ("DRIVE", "TRANSIT", "WALK"):
                key = route_cache._make_key(from_p, to_p, mode)
                entry = route_cache._cache.get(key, {})
                r = entry.get("route", {})
                if (r.get("distance_m", 0) > 0 and
                        r.get("available") is not False and
                        not r.get("api_error")):
                    routes.append({
                        "route_id": entry.get("route_id", ""),
                        "mode": mode,
                        "duration_s": r.get("duration_s", 0),
                        "walking_m": r.get("walking_distance_m", 0),
                        "distance_m": r.get("distance_m", 0),
                    })
            leg_routes[leg_name] = routes

        # Build candidate plans
        if waypoint:
            plans = []
            for r1 in leg_routes.get("O→W", []):
                for r2 in leg_routes.get("W→D", []):
                    plans.append({
                        "leg_route_ids": [r1["route_id"], r2["route_id"]],
                        "total_duration_s": r1["duration_s"] + r2["duration_s"] + stop_duration_min * 60,
                        "total_walking_m": r1["walking_m"] + r2["walking_m"],
                        "modes": [r1["mode"], r2["mode"]],
                    })
        else:
            plans = []
            for r in leg_routes.get("O→D", []):
                plans.append({
                    "leg_route_ids": [r["route_id"]],
                    "total_duration_s": r["duration_s"],
                    "total_walking_m": r["walking_m"],
                    "modes": [r["mode"]],
                })

        if not plans:
            return {"error": "no valid plans", "expected_feasible": False,
                    "acceptable_plan_ids": [], "minimum_possible_duration_min": 0}

        min_dur = min(p["total_duration_s"] for p in plans)
        min_dur_min = max(1, min_dur // 60)

        # Feasibility assignment: 20% infeasible
        is_infeasible = rng.random() < 0.2
        all_modes = sorted(set(m for p in plans for m in p["modes"]))

        if is_infeasible:
            time_budget_min = max(1, int(min_dur_min * 0.7))
            violated = ["arrival_deadline"]
            return {
                "departure_time_str": "",
                "departure_minute": 0,
                "arrival_deadline_str": "",
                "time_budget_min": time_budget_min,
                "stop_duration_min": stop_duration_min,
                "forbidden_modes": [],
                "max_walking_distance_m": None,
                "expected_feasible": False,
                "acceptable_plan_ids": [],
                "minimum_possible_duration_min": min_dur_min,
                "violated_constraints": violated,
            }

        # Feasible task: pick difficulty and target plan
        difficulty = rng.choices(["easy", "medium", "hard"], weights=[30, 50, 20], k=1)[0]

        # Sort plans by duration ascending, pick one of the top-fastest as target
        plans_sorted = sorted(plans, key=lambda p: p["total_duration_s"])
        target_count = max(1, len(plans_sorted) // 3)
        target = rng.choice(plans_sorted[:target_count])

        # Build constraints
        forbidden_modes = []
        if difficulty in ("medium", "hard") and len(all_modes) > 1:
            fastest_mode = plans_sorted[0]["modes"][0]
            if fastest_mode not in target["modes"]:
                forbidden_modes = [fastest_mode]
            elif len(all_modes) > 1:
                alternative = next(m for m in all_modes if m != fastest_mode)
                forbidden_modes = [alternative]
        if difficulty == "hard" and len(forbidden_modes) < 2 and len(all_modes) > 2:
            extra = next(m for m in all_modes if m not in forbidden_modes and m not in target["modes"])
            if extra:
                forbidden_modes.append(extra)

        max_walking_m = None
        if difficulty in ("medium", "hard"):
            walk_values = sorted(p["total_walking_m"] for p in plans_sorted)
            target_walk = target["total_walking_m"]
            # Set threshold tight: between target's walk and the next higher walk
            higher = [w for w in walk_values if w > target_walk]
            if higher:
                max_walking_m = int((target_walk + higher[0]) / 2)
            else:
                max_walking_m = int(target_walk * 1.3)

        time_budget_min = max(1, (target["total_duration_s"] // 60) + rng.randint(5, 15))

        # Collect acceptable plans (all plans satisfying constraints)
        acceptable = []
        for p in plans_sorted:
            ok = True
            if any(m in forbidden_modes for m in p["modes"]):
                ok = False
            if max_walking_m is not None and p["total_walking_m"] > max_walking_m:
                ok = False
            if (p["total_duration_s"] // 60) > time_budget_min:
                ok = False
            if ok:
                acceptable.append(p["leg_route_ids"])

        return {
            "departure_time_str": "",
            "departure_minute": 0,
            "arrival_deadline_str": "",
            "time_budget_min": time_budget_min,
            "stop_duration_min": stop_duration_min,
            "forbidden_modes": forbidden_modes,
            "max_walking_distance_m": max_walking_m,
            "expected_feasible": True,
            "acceptable_plan_ids": acceptable,
            "minimum_possible_duration_min": min_dur_min,
            "violated_constraints": [],
        }

    # ── Helpers ──

    def derive_constraints_for_stops(
        self, core: dict, route_cache: Any, rng: random.Random | None = None,
        snapshot_id: str = "static_v1",
    ) -> dict[str, Any]:
        """Solve required ordered O-(W*)-D plans from API-materialized legs."""
        rng = rng or self.rng
        origin = core["origin"]
        destination = core["dest"]
        waypoints = core.get("waypoints") or ([] if not core.get("waypoint") else [core["waypoint"]])
        ordered_places = [origin, *waypoints, destination]
        stop_durations_min = [int(stop.get("dwell_min", 10)) for stop in waypoints]
        stop_duration_min = sum(stop_durations_min)
        plans = [{"leg_route_ids": [], "total_duration_s": 0, "total_walking_m": 0, "modes": []}]

        for from_place, to_place in zip(ordered_places, ordered_places[1:]):
            routes = []
            for mode in self._modes_for_distance(self._dist(from_place, to_place)):
                entry = route_cache.get(from_place, to_place, mode, snapshot_id=snapshot_id) or {}
                route = entry.get("route", {})
                if route.get("available") is True and route.get("duration_s", 0) > 0:
                    routes.append({"route_id": entry.get("route_id", ""), "mode": mode,
                                   "duration_s": route["duration_s"],
                                   "walking_m": route.get("walking_distance_m", 0)})
            next_plans = []
            for plan in plans:
                for route in routes:
                    next_plans.append({
                        "leg_route_ids": [*plan["leg_route_ids"], route["route_id"]],
                        "total_duration_s": plan["total_duration_s"] + route["duration_s"],
                        "total_walking_m": plan["total_walking_m"] + route["walking_m"],
                        "modes": [*plan["modes"], route["mode"]],
                    })
            plans = next_plans
            if not plans:
                return {"error": "missing_api_leg", "expected_feasible": False,
                        "acceptable_plan_ids": [], "minimum_possible_duration_min": 0}

        for plan in plans:
            plan["total_duration_s"] += stop_duration_min * 60
        plans.sort(key=lambda plan: plan["total_duration_s"])
        minimum_duration_min = max(1, plans[0]["total_duration_s"] // 60)
        if rng.random() < 0.2:
            return {
                "departure_time_str": "", "departure_minute": 0, "arrival_deadline_str": "",
                "time_budget_min": max(1, int(minimum_duration_min * 0.7)),
                "stop_duration_min": stop_duration_min, "stop_durations_min": stop_durations_min,
                "semantic_stop_count": len(waypoints),
                "forbidden_modes": [], "max_walking_distance_m": None,
                "expected_feasible": False, "acceptable_plan_ids": [],
                "minimum_possible_duration_min": minimum_duration_min,
                "violated_constraints": ["arrival_deadline"],
            }

        difficulty = rng.choices(["easy", "medium", "hard"], weights=[30, 50, 20], k=1)[0]
        target = rng.choice(plans[:max(1, len(plans) // 3)])
        all_modes = sorted({mode for plan in plans for mode in plan["modes"]})
        forbidden_modes: list[str] = []
        alternatives = [mode for mode in all_modes if mode not in target["modes"]]
        if difficulty in {"medium", "hard"} and alternatives:
            forbidden_modes.append(alternatives[0])
        max_walking = None
        if difficulty in {"medium", "hard"}:
            higher = sorted({p["total_walking_m"] for p in plans if p["total_walking_m"] > target["total_walking_m"]})
            max_walking = int((target["total_walking_m"] + higher[0]) / 2) if higher else int(target["total_walking_m"] * 1.3)
        time_budget_min = max(1, target["total_duration_s"] // 60 + rng.randint(5, 15))
        acceptable = [
            plan["leg_route_ids"] for plan in plans
            if not any(mode in forbidden_modes for mode in plan["modes"])
            and (max_walking is None or plan["total_walking_m"] <= max_walking)
            and plan["total_duration_s"] // 60 <= time_budget_min
        ]
        if not acceptable:
            return {"error": "constraints_removed_all_plans", "expected_feasible": False,
                    "acceptable_plan_ids": [], "minimum_possible_duration_min": minimum_duration_min}
        return {
            "departure_time_str": "", "departure_minute": 0, "arrival_deadline_str": "",
            "time_budget_min": time_budget_min, "stop_duration_min": stop_duration_min,
            "stop_durations_min": stop_durations_min,
            "semantic_stop_count": len(waypoints), "forbidden_modes": forbidden_modes,
            "max_walking_distance_m": max_walking, "expected_feasible": True,
            "acceptable_plan_ids": acceptable, "minimum_possible_duration_min": minimum_duration_min,
            "violated_constraints": [],
        }

    def _dist(self, a: dict | None, b: dict | None) -> float:
        if not a or not b:
            return 0
        la = a.get("location", {})
        lb = b.get("location", {})
        return _haversine(
            la.get("lat", 0), la.get("lng", 0),
            lb.get("lat", 0), lb.get("lng", 0),
        )

    @staticmethod
    def distance(a: dict | None, b: dict | None) -> float:
        if not a or not b:
            return 0
        la = a.get("location", {})
        lb = b.get("location", {})
        return _haversine(la.get("lat", 0), la.get("lng", 0), lb.get("lat", 0), lb.get("lng", 0))

    def _pick_mode(self, tier: str) -> str:
        options = self.MODE_FOR_DISTANCE.get(tier, ["TRANSIT"])
        return self.rng.choice(options)

    def _pick_mode_for_distance(self, distance_m: float) -> str:
        if distance_m < 100:
            return "WALK"
        if distance_m < 1000:
            return "WALK"
        if distance_m < 5000:
            return self.rng.choice(["WALK", "TRANSIT", "DRIVE"])
        return self.rng.choice(["TRANSIT", "DRIVE"])

    @staticmethod
    def _modes_for_distance(distance_m: float) -> tuple[str, ...]:
        if distance_m < 100:
            return ()
        if distance_m < 1000:
            return ("WALK",)
        if distance_m < 5000:
            return ("WALK", "TRANSIT", "DRIVE")
        return ("TRANSIT", "DRIVE")


# ── Geometry helpers ──

def _haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _estimate_duration(distance_m: float, mode: str, has_waypoint: bool) -> int:
    speeds = {"WALK": 1.4, "DRIVE": 8.3, "TRANSIT": 5.0}
    speed_ms = speeds.get(mode, 5.0)
    base = distance_m / speed_ms
    if has_waypoint:
        base *= 1.3
    return int(base)
