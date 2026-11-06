from __future__ import annotations
from pathlib import Path
from copy import deepcopy
import re
from typing import Any
from zoneinfo import ZoneInfo
from shared.io import read_json
from app.apps.base import BaseApp
'LocalServiceApp operations for services already identified in MapApp.\n\nThis App does not discover POIs and does not read task-private candidate lists.\nIt exposes only public, frozen service-operation facts keyed by ``name`` and\n``location``. MapApp remains authoritative for POI category, address, and\nroutes.\n'

class LocalServiceApp(BaseApp):
    name = 'LocalServiceApp'

    def __init__(self, task_dir: str='', world_state_paths: list[str] | None=None, world_paths: list[str] | None=None):
        super().__init__()
        self._public_services: list[dict[str, Any]] = []
        self._event_history: list[dict[str, Any]] = []
        self._dynamic_baselines: dict[int, dict[str, Any]] = {}
        self._current_time = None
        self._timezone = ''
        self._reservation_ids: dict[tuple[str, int, str], str] = {}
        self._restored_reservation_ids: set[tuple[str, int, str]] = set()
        for path in world_state_paths or []:
            self._load_world_state(path)
        if task_dir:
            self._load(task_dir)
        from shared.opening_hours import effective_opening_hours
        for path in world_paths or []:
            world = read_json(Path(path))
            zones = {str(city.get('city_id') or ''): city.get('timezone') for city in world.get('cities', []) if isinstance(city, dict)}
            for place in world.get('places', []):
                (name, location) = (place.get('name'), place.get('location'))
                if not name or not isinstance(location, dict):
                    continue
                service = self._resolve_public_service(self._public_services, name, location)
                if service is None:
                    self._merge_public_service({'name': name, 'location': dict(location)})
                    service = self._resolve_public_service(self._public_services, name, location)
                if service is None:
                    continue
                hours = effective_opening_hours(place)
                if hours is not None:
                    service['opening_hours'] = deepcopy(hours)
                zone = zones.get(str(place.get('city_id') or ''))
                if isinstance(zone, str) and zone:
                    service['_operating_timezone'] = zone

    @staticmethod
    def _same_location(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
        try:
            return abs(float((left or {}).get('lat')) - float((right or {}).get('lat'))) <= 1e-05 and abs(float((left or {}).get('lng')) - float((right or {}).get('lng'))) <= 1e-05
        except (TypeError, ValueError):
            return False

    def _resolve_public_service(self, items: list[dict[str, Any]], name: str, location: dict[str, Any] | None) -> dict[str, Any] | None:
        normalized = str(name or '').strip().casefold()
        if not normalized or not location:
            return None
        matches = [item for item in items if str(item.get('name') or '').strip().casefold() == normalized and self._same_location(location, item.get('location'))]
        return matches[0] if len(matches) == 1 else None

    def _merge_public_service(self, service: dict[str, Any]) -> None:
        """Merge a declared service state by public identity, never storage ID."""
        name = str(service.get('name') or '')
        location = service.get('location')
        if not name or not isinstance(location, dict):
            return
        public_fields = {key: value for (key, value) in service.items() if key not in {'service_id', 'place_id'}}
        existing = self._resolve_public_service(self._public_services, name, location)
        if existing:
            existing.update(public_fields)
        else:
            self._public_services.append(public_fields)

    def _load(self, task_dir: str) -> None:
        root = Path(task_dir)
        if (root / 'task.json').is_file():
            task = read_json(root / 'task.json')
            self._timezone = str((task.get('time_context') or {}).get('timezone') or '')
        sandbox_path = root / 'task_sandbox.json'
        if sandbox_path.is_file():
            sandbox = read_json(sandbox_path)
            self._timezone = str((sandbox.get('time_context') or {}).get('timezone') or self._timezone)
            for service in sandbox.get('public_services', []) if isinstance(sandbox, dict) else []:
                if isinstance(service, dict):
                    self._merge_public_service(service)
        snapshot_path = root / 'static_app_state.json'
        if snapshot_path.is_file():
            snapshot = read_json(snapshot_path)
            apps = snapshot.get('apps', {}) if isinstance(snapshot, dict) else {}
            state = apps.get('LocalServiceApp', {}) if isinstance(apps, dict) else {}
            for service in state.get('services', []) if isinstance(state, dict) else []:
                if isinstance(service, dict):
                    self._merge_public_service(service)

    def _load_world_state(self, path: str) -> None:
        """Load the complete version-bound LocalServiceApp projection first."""
        state_path = Path(path)
        if not state_path.is_file():
            return
        snapshot = read_json(state_path)
        for service in snapshot.get('services', []) if isinstance(snapshot, dict) else []:
            if isinstance(service, dict):
                self._merge_public_service(service)

    def inject_entry(self, entry: dict[str, Any]) -> None:
        """Apply a time-indexed operating-state update to its public identity."""
        self._event_history.append(dict(entry))
        scope = entry.get('public_scope') or {}
        service = self._resolve_public_service(self._public_services, str(scope.get('name') or ''), scope.get('location'))
        if not service:
            return
        self._dynamic_baselines.setdefault(id(service), deepcopy(service))
        for field in ('open_status', 'queue_time_min', 'availability', 'crowding_level'):
            if field in entry:
                service[field] = entry[field]
        message = str(entry.get('public_message') or '').strip()
        if message:
            service.setdefault('service_notices', []).append({'published_at': str(entry.get('timestep') or ''), 'message': message})

    def refresh_dynamic_state(self, current) -> None:
        if isinstance(current, str):
            from app.runtime.time_manager import TimeManager
            current = TimeManager._parse_time(current)
        self._current_time = current
        from shared.event_time import event_active_at
        fields = ('open_status', 'queue_time_min', 'availability', 'crowding_level')
        for service in self._public_services:
            baseline = self._dynamic_baselines.get(id(service))
            if baseline is None:
                continue
            for field in fields:
                if field in baseline:
                    service[field] = deepcopy(baseline[field])
                else:
                    service.pop(field, None)
            for entry in self._event_history:
                scope = entry.get('public_scope') or {}
                if (not entry.get('end_time') or event_active_at(entry, current)) and self._resolve_public_service([service], str(scope.get('name') or ''), scope.get('location')):
                    for field in fields:
                        if field in entry:
                            service[field] = entry[field]

    def _operating_state(self, service: dict[str, Any]) -> dict[str, Any]:
        from shared.opening_hours import valid_periods
        from shared.event_time import event_active_at
        current = self.env.time_manager.virtual_time() if self.env is not None else self._current_time
        hours = service.get('opening_hours')
        status = 'unknown'
        source = 'insufficient_information'
        zone = service.get('_operating_timezone') or self._timezone
        if current is not None and current.tzinfo is not None and zone and valid_periods(hours):
            local = current.astimezone(ZoneInfo(zone))
            point = (local.weekday() + 1) % 7 * 86400 + local.hour * 3600 + local.minute * 60 + local.second

            def second(value):
                return value['day'] * 86400 + value.get('hour', 0) * 3600 + value.get('minute', 0) * 60
            opened = False
            for period in hours['periods']:
                if 'close' not in period:
                    opened = True
                    break
                (start, end) = (second(period['open']), second(period['close']))
                if end <= start:
                    end += 7 * 86400
                if any((start <= candidate < end for candidate in (point, point + 7 * 86400))):
                    opened = True
                    break
            (status, source) = ('open' if opened else 'closed', 'opening_hours')
        baseline = self._dynamic_baselines.get(id(service), service)
        availability = 'unknown'
        for field in ('open_status', 'availability'):
            value = baseline.get(field)
            if value:
                if field == 'open_status' and source == 'insufficient_information':
                    status = value
                elif field == 'availability':
                    availability = value
                if source == 'insufficient_information':
                    source = 'frozen_service_state'
        for entry in self._event_history:
            scope = entry.get('public_scope') or {}
            if current is not None and event_active_at(entry, current) and self._resolve_public_service([service], scope.get('name', ''), scope.get('location')):
                status = entry.get('open_status', status)
                availability = entry.get('availability', availability)
                source = 'active_event'
        if status == 'closed':
            availability = 'unavailable'
        elif availability == 'unknown' and status == 'open':
            availability = 'available'
        return {'open_status': status, 'availability': availability, 'source': source}

    def get_service_detail(self, name: str, location: dict[str, Any]) -> dict[str, Any]:
        service = self._resolve_public_service(self._public_services, name, location)
        if not service:
            return {'found': False}
        return {'found': True, 'name': service.get('name', ''), 'location': dict(service.get('location') or {}), 'price_level': service.get('price_level'), 'price_per_person': service.get('price_per_person', service.get('price_pp')), 'opening_hours': service.get('opening_hours', service.get('open_until', '')), 'duration_min': service.get('duration_typical_min', service.get('duration_min', 60)), 'dietary_options': service.get('dietary_options', []), 'max_party': service.get('max_party', 0), **self._operating_state(service), 'queue_time_min': service.get('queue_time_min'), 'crowding_level': service.get('crowding_level', ''), 'service_notices': [dict(notice) for notice in service.get('service_notices', [])], 'merchant_information': dict(service.get('merchant_information') or {})}

    def check_availability(self, name: str, location: dict[str, Any], date: str='') -> dict[str, Any]:
        service = self._resolve_public_service(self._public_services, name, location)
        if not service:
            return {'found': False, 'status': 'service_not_found'}
        result = self._operating_state(service)
        return {'found': True, **result}

    def make_reservation(self, name: str, location: dict[str, Any], party_size: int, time: str='', guest_name: str='', special_requests: str='') -> dict[str, Any]:
        service = self._resolve_public_service(self._public_services, name, location)
        if not service:
            return {'status': 'failed', 'error': 'Service not found'}
        if service.get('open_status') == 'closed' or service.get('availability') == 'unavailable':
            return {'status': 'failed', 'error': 'Service is currently unavailable'}
        key = (service.get('name', ''), party_size, time)
        reservation_id = self._reservation_ids.setdefault(key, f'reservation_{abs(hash(key)) % 1000000:06d}')
        return {'status': 'confirmed', 'reservation_id': reservation_id, 'service_name': service.get('name', ''), 'time': time or '19:00', 'party_size': party_size}

    def _restore_reservation_result(self, args, actual, saved):
        """Restore an issued opaque ID during replay; verify all reservation facts.

        Python's process-salted hash cannot reproduce a previous process's ID.
        Retain that recorded identity in this App so later repeated calls agree.
        This private recovery hook is not part of the public tool interface.
        """
        fields = {'status', 'reservation_id', 'service_name', 'time', 'party_size'}
        if not isinstance(actual, dict) or not isinstance(saved, dict):
            return actual
        if set(actual) != fields or set(saved) != fields or actual['status'] != 'confirmed':
            return actual
        if any((actual[k] != saved[k] for k in fields - {'reservation_id'})):
            return actual
        if not isinstance(saved['reservation_id'], str) or not re.fullmatch('reservation_[0-9]{6}', saved['reservation_id']):
            return actual
        key = (actual['service_name'], args.get('party_size'), args.get('time', ''))
        if self._reservation_ids.get(key) != actual['reservation_id']:
            return actual
        if key in self._restored_reservation_ids and actual['reservation_id'] != saved['reservation_id']:
            return actual
        self._reservation_ids[key] = saved['reservation_id']
        self._restored_reservation_ids.add(key)
        return {**actual, 'reservation_id': saved['reservation_id']}

    def cancel_reservation(self, reservation_id: str) -> dict[str, Any]:
        return {'status': 'cancelled', 'reservation_id': reservation_id}

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        identity = {'name': {'type': 'string', 'description': 'Public service name'}, 'location': {'type': 'object', 'description': 'Coordinates returned by MapApp'}}
        return [{'name': 'get_service_detail', 'description': 'Read source-backed merchant/service details and operating state for an identified MapApp place. Missing prices are null; price_level is a grade and price_per_person is a separately recorded amount, never converted from a grade. Query MapApp.get_place_detail for the frozen Google price_range and service_attributes.', 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': identity, 'required': ['name', 'location']}}, {'name': 'check_availability', 'description': 'Read current operating status using local opening hours and active service events. Unknown means insufficient information; available does not assert seating, stock or reservation capacity. The legacy date parameter does not request future availability.', 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {**identity, 'date': {'type': 'string'}}, 'required': ['name', 'location']}}, {'name': 'make_reservation', 'description': 'Reserve an already identified service.', 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {**identity, 'party_size': {'type': 'integer'}, 'time': {'type': 'string'}, 'guest_name': {'type': 'string'}, 'special_requests': {'type': 'string'}}, 'required': ['name', 'location', 'party_size']}}]
