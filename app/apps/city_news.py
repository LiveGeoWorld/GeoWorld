from __future__ import annotations
import re
import math
from copy import deepcopy
from typing import Any
from pathlib import Path
from shared.io import read_json
from app.apps.base import BaseApp
'CityNewsApp sandbox — provides city-level info: events, news, area notices.\nAgent actively queries this app to understand traffic, weather impacts, and regional changes.\nEvents are loaded from env_events.json per task and triggered by step.\n'

class CityNewsApp(BaseApp):
    name = 'CityNewsApp'
    'City information app — events, news, area notices.\n    Data comes from task_sandbox.json. Agent queries with district_id + info_type + time_range.\n    '

    def __init__(self, source_dir: str=''):
        super().__init__()
        self.source_dir = source_dir
        self._entries: list[dict] = []
        self._district_ids_by_name: dict[str, set[str]] = {}
        self._district_name_by_id: dict[str, str] = {}
        self._city_names: set[str] = set()
        self._place_names: set[str] = set()
        self._frozen_places: list[dict] = []
        self._frozen_districts: list[dict] = []
        self._poi_district_by_entry: dict[int, str] = {}
        self._route_districts_by_entry: dict[int, set[str]] = {}
        self._district_scope_by_entry: dict[int, dict] = {}
        self._frozen_routes: dict[str, dict] = {}
        self._current_state_by_scope: dict[str, dict[str, Any]] = {}
        self._event_history: list[dict] = []
        self._dynamic_now = None
        self._safety_view: list[dict] | None = None
        self._safety_district_by_index: dict[int, str] = {}
        self._safety_district_names: set[str] = set()
        self._safety_district_aliases: dict[str, set[str]] = {}
        self._safety_city_names: set[str] = set()
        if source_dir:
            from app.apps.safety_event_view import load_safety_event_view
            view = load_safety_event_view(Path(source_dir))
            self._safety_view = view['CityNewsApp'] if view is not None else None
            snapshot_path = Path(source_dir) / 'static_app_state.json'
            if snapshot_path.exists():
                snapshot = read_json(snapshot_path)
                apps = snapshot.get('apps', {}) if isinstance(snapshot, dict) else {}
                state = apps.get('CityNewsApp', {}) if isinstance(apps, dict) else {}
                self._entries = list(state.get('entries', [])) if isinstance(state, dict) else []
            world_slice_path = Path(source_dir) / 'world_slice.json'
            if world_slice_path.exists():
                world_slice = read_json(world_slice_path)
                self._frozen_places = list(world_slice.get('places') or [])
                self._frozen_districts = list(world_slice.get('districts') or [])
                self._city_names.update((str(row['name']).strip().casefold() for row in world_slice.get('cities', []) if row.get('name')))
                self._place_names.update((str(row['name']).strip().casefold() for row in world_slice.get('places', []) if row.get('name')))
                for district in world_slice.get('districts', []) if isinstance(world_slice, dict) else []:
                    self._register_district(district, name_field='name')
            sandbox_path = Path(source_dir) / 'task_sandbox.json'
            if sandbox_path.exists():
                sandbox = read_json(sandbox_path)
                if isinstance(sandbox, dict):
                    self._frozen_routes = {str(row['route_id']): row for row in (sandbox.get('dynamic_route_graph') or {}).get('route_records', []) if row.get('route_id')}
                    if self._safety_view is not None:
                        self._index_safety_districts(sandbox)
                    self._register_district(sandbox.get('risky_target', {}))
                    for collection in ('alternatives', 'distractors'):
                        for place in sandbox.get(collection, []) or []:
                            self._register_district(place)

    def inject_entry(self, entry: dict) -> None:
        """Append a later CityNews state without mutating historical news."""
        self._event_history.append(dict(entry))
        info_type = entry.get('info_type', 'news')
        title = entry.get('title', '')
        category = entry.get('category', '')
        description = entry.get('public_message') or entry.get('description', '')
        private_scope = entry.get('scope', {}) if isinstance(entry.get('scope'), dict) else {}
        public_scope = entry.get('public_scope', {}) if isinstance(entry.get('public_scope'), dict) else {}
        projected = {'end_time': entry.get('end_time', ''), 'type': entry.get('event_type', ''), 'info_type': info_type, 'title': title, 'category': category, 'description': description, 'severity': entry.get('severity', 'medium'), 'timestamp': entry.get('timestep', ''), 'state_field': entry.get('state_field', ''), 'risk_status': entry.get('risk_status', ''), 'scope': self.public_notice_scope(entry)}
        self._entries.append(projected)
        if private_scope.get('type') == 'district' and private_scope.get('id'):
            self._district_scope_by_entry[id(projected)] = dict(private_scope)
        if private_scope.get('type') in {'place', 'poi', 'poi_circle'} or public_scope.get('type') in {'place', 'poi', 'poi_circle'}:
            district_id = self._bound_poi_district(private_scope, public_scope)
            if district_id:
                self._poi_district_by_entry[id(projected)] = district_id
        if private_scope.get('type') == 'route_operation':
            self._route_districts_by_entry[id(projected)] = self._bound_route_districts(entry)
        if projected['state_field']:
            self._current_state_by_scope[self._state_key(projected['state_field'], self._district_scope_by_entry.get(id(projected), projected['scope']))] = projected

    def trigger_step(self, step: int) -> None:
        pass

    def public_notice_scope(self, entry: dict) -> dict:
        """Expose road impact geometry, never the reference person's journey."""
        private = entry.get('scope') or {}
        public = entry.get('public_scope') or {}
        if private.get('type') != 'route_operation':
            return deepcopy(public or {'type': private.get('type', '')})
        from app.apps.city_news_route_retrieval import route_notice_geometry
        routes = dict(self._frozen_routes)
        if self.env is not None:
            routes.update(getattr(self.env.apps.get('MapApp'), '_frozen_routes', {}))
        result = {'type': 'route_segment', 'geometry': route_notice_geometry(entry, routes)}
        mode = public.get('mode') or (routes.get(str(private.get('id'))) or {}).get('mode')
        if mode:
            result['mode'] = mode
        return result

    def _bound_route_districts(self, entry: dict) -> set[str]:
        from app.apps.city_news_route_retrieval import route_districts
        routes = dict(self._frozen_routes)
        places = list(self._frozen_places)
        districts = list(self._frozen_districts)
        if self.env is not None:
            map_app = self.env.apps.get('MapApp')
            routes.update(getattr(map_app, '_frozen_routes', {}))
            places.extend((dict(row, place_id=key) for (key, row) in getattr(map_app, '_places', {}).items()))
            districts.extend(getattr(map_app, '_frozen_districts', []))
        for district in districts:
            self._register_district(district, name_field='name')
        return route_districts(entry, routes, places, districts)

    def _bound_poi_district(self, private_scope: dict, public_scope: dict) -> str:
        places = list(self._frozen_places)
        district_rows = list(self._frozen_districts)
        if self.env is not None:
            map_app = self.env.apps.get('MapApp')
            places.extend((dict(row, place_id=key) for (key, row) in getattr(map_app, '_places', {}).items()))
            district_rows.extend(getattr(map_app, '_frozen_districts', []))
        for district in district_rows:
            self._register_district(district, name_field='name')
        place_id = private_scope.get('id') if private_scope.get('type') in {'place', 'poi'} else None
        if place_id:
            matches = [row for row in places if row.get('place_id') == place_id]
        else:
            name = str(public_scope.get('name') or '').strip().casefold()
            matches = [row for row in places if name and str(row.get('name') or '').strip().casefold() == name]
            location = public_scope.get('location') or public_scope.get('center')
            if isinstance(location, dict):
                matches = [row for row in matches if row.get('location') == location]
            identities = {str(row.get('place_id') or '') for row in matches}
            if len(identities) != 1 or (not place_id and len(matches) > 1 and (identities == {''})):
                return ''
        districts = set()
        for row in matches:
            if row.get('district_id'):
                districts.add(str(row['district_id']))
            elif row.get('_district_name'):
                districts.update(self._resolve_district(row['_district_name']))
        if districts:
            return next(iter(districts)) if len(districts) == 1 else ''
        locations = {(row['location']['lat'], row['location']['lng']) for row in matches if self._valid_coordinates(row.get('location'))}
        city_ids = {str(row.get('city_id') or row.get('_city_id')) for row in matches if row.get('city_id') or row.get('_city_id')}
        if len(locations) != 1 or len(city_ids) != 1:
            return ''
        (lat, lng) = next(iter(locations))
        city_id = next(iter(city_ids))
        candidates = {}
        for district in district_rows:
            center = (district.get('geometry') or {}).get('data')
            if district.get('city_id') != city_id or not district.get('district_id') or (district.get('geometry') or {}).get('kind') != 'point' or (not self._valid_coordinates(center)):
                continue
            (a, b) = (math.radians(lat), math.radians(center['lat']))
            hav = math.sin((b - a) / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(math.radians(center['lng'] - lng) / 2) ** 2
            candidates[str(district['district_id'])] = 12742000 * math.asin(min(1, math.sqrt(hav)))
        return min(candidates, key=candidates.get) if candidates else ''

    @staticmethod
    def _valid_coordinates(location: Any) -> bool:
        return isinstance(location, dict) and all((type(location.get(k)) in (int, float) and math.isfinite(location[k]) for k in ('lat', 'lng'))) and (-90 <= location['lat'] <= 90) and (-180 <= location['lng'] <= 180)

    def refresh_dynamic_state(self, current) -> None:
        from shared.event_time import event_active_at
        self._dynamic_now = current
        self._current_state_by_scope = {}
        for projected in self._entries:
            field = projected.get('state_field')
            if field and (not projected.get('end_time') or event_active_at(projected, current)):
                self._current_state_by_scope[self._state_key(field, self._district_scope_by_entry.get(id(projected), projected.get('scope')))] = projected

    def get_info(self, district: str='', info_type: str='', time_range: str='', district_id: str='') -> Any:
        """Query city information. Filters from _entries list."""
        if self._safety_view is not None:
            import copy
            from app.apps.safety_event_view import matches_time_range
            requested_types = self._resolve_info_types(info_type)
            if info_type and (not requested_types):
                return {'status': 'invalid_info_type', 'info_type': info_type, 'allowed': ['notice', 'traffic', 'event', 'news'], 'items': []}
            return [copy.deepcopy(row) for (index, row) in enumerate(self._safety_view) if self._matches_safety_scope(row, index, district or district_id) and (not requested_types or row.get('info_type') in requested_types) and matches_time_range(row, time_range)]
        temporal_query = None
        if self._dynamic_now is not None:
            from app.apps.city_news_time_query import query_window, FutureQuery
            try:
                temporal_query = query_window(time_range, self._dynamic_now)
            except FutureQuery:
                return {'status': 'future_time_query_not_allowed', 'items': []}
            except (ValueError, TypeError, AttributeError):
                return {'status': 'invalid_time_range', 'items': []}
        public_district = str(district or district_id or '').strip()
        query_kind = 'district'
        resolved_district_id = ''
        if public_district:
            cities = set(self._city_names)
            places = set(self._place_names)
            if self.env is not None:
                map_app = self.env.apps.get('MapApp')
                for place in getattr(map_app, '_places', {}).values():
                    places.add(str(place.get('name') or '').strip().casefold())
                    cities.add(str(place.get('_city_name') or '').strip().casefold())
            for entry in self._entries:
                scope = entry.get('scope') or {}
                if scope.get('city_name'):
                    cities.add(str(scope['city_name']).strip().casefold())
                if scope.get('type') in {'place', 'poi'} and scope.get('name'):
                    places.add(str(scope['name']).strip().casefold())
            normalized = public_district.casefold()
            if normalized in cities:
                query_kind = 'city'
            elif normalized in places:
                query_kind = 'place'
            district_matches = self._resolve_district(public_district)
            if query_kind == 'district' and len(district_matches) != 1:
                return {'status': 'district_ambiguous' if district_matches else 'district_not_found', 'district': public_district, 'candidates': [self._district_name_by_id[item] for item in sorted(district_matches)], 'items': []}
            if query_kind == 'district':
                resolved_district_id = next(iter(district_matches))
        requested_types = self._resolve_info_types(info_type)
        if info_type and (not requested_types):
            return {'status': 'invalid_info_type', 'info_type': info_type, 'allowed': ['notice', 'traffic', 'event', 'news'], 'items': []}
        results = []
        for e in self._entries:
            if temporal_query is not None:
                from app.apps.city_news_time_query import effective_window, matches_effective_window
                overrides = []
                if e.get('risk_variant'):
                    original_scope = e.get('scope') or {}
                    if original_scope.get('type') == 'district' and original_scope.get('id'):
                        for later in self._entries:
                            binding = self._district_scope_by_entry.get(id(later), later.get('scope') or {})
                            if later.get('state_field') == 'city_news.risk_status' and binding.get('type') == 'district' and (binding.get('id') == original_scope['id']):
                                overrides.append(effective_window(later))
                if not matches_effective_window(e, temporal_query, self._dynamic_now, overrides):
                    continue
            if requested_types and e.get('info_type') not in requested_types:
                continue
            scope = e.get('scope', {}) if isinstance(e.get('scope'), dict) else {}
            poi_district = self._poi_district_by_entry.get(id(e))
            if resolved_district_id and poi_district and (poi_district != resolved_district_id):
                continue
            route_districts = self._route_districts_by_entry.get(id(e))
            if resolved_district_id and route_districts is not None and (resolved_district_id not in route_districts):
                continue
            if public_district and query_kind == 'place':
                if str(scope.get('name') or '').strip().casefold() != public_district.casefold():
                    continue
            if public_district and query_kind == 'city':
                city_name = str(scope.get('city_name') or '').strip().casefold()
                if city_name and city_name != public_district.casefold():
                    continue
            binding_scope = self._district_scope_by_entry.get(id(e), scope)
            if resolved_district_id and binding_scope.get('type') == 'district' and (binding_scope.get('id') != resolved_district_id):
                continue
            if temporal_query is None and e.get('risk_variant') and self._has_later_risk_state(scope):
                continue
            results.append(self._public_entry(e))
        return results

    def _public_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        projected = dict(entry)
        scope = entry.get('scope', {}) if isinstance(entry.get('scope'), dict) else {}
        if scope.get('type') == 'district':
            binding_scope = self._district_scope_by_entry.get(id(entry), scope)
            projected['scope'] = {key: value for (key, value) in scope.items() if key != 'id'}
            projected['scope']['name'] = scope.get('name') or self._district_name_by_id.get(str(binding_scope.get('id') or ''), '')
        return projected

    def _index_safety_districts(self, sandbox: dict[str, Any]) -> None:
        world = (sandbox.get('safety_choice') or {}).get('world') or {}
        districts = {str(row.get('district_id')): str(row.get('name')) for row in world.get('districts', []) if isinstance(row, dict) and row.get('district_id') and row.get('name')}
        self._safety_district_names = {self._normalize_district(name) for name in districts.values()}
        for name in districts.values():
            canonical = self._normalize_district(name)
            aliases = {name, f'The {name}'}
            initials = re.fullmatch('(\\w+)\\s*&\\s*(\\w+)(.*)', name)
            if initials:
                aliases.add(f'{initials[1][0]}&{initials[2][0]}{initials[3]}')
            for alias in aliases:
                self._safety_district_aliases.setdefault(self._normalize_district(alias), set()).add(canonical)
        self._safety_city_names = {self._normalize_district(str(row['name'])) for row in world.get('cities', []) if isinstance(row, dict) and row.get('name')}
        if 'new york' in self._safety_city_names:
            self._safety_city_names.add('new york city')
        places_by_name: dict[str, list[dict[str, Any]]] = {}
        for place in world.get('places', []):
            if isinstance(place, dict) and place.get('name'):
                places_by_name.setdefault(str(place['name']).casefold(), []).append(place)
        for (index, notice) in enumerate(self._safety_view or []):
            scope = notice.get('scope') or {}
            (name, center) = (scope.get('name'), scope.get('center'))
            if not name or not isinstance(center, dict):
                continue
            candidates = [place for place in places_by_name.get(str(name).casefold(), []) if isinstance(place.get('location'), dict) and all((isinstance(center.get(key), (int, float)) and isinstance(place['location'].get(key), (int, float)) and (abs(center[key] - place['location'][key]) < 1e-06) for key in ('lat', 'lng')))]
            if len(candidates) != 1:
                continue
            district_name = districts.get(str(candidates[0].get('district_id')))
            if district_name:
                self._safety_district_by_index[index] = self._normalize_district(district_name)

    def _matches_safety_scope(self, notice: dict[str, Any], index: int, query: str) -> bool:
        from app.apps.safety_event_view import matches_public_scope
        if matches_public_scope(notice, query):
            return True
        districts = self._resolve_safety_district_names(query)
        if not districts:
            return False
        scope = notice.get('scope') or {}
        citywide = bool(scope.get('city_name') and (not scope.get('name')) and (not scope.get('center')))
        return citywide or self._safety_district_by_index.get(index) in districts

    def _resolve_safety_district_name(self, query: str) -> str | None:
        normalized = self._normalize_district(query)
        matches = set(self._safety_district_aliases.get(normalized, set()))
        for city in self._safety_city_names:
            if normalized.endswith(f' {city}'):
                matches.update(self._safety_district_aliases.get(normalized[:-(len(city) + 1)], set()))
            if normalized.startswith(f'{city} '):
                matches.update(self._safety_district_aliases.get(normalized[len(city) + 1:], set()))
        return next(iter(matches)) if len(matches) == 1 else None

    def _resolve_safety_district_names(self, query: str) -> set[str] | None:
        single = self._resolve_safety_district_name(query)
        if single:
            return {single}
        parts = re.split('\\s+and\\s+', query.strip(), flags=re.IGNORECASE)
        resolved = [self._resolve_safety_district_name(part) for part in parts]
        return set(resolved) if all(resolved) else None

    @staticmethod
    def _normalize_district(value: str) -> str:
        normalized = re.sub('[^\\w]+', ' ', value.casefold().replace('&', ' and '))
        return ' '.join((token for token in normalized.split() if token != 'and'))

    def _resolve_district(self, value: str) -> set[str]:
        normalized = self._normalize_district(value)
        exact = self._district_ids_by_name.get(normalized, set())
        if exact:
            return set(exact)
        matches: set[str] = set()
        padded = f' {normalized} '
        for (alias, district_ids) in self._district_ids_by_name.items():
            if f' {alias} ' in padded or padded.strip() in alias:
                matches.update(district_ids)
        return matches

    @staticmethod
    def _resolve_info_types(value: str) -> set[str]:
        if not value.strip():
            return set()
        tokens = set(re.findall('[a-z]+|[\\u4e00-\\u9fff]+', value.casefold()))
        aliases = {'notice': {'notice', 'notices', 'alert', 'alerts', 'advisory', 'advisories', '公告', '通知', '警报', '安全'}, 'traffic': {'traffic', 'transport', 'transit', '交通', '出行'}, 'event': {'event', 'events', 'activity', 'activities', '事件', '活动'}, 'news': {'news', 'update', 'updates', '新闻', '资讯'}}
        return {kind for (kind, words) in aliases.items() if tokens & words}

    def _register_district(self, district: Any, *, name_field: str='district_name') -> None:
        if not isinstance(district, dict):
            return
        district_id = str(district.get('district_id') or '')
        name = str(district.get(name_field) or '').strip()
        if district_id and name:
            aliases = {self._normalize_district(name)}
            words = aliases.copy().pop().split()
            if len(words) >= 2:
                aliases.add(' '.join([*(word[0] for word in words[:-1]), words[-1]]))
            for alias in aliases:
                self._district_ids_by_name.setdefault(alias, set()).add(district_id)
            self._district_name_by_id[district_id] = name

    def _has_later_risk_state(self, scope: dict[str, Any]) -> bool:
        if scope.get('type') != 'district' or not scope.get('id'):
            return False
        return any((field == 'city_news.risk_status' and key.endswith(f":district:{scope['id']}") for (key, value) in self._current_state_by_scope.items() for field in [str(value.get('state_field') or '')]))

    @staticmethod
    def _state_key(state_field: Any, scope: Any) -> str:
        state = str(state_field or '')
        if isinstance(scope, dict) and scope.get('type') == 'district' and scope.get('id'):
            return f"{state}:district:{scope['id']}"
        return f'{state}:global'

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [{'name': 'get_info', 'description': "Query frozen city information by a public city, district/neighborhood, or specific place name. A district may include the city name, e.g. Soho London or London Soho. A query may join multiple known districts with 'and'; all names must resolve uniquely. A district query includes notices for places in that district and citywide notices; a returned place notice still affects only its stated scope. Dynamic road notices return scope.geometry as GeoJSON lines with coordinates in [longitude, latitude] order for the affected road extent; scope.mode, when present, identifies the affected travel mode. Places without a known district remain searchable by place or city. info_type is optional: use notice, traffic, event, or news, or combine categories such as 'notice traffic news'. Common plurals and related words are accepted. Omit info_type to query all categories. time_range is optional: use YYYY-MM-DD for a local calendar day, or an ISO 8601 timestamp with an explicit timezone, for example 2026-03-20T23:00:00+02:00. In dynamic episodes, omit it for currently effective notices; past queries match effective windows of already published notices, including expired ones. Future instants and wholly future intervals are rejected; date/range queries crossing now search only through now. Queries never advance virtual time or reveal unpublished events. A time interval can be start/end with both endpoints in that ISO format. An empty list means no frozen records matched the supplied filters.", 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {'district': {'type': 'string', 'description': "Public city, district/neighborhood, or specific place name, e.g. Cape Town, Rondebosch, Soho London, The Bund, V&A Waterfront, or a MapApp place name. Join known districts with 'and' to search their union; every part must match uniquely. District search does not expand any notice's affected area."}, 'info_type': {'type': 'string', 'description': "Optional: notice, traffic, event, news; combine as 'notice traffic news', or omit for all categories."}, 'time_range': {'type': 'string', 'description': 'Optional local date YYYY-MM-DD or ISO 8601 timestamp with timezone (e.g. 2026-03-20T23:00:00+02:00); interval: start/end in that timestamp format. Do not use natural-language dates.'}}}}]
