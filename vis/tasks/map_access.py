from functools import lru_cache
from pathlib import Path
from threading import Lock
from app.runtime.environment import Environment
from app.apps.map_app import MapAppSandbox
"Read-only MapApp access using the tested Agent's sandbox and dispatcher."
TOOLS = ('search_places', 'get_place_detail', 'query_routes')
_lock = Lock()

def _identity(path):
    path = Path(path).resolve()
    stat = path.stat()
    return (str(path), stat.st_size, stat.st_mtime_ns)

@lru_cache(maxsize=1)
def _load(worlds, routes):
    app = MapAppSandbox(world_paths=[row[0] for row in worlds])
    for row in routes:
        app._load_cache(row[0])
    return app

@lru_cache(maxsize=1)
def _shanghai_map(identities):
    return MapAppSandbox(world_paths=[row[0] for row in identities])

def invoke(world_paths, route_paths, tool, arguments, shanghai_world_paths=()):
    if tool not in TOOLS or not isinstance(arguments, dict):
        raise ValueError('Only the three public MapApp read methods are supported')
    worlds = tuple((_identity(p) for p in world_paths))
    routes = tuple((_identity(p) for p in route_paths))
    if not worlds:
        raise ValueError('当前世界没有可读取的地图资产。')
    with _lock:
        app = _load(worlds, routes)
    env = Environment()
    env.register_app(app)
    env.configure_public_tools({'MapApp': list(TOOLS)})
    name = 'MapApp__' + tool
    observation = env.execute_tool(name, arguments)
    result = dict(tool=name, arguments=arguments, observation=observation)
    if tool == 'query_routes':
        city_map = _shanghai_map(tuple((_identity(p) for p in shanghai_world_paths)))
        in_shanghai = any((city_map._resolve_identity(arguments.get(key, {}).get('name', ''), arguments.get(key, {}).get('location')) for key in ('origin', 'destination')))
        result['viewer_basemap'] = 'configured' if in_shanghai else 'osm'
    return result
