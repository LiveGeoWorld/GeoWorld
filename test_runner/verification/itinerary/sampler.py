from __future__ import annotations
import random
from typing import Any
from test_runner.verification.route_planning.sampler import RouteSampler
CATEGORY_LABELS: dict[str, str] = {'restaurant': 'restaurant', 'cafe': 'cafe', 'bar': 'cafe', 'shopping': 'personal_errand', 'lodging': 'hotel', 'tourist_attraction': 'attraction', 'museum': 'attraction', 'movie_theater': 'attraction', 'park': 'outdoor', 'transit_station': 'transit', 'hospital': 'appointment', 'medical_care': 'appointment', 'pharmacy': 'personal_errand', 'hotel': 'hotel', 'attraction': 'attraction', 'retail': 'personal_errand', 'healthcare': 'appointment', 'restaurant_reservation': 'restaurant', 'hotel_booking': 'hotel', 'attraction_visit': 'attraction', 'local_visit': 'cafe', 'outdoor_visit': 'outdoor', 'retail_visit': 'personal_errand', 'healthcare_visit': 'appointment', 'business_meeting': 'business_meeting', 'office_visit': 'business_meeting', 'coworking_visit': 'business_meeting'}
WEEKDAY_NAMES = ('Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday')

class ItinerarySampler:
    """Samples districts + candidate POIs and builds daily option menus."""

    def __init__(self, world: dict[str, Any], characters: dict[str, Any], route_cache: Any, rng: random.Random | None=None, routing_place_ids: set[str] | None=None):
        self.world = world
        self.characters = characters
        self.route_cache = route_cache
        self.rng = rng or random.Random()
        self.route_sampler = RouteSampler(world, characters, rng=self.rng)
        self.routing_place_ids = {str(place_id) for place_id in routing_place_ids or set() if place_id}
        self.places = world.get('places', [])
        self.services = world.get('services', [])
        self.districts = world.get('districts', [])
        self.place_dict: dict[str, dict] = {p['place_id']: p for p in self.places if p.get('place_id')}
        self.place_district: dict[str, str] = {}
        self.district_places: dict[str, list[dict]] = {}
        for p in self.places:
            did = p.get('district_id', '')
            pid = p.get('place_id', '')
            if did and pid:
                self.place_district[pid] = did
                self.district_places.setdefault(did, []).append(p)
        self.district_coords: dict[str, tuple[float, float]] = {}
        for d in self.districts:
            loc = d.get('center', d.get('location', {}))
            lat = loc.get('lat', 0)
            lng = loc.get('lng', 0)
            if lat and lng:
                self.district_coords[d['district_id']] = (lat, lng)

    def activity_candidates(self, *, routing_only: bool) -> list[dict[str, Any]]:
        """Project factual service capabilities into task candidate records."""
        result: list[dict[str, Any]] = []
        for service in self.services:
            place_id = str(service.get('place_id') or '')
            place = self.place_dict.get(place_id)
            if not place or (routing_only and place_id not in self.routing_place_ids):
                continue
            for service_type in _service_types(service):
                category = CATEGORY_LABELS.get(service_type)
                if not category or service_type in {'lodging', 'hotel', 'hotel_booking'}:
                    continue
                details = _google_details(place)
                result.append({'place_id': place_id, 'name': place.get('name', '?'), 'service_type': service_type, 'primary_service_type': str(service.get('primary_service_type') or service.get('service_type') or ''), 'service_types': _service_types(service), 'required_service_types': _required_service_types(str(service.get('primary_service_type') or service.get('service_type') or ''), service_type), 'primary_type': str(place.get('primary_type') or ''), 'location': dict(place.get('location') or {}), 'address': str(place.get('address') or ''), 'district_id': place.get('district_id', ''), 'district_name': self._district_name(place.get('district_id', '')), 'price_level': self._price_level(service, place), 'price_range': _price_range(details), 'service_features': _service_features(details), 'weekly_opening_intervals': _weekly_opening_intervals(details), 'rating': round(place.get('map_projection', {}).get('rating', 0) or 0, 1)})
        return result

    def _price_level(self, svc: dict, place: dict[str, Any] | None=None) -> str:
        state = svc.get('state_model', {})
        band = state.get('price_band', '')
        mapping = {'low': 'INEXPENSIVE', 'medium': 'MODERATE', 'medium_high': 'EXPENSIVE', 'high': 'VERY_EXPENSIVE'}
        if band in mapping:
            return mapping[band]
        raw = str(_google_details(place or {}).get('price_level') or '')
        return raw.removeprefix('PRICE_LEVEL_')

    def _district_name(self, did: str) -> str:
        for d in self.districts:
            if d.get('district_id') == did:
                return d.get('name', did)
        return did

def _service_types(service: dict[str, Any]) -> list[str]:
    """Read multi-capability services while retaining old single-value worlds."""
    values = service.get('service_types')
    if not isinstance(values, list):
        values = []
    ordered = [str(value) for value in values if str(value)]
    legacy = str(service.get('service_type') or '')
    if legacy and legacy not in ordered:
        ordered.insert(0, legacy)
    return ordered

def _required_service_types(primary_service_type: str, selected_service_type: str) -> list[str]:
    """Expose only the primary capability and a selected secondary capability."""
    primary = str(primary_service_type or '')
    selected = str(selected_service_type or '')
    if not primary:
        return [selected] if selected else []
    if not selected or selected == primary:
        return [primary]
    return [primary, selected]

def _google_details(place: dict[str, Any]) -> dict[str, Any]:
    projection = place.get('map_projection') or {}
    details = projection.get('google_place_details') or {}
    return details if isinstance(details, dict) else {}

def _money_units(value: dict[str, Any] | None) -> float | None:
    if not isinstance(value, dict):
        return None
    try:
        return float(value.get('units') or 0) + float(value.get('nanos') or 0) / 1000000000
    except (TypeError, ValueError):
        return None

def _price_range(details: dict[str, Any]) -> dict[str, Any]:
    raw = details.get('price_range') or {}
    lower = _money_units(raw.get('startPrice'))
    upper = _money_units(raw.get('endPrice'))
    if lower is None and upper is None:
        return {}
    currency = str((raw.get('startPrice') or raw.get('endPrice') or {}).get('currencyCode') or '')
    return {'currency': currency, 'minimum': lower, 'maximum': upper}

def _service_features(details: dict[str, Any]) -> list[str]:
    factual_flags = ('reservable', 'dine_in', 'takeout', 'delivery', 'curbside_pickup', 'outdoor_seating', 'live_music', 'serves_breakfast', 'serves_lunch', 'serves_dinner', 'serves_brunch', 'serves_vegetarian_food', 'wheelchair_accessible_entrance', 'wheelchair_accessible_seating')
    return [key for key in factual_flags if details.get(key) is True]

def _weekly_opening_intervals(details: dict[str, Any]) -> dict[str, list[list[int]]]:
    from shared.opening_hours import effective_hours_from_details
    hours = effective_hours_from_details(details) or {}
    result: dict[str, list[list[int]]] = {}
    for period in hours.get('periods') or []:
        opened = period.get('open') or {}
        closed = period.get('close') or {}
        try:
            open_day = int(opened['day'])
            close_day = int(closed.get('day', open_day))
            start = int(opened.get('hour', 0)) * 60 + int(opened.get('minute', 0))
            close_clock = int(closed.get('hour', 0)) * 60 + int(closed.get('minute', 0))
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
