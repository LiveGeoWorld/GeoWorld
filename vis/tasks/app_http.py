"""Same-process adapter for the existing author App explorer endpoints."""
import json
import sys
from pathlib import Path
from shared.io import read_json
from vis.tasks.server import ROOT as PIPELINE_ROOT, send

class AppHTTP:
    def __init__(self, handler, benchmarks_root):
        self.handler = handler
        self.benchmarks_root = benchmarks_root
    def _json(self, payload):
        send(self.handler, payload)
    def _error(self, message, status):
        send(self.handler, {"error": message}, status)
    def _current_browsing_world_id(self):
        return self.handler.active_world_id
    def _current_world_cities(self):
        studio = sys.modules[type(self.handler).__module__]
        worlds = studio._registered_worlds()
        selected = next((w for w in worlds if w['world_id'] == self._current_browsing_world_id()), {})
        return self._normalise_world_cities(selected.get('cities', []))

    def _active_city_artifacts(self, world_id: str, city: str, version: int) -> dict:
        record_path = PIPELINE_ROOT / 'outputs' / 'worlds' / world_id / 'cities' / f'{city}.json'
        if not record_path.is_file():
            raise FileNotFoundError(f'City registry not found: {city}。')
        record = read_json(record_path)
        active = next((item for item in record.get('city_versions') or [] if int(item.get('version') or 0) == int(version)), None)
        return dict((active or record).get('artifacts') or {})

    @staticmethod
    def _registered_artifact_path(artifacts: dict, name: str) -> Path | None:
        reference = artifacts.get(name) or {}
        value = str(reference.get('path') or '')
        if not value:
            return None
        path = Path(value)
        if not path.is_absolute():
            path = PIPELINE_ROOT / path
        return path if path.is_file() else None

    @staticmethod
    def _normalise_world_cities(cities: object) -> list[dict]:
        """Expose active registered cities through one compact UI contract."""
        if not isinstance(cities, list):
            return []
        options: list[dict] = []
        for raw in cities:
            if not isinstance(raw, dict):
                continue
            city_id = str(raw.get('city_id') or '').strip()
            if not city_id or str(raw.get('lifecycle_status') or 'active') != 'active':
                continue
            version_text = str(raw.get('active_version') or 1).strip().lower()
            if version_text.startswith('v'):
                version_text = version_text[1:]
            if not version_text.isdigit() or int(version_text) < 1:
                continue
            version = int(version_text)
            name = str(raw.get('city_name') or city_id).strip()
            options.append({'city_id': city_id, 'city_name': name, 'active_version': version, 'label': f'{name} · v{version}'})
        return sorted(options, key=lambda item: (item['city_name'].casefold(), item['city_id']))

    def handle(self, path, params):
        if path in ('/api/app-content/query','/api/app-content/map-query') and params.get('folder',[''])[0]:
            from vis.tasks.app_episode import query_episode
            try:
                world=self._current_browsing_world_id()
                if params.get('world',[''])[0] != world:
                    raise ValueError('The current world has changed. Reopen APP.')
                self._json(query_episode(self.benchmarks_root,params['folder'][0],world,
                    params.get('app',['MapApp'])[0],params.get('tool',[''])[0],
                    json.loads(params.get('args',['{}'])[0]),node=int(params.get('node',['0'])[0]),
                    event_mode=params.get('event_mode',['shared_world'])[0]))
            except (OSError,ValueError,TypeError,KeyError) as exc:
                self._error(str(exc),400)
            return True
        if path == '/api/app-content/query':
            from vis.tasks.app_access import invoke
            try:
                world = self._current_browsing_world_id()
                if params.get('world', [''])[0] != world:
                    raise ValueError('The current world has changed. Reopen APP.')
                cities = self._current_world_cities()
                (services, service_warnings, service_worlds) = ([], [], [])
                service_zones = {}
                if params.get('app', [''])[0] == 'LocalServiceApp':
                    for city in cities:
                        assets = self._active_city_artifacts(world, city['city_id'], city['active_version'])
                        map_asset = self._registered_artifact_path(assets, 'map_world')
                        if map_asset is not None:
                            service_worlds.append(map_asset)
                            registry_path = PIPELINE_ROOT / 'outputs' / 'worlds' / world / 'cities' / (city['city_id'] + '.json')
                            if registry_path.is_file():
                                service_zones[str(map_asset.resolve())] = json.loads(registry_path.read_text(encoding='utf-8')).get('timezone')
                        state = self._registered_artifact_path(assets, 'local_service_world_state')
                        if state is None:
                            service_warnings.append(f"{city['city_name']} has no service state assets; its services are excluded from this query.")
                        else:
                            services.append(state)
                    if not services:
                        raise ValueError('The current world has no readable service state assets.')
                result = invoke(self.benchmarks_root, world, cities, services, params.get('app', [''])[0], params.get('tool', [''])[0], json.loads(params.get('args', ['{}'])[0]), params.get('mode', ['static'])[0], service_worlds, service_zones)
                result['warnings'].extend(service_warnings)
                self._json(result)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self._error(str(exc), 400)
        elif path == '/api/app-content/map-query':
            from vis.tasks.map_access import invoke
            try:
                world = self._current_browsing_world_id()
                if params.get('world', [''])[0] != world:
                    raise ValueError('The current world has changed. Reopen APP.')
                (worlds, routes, warnings, shanghai_worlds) = ([], [], [], [])
                for city in self._current_world_cities():
                    artifacts = self._active_city_artifacts(world, city['city_id'], city['active_version'])
                    asset = self._registered_artifact_path(artifacts, 'map_world')
                    if asset is None:
                        raise ValueError(f"{city['city_name']} map assets are missing.")
                    worlds.append(asset)
                    if 'shanghai' in city['city_id'].lower() or str(city.get('city_name', '')).lower() in ('shanghai', '上海'):
                        shanghai_worlds.append(asset)
                    cache = self._registered_artifact_path(artifacts, 'route_cache')
                    if cache is not None:
                        routes.append(cache)
                    else:
                        warnings.append(f"{city['city_name']} has no route cache; external route APIs will not be called.")
                result = invoke(worlds, routes, params.get('tool', [''])[0], json.loads(params.get('args', ['{}'])[0]), shanghai_worlds)
                self._json(dict(world_id=world, warnings=warnings, **result))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self._error(str(exc), 400)
        elif path == '/api/app-content/event-overview':
            from vis.tasks.event_overview import overview
            world = self._current_browsing_world_id()
            if params.get('world', [''])[0] != world:
                self._error('The current world has changed. Reopen APP.', 409)
            else:
                try:
                    self._json(overview(self.benchmarks_root, world, self._current_world_cities(), params.get('city', [''])[0], params.get('mode', ['static'])[0], params.get('time', [''])[0], params.get('layers', ['events'])[0] == 'events_protection'))
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    self._error(str(exc), 400)
        elif path == '/api/app-content/static-events':
            from vis.tasks.app_events import static_events
            world = self._current_browsing_world_id()
            if params.get('world', [''])[0] != world:
                self._error('The current world has changed. Reopen APP.', 409)
            else:
                self._json(static_events(self.benchmarks_root, world, self._current_world_cities()))
        elif path == '/api/app-content/catalog':
            from vis.tasks.app_content import catalog
            self._json(catalog(self.benchmarks_root))
        elif path == '/api/app-content/preview':
            from vis.tasks.app_content import preview
            try:
                self._json(preview(self.benchmarks_root, params.get('folder', [''])[0], int(params.get('node', ['0'])[0]), params.get('event_mode',['shared_world'])[0]))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                self._error(str(exc), 400)
        else:
            return False
        return True
