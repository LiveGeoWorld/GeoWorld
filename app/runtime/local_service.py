"""LocalServiceAppRuntime — executable local merchant/service information app.

Discovery tasks use this app to search candidate services, inspect merchant
details, compare trade-offs, and check availability. It is intentionally
read-only for now: discovery evaluates information gathering and recommendation,
not booking-side state mutation.
"""
from __future__ import annotations
from typing import Any
from app.runtime.base import OperationType, RuntimeApp, app_tool
from app.runtime.geo import distance_m, norm_query, service_payload
from shared.place_type_catalog import category_for_google_types, category_matches
from shared.opening_hours import effective_hours_from_details

class LocalServiceAppRuntime(RuntimeApp):
    """Deterministic service lookup backed by Bench world services + places."""
    name = 'LocalServiceApp'

    def __init__(self, *, services: list[dict[str, Any]], places: list[dict[str, Any]], cities: list[dict[str, Any]] | None=None) -> None:
        self.services = services or []
        self.places = places or []
        self.city_names = {city.get('city_id', ''): city.get('name', '') for city in cities or [] if city.get('city_id')}
        self._places_by_id = {place.get('place_id', ''): place for place in self.places if place.get('place_id')}
        self._services_by_id = {service.get('service_id', ''): service for service in self.services if service.get('service_id')}
        self._services_by_place_id = {service.get('place_id', ''): service for service in self.services if service.get('place_id')}
        super().__init__()

    @app_tool(OperationType.READ, summary='搜索本地服务/商户候选')
    def search_services(self, args: dict[str, Any]) -> dict[str, Any]:
        city_id = str(args.get('city_id', '') or '')
        city_hint = norm_query(args.get('city_hint', ''))
        district_id = str(args.get('district_id', '') or '')
        service_type = str(args.get('service_type', '') or '')
        keyword = norm_query(args.get('keyword', ''))
        filters = args.get('filters') if isinstance(args.get('filters'), dict) else {}
        limit = int(args.get('limit', 10) or 10)
        anchor = self._resolve_anchor(args.get('near') or args.get('start_place_id') or '')
        anchor_loc = anchor.get('location', {}) if anchor else {}
        max_distance_m = int(filters.get('distance_m') or args.get('radius_m') or 0)
        scored: list[tuple[int, int, dict[str, Any], dict[str, Any] | None]] = []
        for service in self.services:
            place = self._place_by_id(service.get('place_id', ''))
            if not self._passes_basic_filters(service, place, city_id, city_hint, district_id, service_type):
                continue
            if keyword and keyword not in self._service_search_text(service, place):
                continue
            distance = self._distance_to_anchor(anchor_loc, place)
            if max_distance_m and distance is not None and (distance > max_distance_m):
                continue
            if not self._passes_structured_filters(service, place, filters):
                continue
            score = self._service_score(service, place, filters)
            if distance is not None:
                score += max(0, 5 - distance // 300)
            scored.append((score, distance if distance is not None else 10 ** 9, service, place))
        scored.sort(key=lambda item: (-item[0], item[1]))
        results = []
        for (score, distance, service, place) in scored[:limit]:
            item = self._service_summary(service, place)
            item['match'] = {'score': score, 'explanation': self._match_explanation(service, place, filters, distance)}
            if distance != 10 ** 9:
                item['distance_to_anchor_m'] = distance
            results.append(item)
        return {'results': results}

    @app_tool(OperationType.READ, summary='查看商户/服务详情')
    def get_service_detail(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self._resolve_service(args)
        if not service:
            return {'status': 'not_found', 'detail': {}}
        place = self._place_by_id(service.get('place_id', ''))
        return {'status': 'ok', 'detail': self._service_detail(service, place)}

    @app_tool(OperationType.READ, summary='比较多个商户候选')
    def compare_services(self, args: dict[str, Any]) -> dict[str, Any]:
        references = args.get('services') or args.get('service_names') or args.get('names') or []
        if isinstance(references, (str, dict)):
            references = [references]
        constraints = args.get('constraints') or args.get('criteria') or []
        if isinstance(constraints, str):
            constraints = [constraints]
        compared = []
        for reference in references:
            identity = reference if isinstance(reference, dict) else {'name': reference}
            service = self._resolve_service(identity)
            if not service:
                continue
            place = self._place_by_id(service.get('place_id', ''))
            detail = self._service_detail(service, place)
            compared.append({'name': detail.get('name', ''), 'location': detail.get('location', {}), 'evidence': self._constraint_evidence(detail, constraints), 'detail': detail})
        return {'comparisons': compared, 'constraints': constraints}

    @app_tool(OperationType.READ, summary='查看服务可用性')
    def check_availability(self, args: dict[str, Any]) -> dict[str, Any]:
        service = self._resolve_service(args)
        if not service:
            return {'status': 'not_found', 'slots': []}
        state = service.get('state_model', {})
        inventory = state.get('reservation_inventory') if isinstance(state.get('reservation_inventory'), dict) else {}
        slots = []
        for slot in state.get('reservation_slots', []) or []:
            slots.append({'time': slot, 'remaining_capacity': inventory.get(slot), 'available': inventory.get(slot, 1) != 0})
        return {'status': 'ok', 'name': self._service_summary(service, self._place_by_id(service.get('place_id', ''))).get('name', ''), 'open_status': state.get('open_status', ''), 'slots': slots}

    def _passes_basic_filters(self, service: dict[str, Any], place: dict[str, Any] | None, city_id: str, city_hint: str, district_id: str, service_type: str) -> bool:
        place_city_id = place.get('city_id', '') if place else ''
        if service_type and (not self._service_type_matches(service, place, service_type)):
            return False
        if city_id and place_city_id != city_id:
            return False
        if city_hint and city_hint not in norm_query(f"{place_city_id} {self.city_names.get(place_city_id, '')}"):
            return False
        if district_id and place and (place.get('district_id') != district_id):
            return False
        return True

    def _passes_structured_filters(self, service: dict[str, Any], place: dict[str, Any] | None, filters: dict[str, Any]) -> bool:
        state = service.get('state_model', {})
        projection = (place or {}).get('map_projection', {})
        rating_min = filters.get('rating_min')
        if rating_min is not None and projection.get('rating') is not None:
            if float(projection.get('rating', 0)) < float(rating_min):
                return False
        price_band = filters.get('price_band') or filters.get('budget')
        if price_band and state.get('price_band') and (state.get('price_band') != price_band):
            return False
        max_queue = filters.get('max_queue_time_min') or filters.get('queue_time_max')
        if max_queue is not None and state.get('queue_time_min') is not None:
            if int(state.get('queue_time_min') or 0) > int(max_queue):
                return False
        if filters.get('open_now') is True and state.get('open_status') not in {'open', 'unknown', ''}:
            return False
        return True

    def _service_score(self, service: dict[str, Any], place: dict[str, Any] | None, filters: dict[str, Any]) -> int:
        state = service.get('state_model', {})
        projection = (place or {}).get('map_projection', {})
        score = 0
        rating = projection.get('rating')
        if rating is not None:
            score += int(float(rating) * 2)
        queue = state.get('queue_time_min')
        if queue is not None:
            score += max(0, 5 - int(queue) // 10)
        if state.get('open_status') == 'open':
            score += 2
        if state.get('price_band') in {'low', 'medium'}:
            score += 1
        constraints = ' '.join((str(item) for item in filters.get('constraints', []) or []))
        haystack = self._service_search_text(service, place)
        for term in [part for part in norm_query(constraints).split() if part]:
            if term in haystack:
                score += 1
        return score

    def _service_summary(self, service: dict[str, Any], place: dict[str, Any] | None) -> dict[str, Any]:
        payload = service_payload(service, place)
        payload['city'] = self.city_names.get((place or {}).get('city_id', ''), '')
        return payload

    def _service_detail(self, service: dict[str, Any], place: dict[str, Any] | None) -> dict[str, Any]:
        state = service.get('state_model', {})
        projection = (place or {}).get('map_projection', {})
        google_details = projection.get('google_place_details', {}) if isinstance(projection.get('google_place_details'), dict) else {}
        payload = self._service_summary(service, place)
        payload.update({'location': (place or {}).get('location', {}), 'city': self.city_names.get((place or {}).get('city_id', ''), ''), 'place_type': (place or {}).get('primary_type', ''), 'types': projection.get('types', []), 'business_status': projection.get('business_status') or state.get('base_status', ''), 'open_status': state.get('open_status', ''), 'opening_hours': effective_hours_from_details(google_details) or google_details.get('current_opening_hours') or {}, 'user_rating_count': projection.get('user_rating_count'), 'noise_level': state.get('noise_level', ''), 'indoor_available': state.get('indoor_available'), 'weather_sensitive': state.get('weather_sensitive'), 'reservation_slots': state.get('reservation_slots', []), 'affordances': service.get('affordances', []), 'constraints': service.get('constraints', []), 'review_summary': google_details.get('editorial_summary') or google_details.get('review_summary') or ''})
        return payload

    def _constraint_evidence(self, detail: dict[str, Any], constraints: list[Any]) -> list[dict[str, Any]]:
        text = norm_query(' '.join((str(value) for value in detail.values() if value is not None)))
        evidence = []
        for constraint in constraints:
            raw = str(constraint)
            key = norm_query(raw)
            evidence.append({'constraint': raw, 'matched': bool(key and key in text), 'supporting_fields': self._supporting_fields(detail, key)})
        return evidence

    def _supporting_fields(self, detail: dict[str, Any], key: str) -> list[str]:
        if not key:
            return []
        fields = []
        for field in ['name', 'address', 'place_type', 'price_band', 'crowding_level', 'review_summary', 'open_status']:
            if key in norm_query(detail.get(field, '')):
                fields.append(field)
        return fields

    def _match_explanation(self, service: dict[str, Any], place: dict[str, Any] | None, filters: dict[str, Any], distance_m_value: int) -> str:
        parts = []
        state = service.get('state_model', {})
        if service.get('service_type'):
            parts.append(f"服务类型={service.get('service_type')}")
        if state.get('price_band'):
            parts.append(f"价格={state.get('price_band')}")
        if state.get('queue_time_min') is not None:
            parts.append(f"排队约{state.get('queue_time_min')}分钟")
        if distance_m_value != 10 ** 9:
            parts.append(f'距锚点约{distance_m_value}米')
        if filters.get('constraints'):
            parts.append('已携带用户约束')
        return '；'.join(parts) or '基础服务候选'

    def _service_search_text(self, service: dict[str, Any], place: dict[str, Any] | None) -> str:
        state = service.get('state_model', {})
        projection = (place or {}).get('map_projection', {})
        return norm_query(' '.join((str(part) for part in [service.get('service_id', ''), service.get('service_type', ''), (place or {}).get('name', ''), (place or {}).get('address', ''), (place or {}).get('primary_type', ''), (place or {}).get('district_id', ''), state.get('price_band', ''), state.get('crowding_level', ''), state.get('noise_level', ''), ' '.join(projection.get('types', []) or [])] if part)))

    def _service_type_matches(self, service: dict[str, Any], place: dict[str, Any] | None, requested: str) -> bool:
        actual = str(service.get('service_type', '') or '')
        place_type = str((place or {}).get('primary_type', '') or '')
        projection_types = set((str(item) for item in (place or {}).get('map_projection', {}).get('types', []) or []))
        types = {place_type, *projection_types}
        return category_matches(requested, actual) or category_matches(requested, category_for_google_types(types))

    def _resolve_service(self, args: dict[str, Any]) -> dict[str, Any] | None:
        """Resolve public name/coordinates without exposing storage keys.

        ID branches remain for legacy private callers during migration. The
        formal tool schema accepts only ``name`` and optional ``location``.
        Ambiguous names deliberately return no result unless coordinates
        distinguish the observed service.
        """
        for key in ['service_id', 'candidate_id']:
            raw = str(args.get(key, '') or '')
            if raw and raw in self._services_by_id:
                return self._services_by_id[raw]
        place_id = str(args.get('place_id', '') or '')
        if place_id and place_id in self._services_by_place_id:
            return self._services_by_place_id[place_id]
        name = norm_query(args.get('name', ''))
        if name:
            candidates = []
            for service in self.services:
                place = self._place_by_id(service.get('place_id', ''))
                if name == norm_query((place or {}).get('name', '')):
                    candidates.append((service, place or {}))
            location = args.get('location') if isinstance(args.get('location'), dict) else {}
            if location:
                candidates = [(service, place) for (service, place) in candidates if self._same_location(location, place.get('location', {}))]
            if len(candidates) == 1:
                return candidates[0][0]
        return None

    @staticmethod
    def _same_location(left: dict[str, Any], right: dict[str, Any]) -> bool:
        try:
            return abs(float(left.get('lat')) - float(right.get('lat'))) <= 1e-05 and abs(float(left.get('lng')) - float(right.get('lng'))) <= 1e-05
        except (TypeError, ValueError):
            return False

    def _resolve_anchor(self, raw: Any) -> dict[str, Any] | None:
        value = str(raw or '')
        if not value:
            return None
        if value in self._places_by_id:
            return self._places_by_id[value]
        needle = norm_query(value)
        for place in self.places:
            if needle and needle in norm_query(f"{place.get('name', '')} {place.get('address', '')}"):
                return place
        return None

    def _place_by_id(self, place_id: str) -> dict[str, Any] | None:
        return self._places_by_id.get(place_id)

    def _distance_to_anchor(self, anchor_loc: dict[str, Any], place: dict[str, Any] | None) -> int | None:
        if not anchor_loc or not place or (not place.get('location')):
            return None
        return round(distance_m(anchor_loc, place.get('location', {})))
