from __future__ import annotations
from pathlib import Path
from copy import deepcopy
import re
from typing import Any
import unicodedata
from difflib import SequenceMatcher
from shared.io import read_json
from shared.public_place_facts import public_place_facts
from app.apps.base import BaseApp
'Public MapApp sandbox backed by a frozen city map snapshot and route cache.\n\nMapApp is the authority for POI discovery and immutable map facts. Its public\nidentity is a name plus coordinates; storage IDs remain inside the sandbox.\nDynamic service state belongs to its owning non-map application.\n'

class MapAppSandbox(BaseApp):
    """Expose frozen MapApp facts without exposing world storage identifiers."""
    name = 'MapApp'
    FUZZY_NAME_THRESHOLD = 0.4
    _QUERY_STOP_WORDS = {'a', 'an', 'at', 'for', 'in', 'near', 'of', 'the', 'to'}
    _TYPE_ALIASES = {'cafe': 'cafe', 'cafes': 'cafe', 'coffee': 'cafe', 'restaurant': 'restaurant', 'restaurants': 'restaurant', 'hotel': 'hotel', 'hotels': 'hotel', 'lodging': 'lodging', 'museum': 'museum', 'museums': 'museum', 'park': 'park', 'parks': 'park', 'shop': 'store', 'shops': 'store', 'shopping': 'store', 'store': 'store', 'stores': 'store', 'university': 'university', 'universities': 'university', 'school': 'school', 'schools': 'school', 'hospital': 'hospital', 'hospitals': 'hospital', 'station': 'station', 'stations': 'station', 'airport': 'airport', 'airports': 'airport', 'bar': 'bar', 'bars': 'bar', 'garden': 'garden', 'gardens': 'garden', 'breakfast': 'breakfast_restaurant', 'brunch': 'brunch_restaurant', '早午餐': 'brunch_restaurant', '面馆': 'noodle_restaurant', '咖啡': 'cafe', '咖啡馆': 'cafe', '咖啡店': 'cafe', '餐厅': 'restaurant', '餐馆': 'restaurant', '饭店': 'restaurant', '酒店': 'hotel', '旅馆': 'hotel', '博物馆': 'museum', '公园': 'park', '酒吧': 'bar', '机场': 'airport', '美术馆': 'art_museum', '画廊': 'art_gallery', '早餐': 'breakfast_restaurant', '早餐店': 'breakfast_restaurant'}
    _ROUTE_MODE_ALIASES = {'all': 'ALL', 'any': 'ALL', 'all modes': 'ALL', 'multimodal': 'ALL', 'drive': 'DRIVE', 'driving': 'DRIVE', 'car': 'DRIVE', 'by car': 'DRIVE', 'automobile': 'DRIVE', 'road': 'DRIVE', 'walk': 'WALK', 'walking': 'WALK', 'on foot': 'WALK', 'foot': 'WALK', 'pedestrian': 'WALK', ' пешком': 'WALK', 'transit': 'TRANSIT', 'public transit': 'TRANSIT', 'public transport': 'TRANSIT', 'transport': 'TRANSIT', 'bus': 'TRANSIT', 'train': 'TRANSIT', 'subway': 'TRANSIT'}

    def __init__(self, route_cache_path: str='', world_paths: list[str] | None=None, route_cache_data: dict[str, Any] | None=None, *, route_cache_paths: list[str] | None=None, prefer_inline_routes: bool=False):
        super().__init__()
        self._places: dict[str, dict[str, Any]] = {}
        self._frozen_districts: list[dict[str, Any]] = []
        self._frozen_routes: dict[str, dict[str, Any]] = {}
        self._route_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for path in world_paths or []:
            if path and Path(path).is_file():
                self._load_world(path)
        if route_cache_path and Path(route_cache_path).is_file():
            self._load_cache(route_cache_path)
        for path in route_cache_paths or []:
            self._load_cache(path)
        if isinstance(route_cache_data, dict):
            if prefer_inline_routes:
                self._remove_overridden_routes(route_cache_data)
            self._load_cache_data(route_cache_data)

    def _remove_overridden_routes(self, data: dict[str, Any]) -> None:
        for row in data.get('cache', []):
            pair = (row['origin_place_id'], row['dest_place_id'])
            if pair in self._route_index:
                self._route_index[pair] = [route for route in self._route_index[pair] if route['mode'] != row['mode']]

    def _load_world(self, path: str) -> None:
        data = read_json(path)
        self._frozen_districts.extend(data.get('districts') or [])
        districts = {str(raw.get('district_id') or ''): str(raw.get('name') or '') for raw in data.get('districts', []) if isinstance(raw, dict)}
        cities = {str(raw.get('city_id') or ''): str(raw.get('name') or '') for raw in data.get('cities', []) if isinstance(raw, dict)}
        for raw in data.get('places', []):
            if not isinstance(raw, dict):
                continue
            place_id = str(raw.get('place_id') or '')
            location = raw.get('location')
            if not place_id or not isinstance(location, dict):
                continue
            self._places[place_id] = {'name': str(raw.get('name') or ''), 'location': {'lat': location.get('lat'), 'lng': location.get('lng')}, 'address': str(raw.get('address') or ''), 'primary_type': str(raw.get('primary_type') or ''), 'google_types': list((raw.get('map_projection') or {}).get('types') or []), '_district_name': districts.get(str(raw.get('district_id') or ''), ''), '_city_name': cities.get(str(raw.get('city_id') or ''), ''), '_city_id': str(raw.get('city_id') or ''), '_aliases': self._saved_aliases(raw), '_public_details': public_place_facts(raw, district_name=districts.get(str(raw.get('district_id') or ''), ''), city_name=cities.get(str(raw.get('city_id') or ''), ''))}

    @staticmethod
    def _saved_aliases(raw: dict[str, Any]) -> list[str]:
        """Only use names actually present in public frozen facts."""
        values = []
        for key in ('aliases', 'alternate_names', 'name_en', 'name_zh'):
            value = raw.get(key)
            values.extend(value if isinstance(value, list) else [value])
        details = (raw.get('map_projection') or {}).get('google_place_details') or {}
        display = details.get('displayName')
        values.append(display.get('text') if isinstance(display, dict) else display)
        return sorted({v.strip() for v in values if isinstance(v, str) and v.strip()})

    def _load_cache(self, path: str) -> None:
        self._load_cache_data(read_json(path))

    def _load_cache_data(self, data: dict[str, Any]) -> None:
        entries = [entry for entry in data.get('cache', []) if isinstance(entry, dict)]
        routes_by_id = {str(entry.get('route_id') or ''): entry.get('route') for entry in entries if entry.get('route_id') and isinstance(entry.get('route'), dict)}
        self._frozen_routes.update({str(row['route_id']): {**row, 'route': row.get('route') or routes_by_id.get(str(row.get('route_ref') or '')) or {}} for row in entries if row.get('route_id')})
        for entry in entries:
            origin_id = str(entry.get('origin_place_id') or '')
            dest_id = str(entry.get('dest_place_id') or '')
            if not origin_id or not dest_id:
                continue
            self._add_legacy_place(origin_id, entry.get('origin_name'), entry.get('origin_lat'), entry.get('origin_lng'))
            self._add_legacy_place(dest_id, entry.get('dest_name'), entry.get('dest_lat'), entry.get('dest_lng'))
            route = entry.get('route') or routes_by_id.get(str(entry.get('route_ref') or '')) or {}
            public_route = {'mode': str(entry.get('mode') or ''), 'distance_m': route.get('distance_m', 0), 'duration_s': route.get('duration_s', 0)}
            for field in ('available', 'summary', 'walking_distance_m'):
                if field in route:
                    public_route[field] = route[field]
            if any((field in route for field in ('steps', 'polyline_points', 'encoded_polyline'))):
                public_route['_navigation'] = {'steps': route.get('steps', []), 'polyline_points': route.get('polyline_points', []), 'encoded_polyline': route.get('encoded_polyline', '')}
            self._route_index.setdefault((origin_id, dest_id), []).append(public_route)

    def _add_legacy_place(self, place_id: str, name: Any, lat: Any, lng: Any) -> None:
        """Keep route-cache-only legacy episodes readable without revealing IDs."""
        if place_id not in self._places:
            self._places[place_id] = {'name': str(name or ''), 'location': {'lat': lat, 'lng': lng}, 'address': '', 'primary_type': '', 'google_types': [], '_district_name': '', '_city_name': ''}

    @staticmethod
    def _normalize_text(value: Any) -> str:
        decomposed = unicodedata.normalize('NFKD', str(value or '').casefold())
        without_marks = ''.join((char for char in decomposed if not unicodedata.combining(char)))
        without_marks = re.sub("(?<=\\w)['’](?=\\w)", '', without_marks)
        return ' '.join(re.findall('[^\\W_]+', without_marks, flags=re.UNICODE))

    @classmethod
    def _tokens(cls, value: Any) -> list[str]:
        return cls._normalize_text(value).split()

    @classmethod
    def _type_terms(cls, place: dict[str, Any]) -> set[str]:
        terms: set[str] = set()
        for raw_type in [place.get('primary_type'), *(place.get('google_types') or [])]:
            terms.add('_'.join(cls._tokens(str(raw_type or '').replace('_', ' '))))
            terms.update(cls._tokens(str(raw_type or '').replace('_', ' ')))
        if 'cafe' in terms:
            terms.add('coffee')
        if {'restaurant', 'cafe', 'food'} & terms:
            terms.add('dining')
        if {'store', 'shop', 'shopping'} & terms:
            terms.update({'store', 'shop'})
        return terms

    @classmethod
    def _name_acronym(cls, name: str) -> str:
        tokens = [token for token in cls._tokens(name) if token not in cls._QUERY_STOP_WORDS]
        return ''.join((token[0] for token in tokens if token))

    @classmethod
    def _score_place(cls, place: dict[str, Any], query: str, *, primary_type_match: bool=False) -> tuple[int, dict[str, Any]] | None:
        normalized_query = cls._normalize_text(query)
        query_tokens = cls._tokens(query)
        if not query_tokens:
            return (0, {'matched_name_terms': [], 'matched_location_terms': [], 'matched_type_terms': []})
        name = cls._normalize_text(place.get('name'))
        name_tokens = set(cls._tokens(place.get('name')))
        address_tokens = set(cls._tokens(place.get('address')))
        district_tokens = set(cls._tokens(place.get('_district_name')))
        city_tokens = set(cls._tokens(place.get('_city_name')))
        location_tokens = address_tokens | district_tokens | city_tokens
        type_terms = cls._type_terms(place)
        chinese_terms = {term for term in name_tokens | location_tokens | set(cls._TYPE_ALIASES) if len(term) >= 2 and re.fullmatch('[\\u3400-\\u9fff]+', term)}
        expanded_tokens: list[str] = []
        for token in query_tokens:
            segments: dict[int, list[str]] = {0: []}
            if re.fullmatch('[\\u3400-\\u9fff]+', token) and token not in chinese_terms:
                for start in range(len(token)):
                    if start not in segments:
                        continue
                    for term in sorted(chinese_terms):
                        if token.startswith(term, start):
                            segments.setdefault(start + len(term), segments[start] + [term])
            expanded_tokens.extend(segments.get(len(token), [token]))
        query_tokens = expanded_tokens
        requested_types = {cls._TYPE_ALIASES[token] for token in query_tokens if token in cls._TYPE_ALIASES}
        type_query_tokens = {token for token in query_tokens if token in cls._TYPE_ALIASES}
        content_tokens = [token for token in query_tokens if token not in cls._QUERY_STOP_WORDS and token not in type_query_tokens]

        def chinese_fragment(token, text):
            return len(token) >= 2 and re.fullmatch('[\\u3400-\\u9fff]+', token) and (token in text)
        matched_name = [token for token in content_tokens if token in name_tokens or chinese_fragment(token, name)]
        location_texts = [cls._normalize_text(place.get(key)) for key in ('address', '_district_name', '_city_name')]
        matched_location = [token for token in content_tokens if token in location_tokens or any((chinese_fragment(token, text) for text in location_texts))]
        matched_types = sorted(requested_types & type_terms)
        acronym_match = len(content_tokens) == 1 and 2 <= len(content_tokens[0]) <= 8 and (content_tokens[0] == cls._name_acronym(str(place.get('name') or '')))
        if requested_types and (not matched_types) and (normalized_query != name):
            return None
        if normalized_query == name:
            score = 1000
        elif name and f' {name} ' in f' {normalized_query} ':
            score = 900 + 45 * len(matched_location) + 80 * len(matched_types)
        elif normalized_query and (f' {normalized_query} ' in f' {name} ' or chinese_fragment(normalized_query, name)):
            score = 850
        else:
            covered = set(matched_name) | set(matched_location)
            all_content_covered = bool(content_tokens) and all((token in covered for token in content_tokens))
            has_identity_signal = bool(matched_name or acronym_match)
            has_location_type_signal = bool(matched_location and (matched_types or primary_type_match or (not requested_types)))
            has_type_only_signal = bool(matched_types and (not content_tokens))
            if not (has_identity_signal or has_location_type_signal or has_type_only_signal):
                return None
            if content_tokens and (not all_content_covered) and (not has_identity_signal):
                return None
            score = 0
            if acronym_match:
                score += 700
            score += 120 * len(matched_name)
            score += 45 * len(matched_location)
            score += 80 * len(matched_types)
            if all_content_covered:
                score += 100
        return (score, {'matched_name_terms': matched_name, 'matched_location_terms': matched_location, 'matched_type_terms': matched_types})
    _CITY_NAME_ALIASES = {'shanghai': ('上海',), 'london': ('伦敦',), 'cape town': ('开普敦',), 'new york': ('纽约',), 'singapore': ('新加坡',), 'tokyo': ('东京',)}

    @staticmethod
    def _phrase_pattern(term: str) -> str:
        if re.fullmatch('[\\u3400-\\u9fff]+', term):
            return re.escape(term)
        return '(?<!\\w)' + re.escape(term) + '(?!\\w)'

    def _query_scope(self, query: str):
        """Recognize explicit cities from the frozen public directory only."""
        text = self._normalize_text(query)
        names = {self._normalize_text(p.get('_city_name')) for p in self._places.values()}
        names.discard('')
        cities = set()
        for name in sorted(names, key=len, reverse=True):
            for alias in (name, *self._CITY_NAME_ALIASES.get(name, ())):
                pattern = self._phrase_pattern(alias)
                if re.search(pattern, text):
                    cities.add(name)
                    text = re.sub(pattern, ' ', text)
        return (cities, ' '.join(text.split()))

    @classmethod
    def _search_score(cls, place: dict[str, Any], query: str, typed: bool):
        query_text = cls._normalize_text(query)
        names = [place.get('name', ''), *place.get('_aliases', [])]
        for (i, name) in enumerate(names):
            if query_text and query_text == cls._normalize_text(name):
                return (4000 if i == 0 else 3000, {} if i == 0 else {'match_kind': 'saved_alias', 'matched_alias': name})
        tokens = query_text.split()
        requested = {cls._TYPE_ALIASES[t] for t in tokens if t in cls._TYPE_ALIASES}
        if requested and (not requested.intersection(cls._type_terms(place))):
            return None
        best = cls._score_place(place, query, primary_type_match=typed)
        if best is not None:
            best = (2500 if best[0] >= 850 else 1000 + min(best[0], 999) if requested or not query_text else 50, best[1])
        for alias in place.get('_aliases', []):
            matched = cls._score_place({**place, 'name': alias}, query, primary_type_match=typed)
            if matched is not None:
                score = 2400 if matched[0] >= 850 else 1000 + min(matched[0], 999)
                if best is None or score > best[0]:
                    best = (score, {'match_kind': 'saved_alias', 'matched_alias': alias})
        location = set(cls._tokens(place.get('address'))) | set(cls._tokens(place.get('_city_name'))) | set(cls._tokens(place.get('_district_name')))
        core = ' '.join((t for t in tokens if t not in location and t not in cls._QUERY_STOP_WORDS and (t not in cls._TYPE_ALIASES)))
        for name in names:
            normalized = cls._normalize_text(name)
            if min(len(core), len(normalized)) < 4:
                continue
            similarity = SequenceMatcher(None, core, normalized, autojunk=False).ratio()
            score = 100 + int(similarity * 50)
            if similarity >= cls.FUZZY_NAME_THRESHOLD and (best is None or score > best[0]):
                best = (score, {'match_kind': 'fuzzy_name', 'matched_name': name, 'similarity': round(similarity, 3)})
        return best

    @staticmethod
    def _same_location(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
        try:
            return abs(float((left or {}).get('lat')) - float((right or {}).get('lat'))) <= 1e-05 and abs(float((left or {}).get('lng')) - float((right or {}).get('lng'))) <= 1e-05
        except (TypeError, ValueError):
            return False

    def _resolve_identity(self, name: str, location: dict[str, Any] | None) -> tuple[str, dict[str, Any]] | None:
        normalized = ' '.join(str(name or '').casefold().split())
        if not normalized or not location:
            return None
        matches = [(place_id, place) for (place_id, place) in self._places.items() if ' '.join(place['name'].casefold().split()) == normalized and self._same_location(location, place.get('location'))]
        return matches[0] if len(matches) == 1 else None

    @classmethod
    def _normalize_route_mode(cls, mode: Any) -> str | None:
        """Normalize natural-language route modes to frozen cache enums."""
        raw = cls._normalize_text(mode).replace('-', ' ').strip()
        if not raw:
            return 'ALL'
        return cls._ROUTE_MODE_ALIASES.get(raw, raw.upper() if raw.upper() in {'ALL', 'DRIVE', 'WALK', 'TRANSIT'} else None)

    @staticmethod
    def _public_place(place: dict[str, Any]) -> dict[str, Any]:
        return {'name': place['name'], 'location': dict(place['location']), 'address': place['address'], 'primary_type': place['primary_type'], 'google_types': list(place['google_types'])}

    @staticmethod
    def _normalize_primary_type(value: str) -> str:
        return '_'.join(str(value or '').casefold().replace('_', ' ').split())

    def _supported_primary_types(self) -> list[str]:
        return sorted({self._normalize_primary_type(place.get('primary_type', '')) for place in self._places.values() if str(place.get('primary_type') or '').strip()})

    def search_places(self, query: str='', limit: int=10, primary_type: str='', offset: int | None=None) -> dict[str, Any]:
        """Search the complete frozen map directory, not a task candidate set."""
        try:
            max_results = max(1, min(int(limit), 50))
        except (TypeError, ValueError):
            max_results = 10
        requested_type = self._normalize_primary_type(primary_type)
        supported_types = self._supported_primary_types()
        if requested_type and requested_type not in supported_types:
            return {'count': 0, 'results': [], 'status': 'unsupported_primary_type', 'feedback': {'message': 'This primary type is not supported by the current frozen map. Choose a supported_primary_types value, or omit primary_type and search by query.', 'requested_primary_type': requested_type, 'supported_primary_types': supported_types}}
        (cities, scoped_query) = self._query_scope(query)
        ranked: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        for place in self._places.values():
            if cities and self._normalize_text(place.get('_city_name')) not in cities:
                continue
            if requested_type and requested_type != self._normalize_primary_type(place.get('primary_type', '')):
                continue
            scored = self._search_score(place, scoped_query, bool(requested_type))
            full = self._normalize_text(query)
            if full and full == self._normalize_text(place.get('name')):
                scored = (4000, {})
            elif full and any((full == self._normalize_text(a) for a in place.get('_aliases', []))):
                scored = (3000, {'match_kind': 'saved_alias', 'matched_alias': query})
            if scored is None:
                continue
            (score, evidence) = scored
            public = self._public_place(place)
            if evidence.get('match_kind'):
                public['search_match'] = evidence
            ranked.append((score, public, evidence))
        ranked.sort(key=lambda item: (-item[0], item[1]['name'].casefold(), item[1]['location']['lat'], item[1]['location']['lng']))
        start = max(0, int(offset or 0))
        results = [item[1] for item in ranked[start:start + max_results]]
        response: dict[str, Any] = {'count': len(results), 'results': results}
        if offset is not None:
            response.update(total=len(ranked), offset=start)
            if ranked and start >= len(ranked):
                response.update(status='page_exhausted', feedback={'message': 'No more results at this offset. Use an offset below total or change the query.'})
        if not ranked:
            query_tokens = self._tokens(query)
            response['status'] = 'no_matches'
            response['feedback'] = {'message': 'No grounded place matched this query. Try a shorter place name or separate the area and place type.', 'recognized_type_terms': sorted({token for token in query_tokens if token in self._TYPE_ALIASES}), 'query_terms': query_tokens, 'suggested_queries': [value for value in [' '.join((token for token in query_tokens if token not in self._TYPE_ALIASES)), ' '.join((token for token in query_tokens if token in self._TYPE_ALIASES))] if value and value != self._normalize_text(query)]}
            if requested_type:
                response['feedback'].update({'message': 'No place matched both query and the requested primary_type. Try a shorter query or omit primary_type to broaden the search.', 'requested_primary_type': requested_type})
        return response

    def get_place_detail(self, name: str, location: dict[str, Any]) -> dict[str, Any]:
        resolved = self._resolve_identity(name, location)
        if not resolved:
            return {'found': False}
        return {'found': True, **self._public_place(resolved[1]), **deepcopy(resolved[1].get('_public_details') or {})}

    def query_routes(self, origin: dict[str, Any], destination: dict[str, Any], mode: str='ALL', detail_level: str='summary', include_navigation: bool | None=None) -> dict[str, Any]:
        if include_navigation is not None:
            detail_level = 'full' if include_navigation else 'summary'
        detail_level = self._normalize_text(detail_level).replace('-', '_') or 'summary'
        if detail_level not in {'summary', 'steps', 'geometry', 'full'}:
            return {'count': 0, 'routes': [], 'status': 'invalid_detail_level', 'feedback': {'message': 'detail_level must be summary, steps, geometry, or full.'}}
        canonical_mode = self._normalize_route_mode(mode)
        if canonical_mode is None:
            return {'count': 0, 'routes': [], 'status': 'invalid_mode', 'feedback': {'message': 'Use a route mode such as ALL, DRIVE/driving, WALK/walking, or TRANSIT/public transit.'}}
        origin_resolved = self._resolve_identity(origin.get('name', ''), origin.get('location'))
        destination_resolved = self._resolve_identity(destination.get('name', ''), destination.get('location'))
        if not origin_resolved or not destination_resolved:
            return {'count': 0, 'routes': [], 'status': 'place_not_resolved', 'feedback': {'message': 'Copy the exact public name and coordinates from search_places for each unresolved endpoint.', 'unresolved_endpoints': [key for (key, resolved) in (('origin', origin_resolved), ('destination', destination_resolved)) if not resolved]}}
        routes = self._route_index.get((origin_resolved[0], destination_resolved[0]), [])
        if not routes:
            return {'count': 0, 'routes': [], 'status': 'route_not_in_snapshot', 'feedback': {'message': 'The frozen map has no recorded route for this ordered pair. No transport mode is recorded for this ordered pair; changing mode or repeating this pair will not produce a route. This is not proof of real-world unreachability. Try another destination or connection.'}}
        available_modes = sorted({route.get('mode', '').upper() for route in routes})
        if canonical_mode != 'ALL':
            routes = [route for route in routes if route.get('mode', '').upper() == canonical_mode]
        if not routes:
            return {'count': 0, 'routes': [], 'status': 'mode_not_in_snapshot', 'feedback': {'message': 'This pair has recorded routes, but none for the requested mode. Use ALL to inspect them if the task permits.', 'recorded_modes': available_modes}}
        public_routes = []
        for route in routes:
            result = {key: value for (key, value) in route.items() if key != '_navigation'}
            navigation = route.get('_navigation') or {}
            if detail_level in {'steps', 'full'}:
                result['steps'] = navigation.get('steps', [])
            if detail_level in {'geometry', 'full'}:
                result['polyline_points'] = navigation.get('polyline_points', [])
                result['encoded_polyline'] = navigation.get('encoded_polyline', '')
            public_routes.append(result)
        return {'count': len(public_routes), 'routes': public_routes}

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        primary_type_description = "Optional strict filter on the place's primary_type only, not its secondary google_types. Case, spaces and underscores are normalized: coffee_shop, Coffee Shop and coffee shop refer to the same type. Omit or leave empty for no type filter. Unsupported values return status=unsupported_primary_type with supported_primary_types; a supported type with no matching query returns status=no_matches. Supported primary types in the current frozen map: " + (', '.join(self._supported_primary_types()) or 'none') + '.'
        identity = {'name': {'type': 'string', 'description': 'Public place name'}, 'location': {'type': 'object', 'description': 'Coordinates returned by MapApp'}}
        return [{'name': 'search_places', 'description': 'Search real places by name, area/address, and place type, including common Chinese type words. Multiple type keywords match broadly (any type), not all required services. Exact names rank before approximate names; saved aliases and fuzzy name candidates include search_match explaining the match. Copy the returned canonical name and coordinates for subsequent calls. Recognized explicit city names restrict results to that city. Category words filter by any matching category; fuzzy names cannot bypass city or category filters. Explicit primary_type remains a strict filter. No matches return recovery feedback; page_exhausted means the offset is beyond the results.', 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {'query': {'type': 'string'}, 'limit': {'type': 'integer'}, 'primary_type': {'type': 'string', 'description': primary_type_description}, 'offset': {'type': 'integer', 'minimum': 0, 'description': 'Optional pagination offset; returns total and offset when supplied.'}}, 'required': ['query']}}, {'name': 'get_place_detail', 'description': 'Read frozen map facts for an identified place: district/city, rating, review count, price_level, price_range (currency/minimum/maximum), regular opening_hours and boolean service_attributes. Null means not recorded, not false or free. Price level is a grade, not a monetary amount; the saved range is not a reservation quote. Live operating state belongs to LocalServiceApp.', 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': identity, 'required': ['name', 'location']}}, {'name': 'query_routes', 'description': 'Query frozen routes between two MapApp identities. Route mode accepts ALL/any, DRIVE/drive/driving/car, WALK/walk/walking/on foot, and TRANSIT/transit/public transit/bus/train in any case. detail_level is optional and defaults to summary. Choose summary when you only need mode, distance_m, duration_s, available, summary, and walking_distance_m. Choose steps when you need those summary fields plus the steps array; each step contains distance_m, duration_s, startLocation, endLocation, travelMode, instruction, and its encoded step polyline. Choose geometry when you need the summary fields plus polyline_points and encoded_polyline for drawing or geometric inspection. Choose full only when you need both steps and geometry; it returns all of the above and can be large. The tool does not provide departure or arrival schedules.', 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {'origin': {'type': 'object', 'properties': identity, 'required': ['name', 'location']}, 'destination': {'type': 'object', 'properties': identity, 'required': ['name', 'location']}, 'mode': {'type': 'string', 'description': 'Route mode; natural-language aliases and any case are accepted.'}, 'detail_level': {'type': 'string', 'enum': ['summary', 'steps', 'geometry', 'full'], 'description': 'Optional response detail, default summary. summary=mode/distance_m/duration_s/available/summary/walking_distance_m; steps=summary plus turn-by-turn steps; geometry=summary plus polyline_points and encoded_polyline; full=steps plus geometry. Choose the least detail needed.'}, 'include_navigation': {'type': 'boolean', 'description': 'Optional. Include detailed navigation steps and geometry when true.'}}, 'required': ['origin', 'destination']}}]
