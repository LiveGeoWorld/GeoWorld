"""Itinerary sampler — groups POIs by district, builds daily option menus for LLM.

Key design change (方案 C):
  - Sampler selects districts and builds candidate POI pools per half-day slot
  - LLM decides: which optional events to include + which POI from each candidate pool + times
  - Geographic coherence ensured by pooling candidates within same district

"""

from __future__ import annotations

import math
import random
from typing import Any

from task_module.task_types.route_planning.sampler import RouteSampler

CATEGORY_LABELS: dict[str, str] = {
    "restaurant": "restaurant",
    "cafe": "cafe",
    "bar": "cafe",
    "shopping": "personal_errand",
    "lodging": "hotel",
    "tourist_attraction": "attraction",
    "museum": "attraction",
    "movie_theater": "attraction",
    "park": "outdoor",
    "transit_station": "transit",
    "hospital": "appointment",
    "medical_care": "appointment",
    "pharmacy": "personal_errand",
    # Historical world versions retain these former broad service IDs.
    "hotel": "hotel",
    "attraction": "attraction",
    "retail": "personal_errand",
    "healthcare": "appointment",
    "restaurant_reservation": "restaurant",
    "hotel_booking": "hotel",
    "attraction_visit": "attraction",
    "local_visit": "cafe",
    "outdoor_visit": "outdoor",
    "retail_visit": "personal_errand",
    "healthcare_visit": "appointment",
    "business_meeting": "business_meeting",
    "office_visit": "business_meeting",
    "coworking_visit": "business_meeting",
}

OPENING_HOURS: dict[str, tuple[str, str]] = {
    "restaurant": ("11:00", "22:00"),
    "hotel": ("00:00", "24:00"),
    "attraction": ("09:00", "17:00"),
    "cafe": ("08:00", "20:00"),
    "transit": ("00:00", "24:00"),
    "outdoor": ("06:00", "22:00"),
    "personal_errand": ("10:00", "20:00"),
    "appointment": ("09:00", "17:00"),
    "business_meeting": ("09:00", "18:00"),
}

DURATION_MIN: dict[str, int] = {
    "restaurant": 90, "hotel": 30, "attraction": 120, "cafe": 45, "transit": 60,
    "outdoor": 90, "personal_errand": 60, "appointment": 60, "business_meeting": 90,
}

SLOT_DEFS: list[dict] = [
    {"event_type": "breakfast", "category": "cafe", "time_range": ("07:30", "09:00"), "half": "morning"},
    {"event_type": "morning_activity", "category": "attraction", "time_range": ("09:00", "12:00"), "half": "morning"},
    {"event_type": "lunch", "category": "restaurant", "time_range": ("12:00", "14:00"), "half": "morning"},
    {"event_type": "afternoon_activity", "category": "attraction", "time_range": ("14:00", "17:00"), "half": "afternoon"},
    {"event_type": "dinner", "category": "restaurant", "time_range": ("17:00", "19:00"), "half": "afternoon"},
    {"event_type": "evening_leisure", "category": "cafe", "time_range": ("19:00", "22:00"), "half": "afternoon"},
]

WEEKDAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


class ItinerarySampler:
    """Samples districts + candidate POIs and builds daily option menus."""

    def __init__(self, world: dict[str, Any], characters: dict[str, Any],
                 route_cache: Any, rng: random.Random | None = None,
                 routing_place_ids: set[str] | None = None):
        self.world = world
        self.characters = characters
        self.route_cache = route_cache
        self.rng = rng or random.Random()
        self.route_sampler = RouteSampler(world, characters, rng=self.rng)
        self.routing_place_ids = {
            str(place_id) for place_id in (routing_place_ids or set()) if place_id
        }

        self.places = world.get("places", [])
        self.services = world.get("services", [])
        self.districts = world.get("districts", [])

        self.place_dict: dict[str, dict] = {p["place_id"]: p for p in self.places if p.get("place_id")}
        self.place_district: dict[str, str] = {}
        self.district_places: dict[str, list[dict]] = {}
        for p in self.places:
            did = p.get("district_id", "")
            pid = p.get("place_id", "")
            if did and pid:
                self.place_district[pid] = did
                self.district_places.setdefault(did, []).append(p)

        self.district_coords: dict[str, tuple[float, float]] = {}
        for d in self.districts:
            loc = d.get("center", d.get("location", {}))
            lat = loc.get("lat", 0)
            lng = loc.get("lng", 0)
            if lat and lng:
                self.district_coords[d["district_id"]] = (lat, lng)

    # ── character / duration / hotel ──

    def sample_character(self) -> dict:
        chars = list(self.characters.get("characters", []))
        if not chars:
            return {}
        c = self.rng.choice(chars)
        mob = c.get("mobility", {})
        prefs = c.get("preferences", {})
        preferred_modes = [str(value).lower() for value in mob.get("preferred_modes", [])]
        if mob.get("has_car") or any(value in {"drive", "car", "driving"} for value in preferred_modes):
            preferred_transit = "DRIVE"
        elif any(value in {"train", "bus", "subway", "transit"} for value in preferred_modes):
            preferred_transit = "TRANSIT"
        elif any(value in {"walk", "walking"} for value in preferred_modes):
            preferred_transit = "WALK"
        else:
            preferred_transit = self.rng.choice(["TRANSIT", "DRIVE", "TRANSIT"])
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
            "preferred_transit": preferred_transit,
            "budget_daily": 0,
            "preferences": prefs,
        }

    def determine_duration(self) -> int:
        return self.rng.choices([2, 3, 4, 5], weights=[0.30, 0.45, 0.20, 0.05])[0]

    def sample_start_weekday(self) -> int:
        return self.rng.randrange(7)

    def sample_scenario_profile(self, rng: random.Random | None = None) -> dict[str, Any]:
        """Select an itinerary family from services actually available in this city.

        This is deliberately capability-driven.  A city without a meeting-like
        service cannot silently receive a fictional business appointment.
        """
        rng = rng or self.rng
        available = {
            CATEGORY_LABELS.get(service_type, "")
            for service in self.services
            for service_type in _service_types(service)
        }
        available.discard("")
        families: list[tuple[str, str]] = [("travel_leisure", "attraction")]
        if "business_meeting" in available:
            families.append(("travel_business", "business_meeting"))
        if "personal_errand" in available:
            families.append(("travel_personal", "personal_errand"))
        elif "appointment" in available:
            families.append(("travel_personal", "appointment"))
        _, obligation_category = rng.choice(families)
        return {
            "obligation_category": obligation_category,
            "available_categories": sorted(available),
            "departure_ends_task": True,
            # The generator assigns the target from its requested batch quota.
            # Sampling must not silently impose a global negative-task ratio.
            "feasibility_target": "feasible",
        }

    def build_scenario_sequence(
        self, num_days: int, rng: random.Random | None = None
    ) -> list[str]:
        """Build one contiguous scenario sequence; mixed trips switch once."""
        rng = rng or self.rng
        available = {
            CATEGORY_LABELS.get(service_type, "")
            for service in self.services
            for service_type in _service_types(service)
        }
        families = ["travel_leisure"]
        if "business_meeting" in available:
            families.append("travel_business")
        if available & {"personal_errand", "appointment"}:
            families.append("travel_personal")
        first = rng.choice(families)
        if num_days < 2 or len(families) < 2 or rng.random() >= 0.30:
            return [first] * num_days
        second = rng.choice([family for family in families if family != first])
        switch_day = rng.randint(1, num_days - 1)
        return [first] * switch_day + [second] * (num_days - switch_day)

    def available_service_types(self) -> list[str]:
        """Return factual service types that have Routing-covered POIs."""
        lodging_types = {"lodging", "hotel", "hotel_booking"}
        result = {
            service_type
            for service in self.services
            for service_type in _service_types(service)
            if service_type in CATEGORY_LABELS
            and service_type not in lodging_types
            and str(service.get("place_id") or "") in self.routing_place_ids
            and str(service.get("place_id") or "") in self.place_dict
        }
        return sorted(item for item in result if item)

    def routing_activity_candidates(self) -> list[dict[str, Any]]:
        """Return the frozen Routing-covered universe used by requirement matching."""
        return self.activity_candidates(routing_only=True)

    def activity_candidates(self, *, routing_only: bool) -> list[dict[str, Any]]:
        """Project factual service capabilities into task candidate records."""
        result: list[dict[str, Any]] = []
        for service in self.services:
            place_id = str(service.get("place_id") or "")
            place = self.place_dict.get(place_id)
            if not place or (routing_only and place_id not in self.routing_place_ids):
                continue
            for service_type in _service_types(service):
                category = CATEGORY_LABELS.get(service_type)
                if not category or service_type in {"lodging", "hotel", "hotel_booking"}:
                    continue
                details = _google_details(place)
                result.append({
                    "place_id": place_id,
                    "name": place.get("name", "?"),
                    "service_type": service_type,
                    "primary_service_type": str(service.get("primary_service_type") or service.get("service_type") or ""),
                    "service_types": _service_types(service),
                    "required_service_types": _required_service_types(
                        str(service.get("primary_service_type") or service.get("service_type") or ""),
                        service_type,
                    ),
                    "primary_type": str(place.get("primary_type") or ""),
                    "location": dict(place.get("location") or {}),
                    "address": str(place.get("address") or ""),
                    "district_id": place.get("district_id", ""),
                    "district_name": self._district_name(place.get("district_id", "")),
                    "price_level": self._price_level(service, place),
                    "price_range": _price_range(details),
                    "service_features": _service_features(details),
                    "weekly_opening_intervals": _weekly_opening_intervals(details),
                    "rating": round(place.get("map_projection", {}).get("rating", 0) or 0, 1),
                })
        return result

    def sample_poi_for_service_type(
        self,
        service_type: str,
        *,
        excluded_place_ids: set[str] | None = None,
        weekday_index: int | None = None,
    ) -> dict[str, Any] | None:
        """Seeded concrete-POI selection after an Agent chooses only a type."""
        category = CATEGORY_LABELS.get(service_type)
        if not category:
            return None
        excluded = excluded_place_ids or set()
        candidates: list[dict[str, Any]] = []
        for service in self.services:
            if service_type not in _service_types(service):
                continue
            place_id = str(service.get("place_id") or "")
            place = self.place_dict.get(place_id)
            if not place or place_id not in self.routing_place_ids or place_id in excluded:
                continue
            location = place.get("location", {})
            if location.get("lat") is None or location.get("lng") is None:
                continue
            details = _google_details(place)
            weekly_hours = _weekly_opening_intervals(details)
            day_intervals = list(weekly_hours.get(str(weekday_index), [])) if weekday_index is not None else []
            if weekday_index is not None and not day_intervals:
                continue
            primary_service_type = str(
                service.get("primary_service_type") or service.get("service_type") or ""
            )
            candidates.append({
                "service_id": str(service.get("service_id") or ""),
                "service_type": service_type,
                "selected_service_type": service_type,
                "primary_service_type": primary_service_type,
                "service_types": _service_types(service),
                "required_service_types": _required_service_types(
                    primary_service_type, service_type,
                ),
                "place_id": place_id,
                "name": place.get("name", "?"),
                "location": location,
                "district_id": place.get("district_id", ""),
                "district_name": self._district_name(place.get("district_id", "")),
                "category": category,
                "rating": round(place.get("map_projection", {}).get("rating", 0) or 0, 1),
                "primary_type": str(place.get("primary_type") or ""),
                "price_level": self._price_level(service, place),
                "price_range": _price_range(details),
                "service_features": _service_features(details),
                "weekly_opening_intervals": weekly_hours,
                "opening_intervals": day_intervals,
                "opening_open": _format_minutes(min(interval[0] for interval in day_intervals)),
                "opening_close": _format_minutes(max(interval[1] for interval in day_intervals)),
                "opening_hours": (
                    f"{_format_minutes(min(interval[0] for interval in day_intervals))}-"
                    f"{_format_minutes(max(interval[1] for interval in day_intervals))}"
                ),
                "duration_typical_min": DURATION_MIN.get(category, 60),
            })
        return self.rng.choice(candidates) if candidates else None

    def sample_hotel(self) -> dict | None:
        candidates = []
        for svc in self.services:
            if not set(_service_types(svc)).intersection({"lodging", "hotel", "hotel_booking"}):
                continue
            pid = svc.get("place_id", "")
            if pid not in self.routing_place_ids:
                continue
            place = self.place_dict.get(pid)
            if not place:
                continue
            candidates.append({
                "service_id": svc.get("service_id", ""),
                "place_id": pid,
                "name": place.get("name", "?"),
                "location": place.get("location", {}),
                "district_id": place.get("district_id", ""),
                "district_name": self._district_name(place.get("district_id", "")),
                "rating": place.get("map_projection", {}).get("rating", 4.0) or 4.0,
                "price_level": self._price_level(svc),
            })
        if not candidates:
            return None
        self.rng.shuffle(candidates)
        return candidates[0]

    # ── POI collection by district ──

    def _collect_pois(self) -> dict[str, dict[str, list[dict]]]:
        """Returns {district_id: {category: [poi_dict, ...]}}."""
        by_district: dict[str, dict[str, list[dict]]] = {}
        for svc in self.services:
            stype = svc.get("service_type", "")
            cat = CATEGORY_LABELS.get(stype)
            if not cat:
                continue
            pid = svc.get("place_id", "")
            if pid not in self.routing_place_ids:
                continue
            place = self.place_dict.get(pid)
            if not place:
                continue
            loc = place.get("location", {})
            if not loc.get("lat") or not loc.get("lng"):
                continue
            did = place.get("district_id", "")
            if not did:
                continue
            opening = OPENING_HOURS.get(cat, ("09:00", "17:00"))
            poi = {
                "service_id": svc.get("service_id", ""),
                "place_id": pid,
                "name": place.get("name", "?"),
                "location": loc,
                "district_id": did,
                "district_name": self._district_name(did),
                "category": cat,
                "rating": round(place.get("map_projection", {}).get("rating", 0) or 0, 1),
                "price_level": self._price_level(svc),
                "opening_hours": f"{opening[0]}-{opening[1]}",
                "opening_open": opening[0],
                "opening_close": opening[1],
                "duration_typical_min": DURATION_MIN.get(cat, 60),
            }
            by_district.setdefault(did, {}).setdefault(cat, []).append(poi)
        return by_district

    def _pick_transit_hubs(self, hotel_did: str, count: int) -> list[dict]:
        """Pick transport hubs, preferring districts outside the hotel area."""
        transit: list[dict] = []
        for p in self.places:
            if str(p.get("place_id") or "") not in self.routing_place_ids:
                continue
            ptype = p.get("primary_type", "")
            # Map builders use Google place types, where subway_station and
            # bus_stop are common equivalents of train/bus stations.
            if ptype not in ("train_station", "bus_station", "subway_station", "bus_stop"):
                continue
            loc = p.get("location", {})
            if not loc.get("lat"):
                continue
            did = p.get("district_id", "")
            transit.append({
                "place_id": p.get("place_id", ""),
                "name": p.get("name", "?"),
                "location": loc,
                "district_id": did,
                "district_name": self._district_name(did),
                "category": "transit",
                "rating": 0,
                "price_level": "FREE",
                "opening_hours": "00:00-24:00",
                "opening_open": "00:00",
                "opening_close": "24:00",
                "duration_typical_min": 60,
            })
        far = [t for t in transit if t["district_id"] != hotel_did]
        near = [t for t in transit if t["district_id"] == hotel_did]
        self.rng.shuffle(far)
        self.rng.shuffle(near)
        return (far + near)[:count]

    # ── district selection ──

    def _pick_districts_for_day(self, day_idx: int, num_days: int,
                                 hotel_did: str, used_dids: set[str],
                                 pois_by_district: dict[str, dict[str, list[dict]]]) -> tuple[str, str]:
        """Pick morning_district and afternoon_district for this day."""
        viable = []
        for did, cats in pois_by_district.items():
            has_restaurant = len(cats.get("restaurant", [])) >= 2
            has_attraction = len(cats.get("attraction", [])) >= 1
            if has_restaurant and has_attraction:
                viable.append(did)

        fresh = [d for d in viable if d not in used_dids]
        reused = [d for d in viable if d in used_dids]
        self.rng.shuffle(fresh)
        self.rng.shuffle(reused)
        ordered = fresh + reused

        if not ordered:
            ordered = [hotel_did] if hotel_did in viable else viable[:1]

        if len(ordered) >= 2 and self.rng.random() < 0.6:
            am_did, pm_did = ordered[0], ordered[1]
            used_dids.update([am_did, pm_did])
        elif len(ordered) >= 1:
            am_did = pm_did = ordered[0]
            used_dids.add(am_did)
        else:
            am_did = pm_did = hotel_did

        return am_did, pm_did

    # ── build daily menus ──

    def build_daily_menus(self, hotel: dict, num_days: int, transit_hubs: list[dict],
                          scenario: dict[str, Any] | None = None) -> tuple[list[dict], list[dict]]:
        """Returns (menus, all_pois_list).

        menus: [{day, fixed_events, required_slots, optional_slots}]
        Each slot: {event_type, candidates: [{place_id, name, ...}], time_range, half}
        """
        scenario = scenario or self.sample_scenario_profile()
        pois_by_district = self._collect_pois()
        hotel_did = hotel.get("district_id", "")
        used_dids: set[str] = set()
        menus: list[dict] = []
        all_pois: list[dict] = [hotel] + list(transit_hubs)

        arrival = transit_hubs[0] if transit_hubs else None
        departure = transit_hubs[-1] if len(transit_hubs) >= 2 else (transit_hubs[0] if transit_hubs else None)

        for day_idx in range(num_days):
            am_did, pm_did = self._pick_districts_for_day(
                day_idx, num_days, hotel_did, used_dids, pois_by_district,
            )

            fixed: list[dict] = []
            required: list[dict] = []
            optional: list[dict] = []

            # Day 1 fixed: arrival + hotel_checkin
            if day_idx == 0:
                if arrival:
                    h = arrival
                    fixed.append(self._make_event("arrival", h, "transit", 1,
                                                   time_range=("10:00", "10:00"), is_fixed=True,
                                                   event_id="day1_arrival"))
                    all_pois.append(h)
                fixed.append(self._make_event("hotel_checkin", hotel, "hotel", 1,
                                               earliest="14:00", latest="14:00", is_fixed=True,
                                               event_id="day1_hotel_checkin",
                                               depends_on=["day1_arrival"]))

            # Last day fixed: checkout + departure
            if day_idx == num_days - 1:
                fixed.append(self._make_event("checkout", hotel, "hotel", num_days,
                                               earliest="10:30", latest="10:30", is_fixed=True,
                                               event_id=f"day{num_days}_checkout"))
                if departure and scenario.get("departure_ends_task", True):
                    h = departure
                    fixed.append(self._make_event("departure", h, "transit", num_days,
                                                   time_range=("14:00", "14:00"), is_fixed=True,
                                                   event_id=f"day{num_days}_departure",
                                                   depends_on=[f"day{num_days}_checkout"]))
                    if h not in all_pois:
                        all_pois.append(h)

            # Build slots for this day
            for slot_def in SLOT_DEFS:
                half = slot_def["half"]
                target_did = am_did if half == "morning" else pm_did
                cat = self._slot_category(slot_def, scenario)

                candidates = self._pick_candidates(cat, target_did, pois_by_district, count=3)
                for c in candidates:
                    if c not in all_pois:
                        all_pois.append(c)

                slot_entry = {
                    "slot_id": f"day{day_idx + 1}_{slot_def['event_type']}",
                    "event_type": slot_def["event_type"],
                    "category": cat,
                    "time_range": slot_def["time_range"],
                    "half": half,
                    "district_name": self._district_name(target_did),
                    "candidates": candidates,
                    "requirement": "optional",
                    "depends_on": [],
                }

                if self._is_required_slot(slot_def, day_idx, scenario):
                    slot_entry["requirement"] = "required"
                    required.append(slot_entry)
                else:
                    optional.append(slot_entry)

            menus.append({
                "day": day_idx + 1,
                "fixed_events": fixed,
                "required_slots": required,
                "optional_slots": optional,
            })

            # Route coverage is deliberately scoped to the temporal slots
            # selected for this scenario, not every pair of all six daily
            # template slots.  The solver reads this same marker, keeping the
            # private feasibility graph and the factual route precheck aligned.
            self._mark_route_selected_slots(menus[-1])

        return menus, all_pois

    @staticmethod
    def _mark_route_selected_slots(menu: dict[str, Any]) -> None:
        """Mark the optional slots that participate in this task's route graph.

        Activities are the primary planning choices.  One meal alternates by
        day to keep the resulting itinerary realistic without forcing all
        template slots into the route graph.  Slots after a fixed departure or
        overlapping a fixed event are not selected.  They remain available in
        the task catalogue, but do not create speculative route requests.
        """
        fixed_intervals: list[tuple[int, int]] = []
        departure_start: int | None = None
        for event in menu.get("fixed_events", []):
            start, _ = _item_window(event)
            fixed_intervals.append((start, start + int(event.get("duration_min", 30))))
            if event.get("event_type") == "departure":
                departure_start = start

        day = int(menu.get("day", 1))
        for slot in menu.get("optional_slots", []):
            slot_start, _ = _item_window(slot)
            shortest_duration = min(
                (int(candidate.get("duration_typical_min", 60))
                 for candidate in slot.get("candidates", [])),
                default=60,
            )
            overlaps_fixed = any(
                not (slot_start + shortest_duration <= fixed_start or slot_start >= fixed_end)
                for fixed_start, fixed_end in fixed_intervals
            )
            event_type = slot.get("event_type", "")
            slot["route_selected"] = bool(
                not overlaps_fixed
                and (departure_start is None or slot_start < departure_start)
                and (
                    event_type in {"morning_activity", "afternoon_activity"}
                    or (event_type == "lunch" and day % 2 == 0)
                    or (event_type == "dinner" and day % 2 == 1)
                )
            )

    def _slot_category(self, slot_def: dict[str, Any], scenario: dict[str, Any]) -> str:
        """Use the scenario obligation in one activity slot, retaining slot names."""
        obligation = scenario.get("obligation_category", "attraction")
        if slot_def["event_type"] == "morning_activity" and obligation != "attraction":
            return obligation
        if slot_def["event_type"] == "afternoon_activity":
            available = set(scenario.get("available_categories", []))
            if "outdoor" in available and self.rng.random() < 0.35:
                return "outdoor"
        return slot_def["category"]

    @staticmethod
    def _is_required_slot(slot_def: dict[str, Any], day_idx: int, scenario: dict[str, Any]) -> bool:
        """Only explicit obligations are required; meals remain optional by default."""
        return (
            day_idx == 0
            and slot_def["event_type"] == "morning_activity"
            and scenario.get("obligation_category", "attraction") != "attraction"
        )

    def _pick_candidates(self, category: str, district_id: str,
                          pois_by_district: dict[str, dict[str, list[dict]]],
                          count: int) -> list[dict]:
        """Pick up to `count` candidates from a district, fallback to other districts."""
        did_pois = pois_by_district.get(district_id, {})
        primary = list(did_pois.get(category, []))
        self.rng.shuffle(primary)

        if len(primary) >= count:
            return primary[:count]

        fallback = []
        for did, cats in pois_by_district.items():
            if did == district_id:
                continue
            fallback.extend(cats.get(category, []))
        self.rng.shuffle(fallback)
        return primary + fallback[: count - len(primary)]

    # ── commute precheck (menu-aware) ──

    def precheck_temporal_graph_commutes(self, menus: list[dict], maps_client: Any = None) -> tuple[dict, list]:
        """Materialize routes only between selected adjacent time periods.

        Each selected slot retains up to three factual candidates and every
        adjacent candidate pair retains DRIVE/WALK/TRANSIT observations.  This
        is the intended ``3 x 3 x 3`` coverage, rather than the quadratic
        cross-product over every time-compatible template slot.  Explicit
        fixed dependencies are included even when a selected optional period
        lies between them; they are factual hard constraints, not speculative
        shortcuts.  Overnight residence is represented by the hotel and does
        not create an inter-day commute.
        """
        route_data: dict[str, dict] = {}
        query_log: list[dict] = []
        for menu in menus:
            items = self._selected_timeline_items(menu)
            for source, target in zip(items, items[1:]):
                self._materialize_pair_routes(
                    self._item_pois(source), self._item_pois(target),
                    route_data, query_log, maps_client,
                )

            by_event_id = {
                item.get("event_id"): item
                for item in menu.get("fixed_events", [])
                if item.get("event_id")
            }
            for target in menu.get("fixed_events", []) + menu.get("required_slots", []):
                for predecessor_id in target.get("depends_on", []):
                    source = by_event_id.get(predecessor_id)
                    if source is not None:
                        self._materialize_pair_routes(
                            self._item_pois(source), self._item_pois(target),
                            route_data, query_log, maps_client,
                        )
        return route_data, query_log

    @staticmethod
    def _selected_timeline_items(menu: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the ordered slots actually participating in route coverage."""
        items = list(menu.get("fixed_events", []))
        items.extend(menu.get("required_slots", []))
        items.extend(slot for slot in menu.get("optional_slots", []) if slot.get("route_selected"))
        return sorted(items, key=lambda item: (_item_window(item)[0], item.get("slot_id", item.get("event_id", ""))))

    def materialize_infeasible_transition(self, menus: list[dict], route_data: dict[str, dict],
                                          scenario: dict[str, Any]) -> bool:
        """Create one factual minimal unsatisfiable transition when requested.

        The constrained event is made required and its time window is derived
        from a cached route.  If no factual route is available, the task stays
        feasible rather than being labelled infeasible by assertion.
        """
        if scenario.get("feasibility_target") != "infeasible":
            return False
        for menu in menus:
            anchors = [e for e in menu.get("fixed_events", []) if e.get("event_type") in {"hotel_checkin", "checkout"}]
            for anchor in anchors:
                anchor_start, _ = _item_window(anchor)
                anchor_end = anchor_start + int(anchor.get("duration_min", 30))
                for slot in list(menu.get("optional_slots", [])):
                    slot_start, _ = _item_window(slot)
                    if slot_start < anchor_end or not slot.get("candidates"):
                        continue
                    candidate = slot["candidates"][0]
                    durations = route_data.get(_route_key(anchor.get("place_id", ""), candidate.get("place_id", "")), {}).values()
                    route_min = min((int(value) for value in durations if isinstance(value, (int, float))), default=0)
                    available_travel = slot_start - anchor_end
                    if route_min + 5 <= available_travel:
                        continue
                    duration = int(candidate.get("duration_typical_min", 60))
                    slot["time_range"] = (_format_minutes(slot_start), _format_minutes(slot_start + duration))
                    slot["requirement"] = "required"
                    slot["forced_candidate_place_id"] = candidate.get("place_id", "")
                    menu["optional_slots"].remove(slot)
                    menu.setdefault("required_slots", []).append(slot)
                    scenario["infeasibility_certificate_seed"] = {
                        "from_event_id": anchor.get("event_id", ""),
                        "from_place_id": anchor.get("place_id", ""),
                        "to_slot_id": slot.get("slot_id", ""),
                        "to_place_id": candidate.get("place_id", ""),
                        "available_travel_min": available_travel,
                        "required_travel_min": route_min + 5,
                    }
                    return True
        scenario["feasibility_target"] = "feasible"
        return False

    @staticmethod
    def _item_pois(item: dict[str, Any]) -> list[dict]:
        if "candidates" in item:
            return item["candidates"]
        if item.get("place_id"):
            return [{"place_id": item["place_id"], "name": item.get("place_name", "?"),
                     "location": item.get("location", {})}]
        return []

    @staticmethod
    def _can_temporally_follow(source: dict[str, Any], target: dict[str, Any]) -> bool:
        source_start, source_end = _item_window(source)
        target_start, target_end = _item_window(target)
        if source.get("event_type") == "departure" or target.get("event_type") == "arrival":
            return False
        return source_start < target_end and source_end <= target_end

    def _materialize_pair_routes(self, origins: list[dict], destinations: list[dict],
                                 route_data: dict, query_log: list, maps_client: Any) -> None:
        for origin in origins:
            for destination in destinations:
                origin_id = origin.get("place_id", "")
                destination_id = destination.get("place_id", "")
                if not origin_id or not destination_id or origin_id == destination_id:
                    continue
                key = _route_key(origin_id, destination_id)
                if key in route_data:
                    continue
                route_data[key] = {}
                origin_for_cache = {"place_id": origin_id, "name": origin.get("name", "?"),
                                    "location": origin.get("location", {})}
                destination_for_cache = {"place_id": destination_id, "name": destination.get("name", "?"),
                                         "location": destination.get("location", {})}
                distance_m = self.route_sampler.distance(origin_for_cache, destination_for_cache)
                for mode in self.route_sampler._modes_for_distance(distance_m):
                    if maps_client:
                        before = dict(getattr(self.route_cache, "stats", {}))
                        entry = self.route_cache.get_or_compute(
                            origin_for_cache, destination_for_cache, mode, maps_client,
                        )
                        after = dict(getattr(self.route_cache, "stats", {}))
                        api_requests = int(after.get("api_requests", 0)) - int(before.get("api_requests", 0))
                        coordinate_reuse = int(after.get("coordinate_reuse_materialized", 0)) - int(
                            before.get("coordinate_reuse_materialized", 0)
                        )
                        cache_hits = int(after.get("cache_hits", 0)) - int(before.get("cache_hits", 0))
                        source = (
                            "api" if api_requests > 0 else
                            "coordinate_reuse" if coordinate_reuse > 0 else
                            "cache" if cache_hits > 0 else "unknown"
                        )
                    else:
                        entry = self.route_cache.get(origin_for_cache, destination_for_cache, mode) or {}
                        source = "cache" if entry else "cache_miss"
                        api_requests = coordinate_reuse = cache_hits = 0
                    route = entry.get("route", {})
                    duration = route.get("duration_s", 0)
                    if duration and route.get("available") is not False:
                        route_data[key][mode] = math.ceil(duration / 60)
                    query_log.append({
                        "from_place_id": origin_id, "to_place_id": destination_id,
                        "from": origin.get("name", "?"), "to": destination.get("name", "?"),
                        "mode": mode, "duration_s": duration, "source": source,
                        "api_requests": api_requests,
                        "coordinate_reuse": coordinate_reuse,
                        "cache_hits": cache_hits,
                        "route_id": entry.get("route_id", ""),
                    })

    def precheck_menu_commutes(self, menus: list[dict],
                                maps_client: Any = None) -> tuple[dict, list]:
        """Compute commutes only between consecutive slots within each day.

        For each day, flatten fixed_events + required_slots + optional_slots
        in chronological order. Then for each consecutive pair (A, B):
        compute every POI in A × every POI in B × 3 modes.
        Also compute cross-day: last event of day N → first event of day N+1.
        """
        route_data: dict[str, dict] = {}
        query_log: list[dict] = []

        def _item_pois(item: dict) -> list[dict]:
            """Return list of {place_id, name, location} for an item."""
            if "candidates" in item:
                return item["candidates"]
            if item.get("place_id"):
                return [{"place_id": item["place_id"],
                         "name": item.get("place_name", "?"),
                         "location": item.get("location", {})}]
            return []

        def _item_time_key(item: dict) -> str:
            """Sort key: earliest time window."""
            if item.get("time_range"):
                return item["time_range"][0]
            if item.get("earliest"):
                return item["earliest"]
            return "06:00"

        # collect all ordered item sequences per day
        day_sequences: list[list[dict]] = []
        for m in menus:
            items: list[dict] = list(m.get("fixed_events", []))
            for s in m.get("required_slots", []):
                items.append(s)
            for s in m.get("optional_slots", []):
                items.append(s)
            items.sort(key=_item_time_key)
            day_sequences.append(items)

        # compute within-day adjacent pairs
        for day_idx, items in enumerate(day_sequences):
            for i in range(len(items) - 1):
                srcs = _item_pois(items[i])
                dsts = _item_pois(items[i + 1])
                self._pair_commutes(srcs, dsts, route_data, query_log, maps_client)

            # cross-day: last of this day → first of next day
            if day_idx < len(day_sequences) - 1:
                next_items = day_sequences[day_idx + 1]
                if items and next_items:
                    srcs = _item_pois(items[-1])
                    dsts = _item_pois(next_items[0])
                    self._pair_commutes(srcs, dsts, route_data, query_log, maps_client)

        return route_data, query_log

    def _pair_commutes(self, srcs: list[dict], dsts: list[dict],
                       route_data: dict, query_log: list,
                       maps_client: Any) -> None:
        seen: set[tuple[str, str]] = set()
        for origin in srcs:
            for dest in dsts:
                oid = origin.get("place_id", "")
                did = dest.get("place_id", "")
                if not oid or not did or oid == did:
                    continue
                pair = (oid, did)
                if pair in seen:
                    continue
                seen.add(pair)
                seen.add((did, oid))

                oname = origin.get("name", "?")[:15]
                dname = dest.get("name", "?")[:15]
                key = f"{oname}→{dname}"
                if key in route_data:
                    continue
                route_data[key] = {}

                for mode in ("DRIVE", "WALK", "TRANSIT"):
                    op = {"place_id": oid, "name": oname, "location": origin.get("location", {})}
                    dp = {"place_id": did, "name": dname, "location": dest.get("location", {})}

                    if maps_client:
                        entry = self.route_cache.get_or_compute(op, dp, mode, maps_client)
                    else:
                        ck = self.route_cache._make_key(op, dp, mode)
                        entry = self.route_cache._cache.get(ck, {})

                    rt = entry.get("route", {})
                    dist = rt.get("distance_m", 0)
                    dur = rt.get("duration_s", 0)
                    avail = rt.get("available")
                    err = rt.get("api_error", False)

                    if dist > 0 and avail is not False:
                        route_data[key][mode] = dur // 60
                        query_log.append({
                            "from": oname, "to": dname, "mode": mode,
                            "distance_m": dist, "duration_s": dur,
                            "source": "cache" if not maps_client else "api_or_cache",
                            "route_id": entry.get("route_id", ""),
                        })
                    elif err:
                        query_log.append({
                            "from": oname, "to": dname, "mode": mode,
                            "source": "api_error", "error": "API failed",
                        })

    # ── helpers ──

    def _make_event(self, event_type: str, poi: dict, category: str, day: int,
                     time_range: tuple[str, str] | None = None,
                     earliest: str | None = None, latest: str | None = None,
                     is_fixed: bool = False, event_id: str = "",
                     depends_on: list[str] | None = None) -> dict:
        evt: dict[str, Any] = {
            "event_type": event_type,
            "place_id": poi["place_id"],
            "place_name": poi["name"],
            "location": poi.get("location", {}),
            "category": category,
            "day": day,
            "is_fixed": is_fixed,
            "duration_min": DURATION_MIN.get(category, 60),
            "event_id": event_id or f"day{day}_{event_type}",
            "depends_on": list(depends_on or []),
        }
        if is_fixed:
            if time_range:
                evt["earliest"] = time_range[0]
                evt["latest"] = time_range[1]
        if earliest:
            evt["earliest"] = earliest
        if latest:
            evt["latest"] = latest
        if time_range and not is_fixed:
            evt["earliest"] = time_range[0]
            evt["latest"] = time_range[1]
        return evt

    def _price_level(self, svc: dict, place: dict[str, Any] | None = None) -> str:
        state = svc.get("state_model", {})
        band = state.get("price_band", "")
        mapping = {"low": "INEXPENSIVE", "medium": "MODERATE",
                   "medium_high": "EXPENSIVE", "high": "VERY_EXPENSIVE"}
        if band in mapping:
            return mapping[band]
        raw = str(_google_details(place or {}).get("price_level") or "")
        return raw.removeprefix("PRICE_LEVEL_")

    def _district_name(self, did: str) -> str:
        for d in self.districts:
            if d.get("district_id") == did:
                return d.get("name", did)
        return did


def _item_window(item: dict[str, Any]) -> tuple[int, int]:
    if item.get("time_range"):
        start, end = item["time_range"]
    else:
        start = item.get("earliest", "00:00")
        end = item.get("latest", start)
    return _minutes(start), _minutes(end)


def _service_types(service: dict[str, Any]) -> list[str]:
    """Read multi-capability services while retaining old single-value worlds."""
    values = service.get("service_types")
    if not isinstance(values, list):
        values = []
    ordered = [str(value) for value in values if str(value)]
    legacy = str(service.get("service_type") or "")
    if legacy and legacy not in ordered:
        ordered.insert(0, legacy)
    return ordered


def _required_service_types(primary_service_type: str, selected_service_type: str) -> list[str]:
    """Expose only the primary capability and a selected secondary capability."""
    primary = str(primary_service_type or "")
    selected = str(selected_service_type or "")
    if not primary:
        return [selected] if selected else []
    if not selected or selected == primary:
        return [primary]
    return [primary, selected]


def _google_details(place: dict[str, Any]) -> dict[str, Any]:
    projection = place.get("map_projection") or {}
    details = projection.get("google_place_details") or {}
    return details if isinstance(details, dict) else {}


def _money_units(value: dict[str, Any] | None) -> float | None:
    if not isinstance(value, dict):
        return None
    try:
        return float(value.get("units") or 0) + float(value.get("nanos") or 0) / 1_000_000_000
    except (TypeError, ValueError):
        return None


def _price_range(details: dict[str, Any]) -> dict[str, Any]:
    raw = details.get("price_range") or {}
    lower = _money_units(raw.get("startPrice"))
    upper = _money_units(raw.get("endPrice"))
    if lower is None and upper is None:
        return {}
    currency = str((raw.get("startPrice") or raw.get("endPrice") or {}).get("currencyCode") or "")
    return {"currency": currency, "minimum": lower, "maximum": upper}


def _service_features(details: dict[str, Any]) -> list[str]:
    factual_flags = (
        "reservable", "dine_in", "takeout", "delivery", "curbside_pickup",
        "outdoor_seating", "live_music", "serves_breakfast", "serves_lunch",
        "serves_dinner", "serves_brunch", "serves_vegetarian_food",
        "wheelchair_accessible_entrance", "wheelchair_accessible_seating",
    )
    return [key for key in factual_flags if details.get(key) is True]


def _weekly_opening_intervals(details: dict[str, Any]) -> dict[str, list[list[int]]]:
    from shared.opening_hours import effective_hours_from_details
    hours = effective_hours_from_details(details) or {}
    result: dict[str, list[list[int]]] = {}
    for period in hours.get("periods") or []:
        opened = period.get("open") or {}
        closed = period.get("close") or {}
        try:
            open_day = int(opened["day"])
            close_day = int(closed.get("day", open_day))
            start = int(opened.get("hour", 0)) * 60 + int(opened.get("minute", 0))
            close_clock = int(closed.get("hour", 0)) * 60 + int(closed.get("minute", 0))
        except (KeyError, TypeError, ValueError):
            continue
        day_delta = (close_day - open_day) % 7
        end = close_clock + day_delta * 24 * 60
        if end <= start:
            end += 24 * 60
        result.setdefault(str(open_day), []).append([start, end])
    for intervals in result.values():
        intervals.sort()
    return result


def _minutes(value: str) -> int:
    hour, minute = value.split(":")[:2]
    return int(hour) * 60 + int(minute)


def _route_key(origin_place_id: str, destination_place_id: str) -> str:
    return f"{origin_place_id}__TO__{destination_place_id}"


def _format_minutes(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"
