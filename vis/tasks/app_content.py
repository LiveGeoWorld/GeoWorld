from pathlib import Path, PurePosixPath
import json
from copy import deepcopy
from shared.io import read_json
from app.assets.task_types import SOURCE_TYPES
from app.runtime.environment import Environment
from app.apps.map_app import MapAppSandbox
from app.apps.local_service import LocalServiceApp
from app.apps.messaging import MessagingApp
from app.apps.calendar import CalendarApp
from app.apps.weather_app import WeatherApp
from app.apps.city_news import CityNewsApp
from app.apps.user_interface import UserInterfaceApp
'Read-only author preview of the real sandbox Apps, never raw task/GT dumps.'
TYPES = [*SOURCE_TYPES, 'dynamic_replanning']

def catalog(root):
    root = Path(root).resolve()
    (tasks, errors) = ([], [])
    for kind in TYPES:
        for path in (root / kind).glob('*/*/*/tasks/*/task.json'):
            try:
                task = read_json(path)
                folder = path.parent
                provenance = read_json(folder / 'task_provenance.json')
                context = provenance.get('generation_context') or {}
                parts = folder.relative_to(root).parts
                version = context.get('city_version') or (context.get('world_city_versions') or {}).get(parts[2])
                tasks.append(dict(folder=folder.relative_to(root).as_posix(), world=parts[1], city=parts[2], version=str(version or '未知').removeprefix('v'), batch=parts[3], task_type=kind, task_id=task.get('task_id') or folder.name, label=(task.get('user_prompt') or '')[:90], mode='dynamic' if kind == 'dynamic_replanning' else 'static'))
            except (OSError, ValueError, TypeError) as exc:
                errors.append(dict(folder=path.parent.relative_to(root).as_posix(), error=str(exc)))
    names = {}
    for path in (root.parent / 'worlds').glob('*/cities/*.json'):
        try:
            record = read_json(path)
            names[path.stem] = record.get('city_name') or record.get('name') or path.stem
        except (OSError, ValueError):
            continue
    return dict(tasks=sorted(tasks, key=lambda r: (r['world'], r['city'], r['batch'], r['task_id']), reverse=True), errors=errors, city_names=names)

def resolve_folder(root, reference):
    root = Path(root).resolve()
    folder = (root / str(reference)).resolve()
    if not folder.is_relative_to(root) or not (folder / 'task.json').is_file():
        raise ValueError('任务路径无效或不在当前数据目录内。')
    if read_json(folder / 'task.json').get('task_type') not in TYPES:
        raise ValueError('该任务类型尚未接入此 App 预览。')
    return folder

def _environment(folder, event_mode="shared_world"):
    from app.runtime.episode import load_episode, _resolve_place_world_snapshot
    _resolve_place_world_snapshot(None,folder,read_json(folder / "task.json"))
    from app.runtime.asset_bindings import _resolve_route_cache
    episode=load_episode(folder,route_cache_path=_resolve_route_cache(None,folder.parent),dynamic_event_mode=event_mode)
    return episode['env'],{},episode['task'],episode['sandbox'],episode['env'].time_manager.virtual_time_iso()

def _rows(app):
    """Enumerate public identities, then call existing public read methods."""
    if app.name == 'MapApp':
        rows = [dict(category='地点', **app.get_place_detail(p['name'], p['location'])) for p in app._places.values()]
        for (origin, dest) in app._route_index:
            (a, b) = (app._places.get(origin), app._places.get(dest))
            if a and b:
                rows.append(dict(category='路线', name=a['name'] + ' → ' + b['name'], origin={'name': a['name'], 'location': a['location']}, destination={'name': b['name'], 'location': b['location']}, **app.query_routes(a, b)))
        return rows
    if app.name == 'LocalServiceApp':
        return [dict(category='服务', **app.get_service_detail(s['name'], s['location'])) for s in app._public_services]
    if app.name == 'WeatherApp':
        notices = app.get_alerts()
        return [dict(category='天气事件', **r) for r in notices['alerts']] + [dict(category='天气事件更新', **r) for r in notices.get('updates', [])]
    if app.name == 'CityNewsApp':
        return [dict(category='公告', **r) for r in app.get_info()]
    if app.name == 'CalendarApp':
        return [dict(category='Calendar', **app.get_availability(p.get('name', ''))) for p in app._sandbox.get('participants', [])]
    if app.name == 'MessagingApp':
        return [dict(category='会话', **app.get_contact(contact_name=p.get('name', '')), **app.get_conversation_history(p.get('character_id', ''), limit=100000)) for p in app._sandbox.get('participants', [])]
    return []

def preview(root, reference, node=0, event_mode="shared_world"):
    folder = resolve_folder(root, reference)
    (env, errors, task, sandbox, start) = _environment(folder,event_mode)
    parser = env.time_manager._parse_time
    from app.runtime.episode import event_checkpoints
    times = event_checkpoints(env)
    if isinstance(node, bool) or not isinstance(node, int) or (not 0 <= node <= len(times)):
        raise ValueError('无效事件节点。')
    baseline = {}
    if node:
        for (name, app) in env.apps.items():
            try:
                baseline[name] = deepcopy(_rows(app))
            except (ValueError, TypeError, KeyError) as exc:
                errors[name] = str(exc)
        if node > 1:
            env.advance_virtual_time(times[node - 2], reason='read_only_preview')
            baseline = {name: deepcopy(_rows(app)) for (name, app) in env.apps.items() if name not in errors}
        env.advance_virtual_time(times[node - 1], reason='read_only_preview')
    apps = []
    for name in ['MapApp', 'LocalServiceApp', 'WeatherApp', 'CityNewsApp', 'CalendarApp', 'MessagingApp', 'UserInterfaceApp']:
        if name in errors:
            apps.append(dict(name=name, error=errors[name], rows=[], count=None))
            continue
        app = env.apps.get(name)
        if app is None:
            continue
        try:
            rows = _rows(app)
            previous = {json.dumps(r, sort_keys=True, ensure_ascii=False) for r in baseline.get(name, [])}
            apps.append(dict(name=name, count=len(rows), rows=[dict(content=r, changed=bool(node and json.dumps(r, sort_keys=True, ensure_ascii=False) not in previous)) for r in rows]))
        except (ValueError, TypeError, KeyError) as exc:
            apps.append(dict(name=name, error=str(exc), rows=[], count=None))
    source = sandbox.get('source_task') or {}
    parts = PurePosixPath(str(source.get('task_folder') or '').replace('\\', '/')).parts
    source_ref = ''
    if 'benchmarks' in parts:
        source_ref = '/'.join(parts[parts.index('benchmarks') + 1:])
    try:
        if source_ref:
            source_path = resolve_folder(root, source_ref)
            if read_json(source_path / 'task.json').get('task_id') != source.get('task_id'):
                source_ref = ''
    except ValueError:
        source_ref = ''
    return dict(task_id=task.get('task_id'), mode='dynamic' if task['task_type'] == 'dynamic_replanning' else 'static', time=env.time_manager.virtual_time_iso() or start, nodes=[dict(index=0, label='Initial state', time=start)] + [dict(index=i, label=f'Event checkpoint {i}', time=t) for (i, t) in enumerate(times, 1)], node=node, apps=apps, source_folder=source_ref)
