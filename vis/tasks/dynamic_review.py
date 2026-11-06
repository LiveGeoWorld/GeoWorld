import hashlib
import json
from pathlib import Path, PurePosixPath
from shapely.geometry import Point
from shared.io import read_json
from app.assets.benchmark_layout import latest_batch, task_dirs
from app.runtime.dynamic_events import normalize_event_payload
from app.assets.safety.occupancy import scope_geometry
from world_module.service_detail_snapshot import public_identity, identity_key
'Read the selected dynamic batch and its exact frozen sources for review only.'
STATE_FIELDS = {'open_status', 'availability', 'queue_time_min', 'crowding_level'}

def bound_source(root, source, world, city):
    parts = PurePosixPath(str(source['task_folder']).replace('\\', '/')).parts
    if 'benchmarks' in parts:
        parts = parts[parts.index('benchmarks') + 1:]
    root = Path(root).resolve()
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root) or len(parts) != 6 or parts[1:3] != (world, city['city_id']):
        raise ValueError('动态来源的世界或城市绑定不一致')
    sandbox = 'route_sandbox.json' if source['task_type'] == 'route_planning' else 'task_sandbox.json'
    for (name, key) in [('task.json', 'task_json_sha256'), (sandbox, 'sandbox_sha256')]:
        if hashlib.sha256((path / name).read_bytes()).hexdigest() != source[key]:
            raise ValueError('动态来源内容与冻结哈希不一致')
    if read_json(path / 'task.json')['task_id'] != source['task_id']:
        raise ValueError('动态来源任务身份不一致')
    if parts[0] != source['task_type']:
        raise ValueError('动态来源类型不一致')
    provenance = read_json(path / 'task_provenance.json')
    context = provenance['generation_context']
    version = context.get('city_version') or context.get('world_city_versions', {}).get(city['city_id'])
    if provenance['world_id'] != world or str(version).removeprefix('v') != str(city['active_version']):
        raise ValueError('动态来源与当前城市版本不一致')
    return path

def service_identity(event, services):
    public = public_identity(event.get('public_scope') or {})
    ref = event.get('scope', {}).get('id')
    matches = {identity_key(identity): identity for service in services if ref and ref in (service.get('place_id'), service.get('service_id')) and ((identity := public_identity(service)) is not None)}
    if public:
        if matches and identity_key(public) not in matches:
            raise ValueError('动态事件公开地点与绑定地点不一致')
        return public
    if len(matches) == 1:
        return next(iter(matches.values()))
    raise ValueError('动态事件缺少可唯一定位的服务地点')

def patch_event(event, services):
    identity = service_identity(event, services)
    location = identity['location']
    return ({'title': event.get('description') or '服务状态更新', 'active_window': {'start': event['activation_time'], 'end': event.get('end_time') or None}, 'review_patch': {k: v for (k, v) in event['state_patch'].items() if k in STATE_FIELDS}, 'review_identity': identity_key(identity)}, Point(location['lng'], location['lat']))

def load_dynamic_review(root, world, cities, world_paths=()):
    result = {}
    for city in cities:
        output = result[city['city_id']] = {'events': [], 'protections': [], 'error': None, 'issues': [], 'overview': []}
        batch = latest_batch(root, 'dynamic_replanning', world, city['city_id'])
        if batch is None:
            output['error'] = '当前城市尚无动态批次'
            continue
        (seen, initial_sources) = ({}, set())
        for folder in task_dirs(batch):
            try:
                sandbox = read_json(folder / 'task_sandbox.json')
                source = bound_source(root, sandbox['source_task'], world, city)
                snapshot = read_json(folder / 'static_app_state.json')
                services = snapshot.get('apps', {}).get('LocalServiceApp', {}).get('services', [])
                services = list(services)
                for path in world_paths:
                    if city['city_id'] in path.parts:
                        services.extend(read_json(path).get('places', []))
                if source not in initial_sources:
                    initial_sources.add(source)
                    if sandbox['source_task']['task_type'] == 'safety_planning':
                        history = read_json(source / 'safety_evidence_history.json')
                        if history['world_binding'] != f"{world}:{city['city_id']}:v{city['active_version']}":
                            raise ValueError('动态初始事件版本不一致')
                        for entry in history['entries']:
                            context = entry['context']
                            anchors = {a['scope_ref']: a for a in context['geographic_anchors']}
                            for event in entry['event'].get('events', [entry['event']]):
                                output['events'].append((event, scope_geometry(anchors[event['scope_ref']], context)))
                            for protection in context.get('compiled_gt_protection', []):
                                for scope_ref in protection.get('scope_refs', []):
                                    anchor = anchors.get(scope_ref)
                                    if not anchor:
                                        continue
                                    output['protections'].append({'task_id': entry['owner'], 'candidate_ref': protection.get('candidate_ref', ''), 'dependency_kind': protection.get('dependency_kind', 'place'), 'source_refs': protection.get('source_refs', []), 'active_window': protection.get('active_window'), 'scope_ref': scope_ref, 'geometry': scope_geometry(anchor, context)})
                start = sandbox['time_context'].get('episode_start') or sandbox['time_context']['instruction_time']
                for service in snapshot.get('apps', {}).get('LocalServiceApp', {}).get('services', []):
                    patch = {k: service[k] for k in STATE_FIELDS if k in service}
                    if patch:
                        output['events'].append(patch_event({'public_scope': service, 'scope': {}, 'activation_time': start, 'state_patch': patch, 'description': '初始服务状态'}, services))
                for event in normalize_event_payload(read_json(folder / 'env_events.json')):
                    event_id = event['event_id']
                    if event_id in seen and seen[event_id] != event:
                        raise ValueError('动态事件副本不一致')
                    seen[event_id] = event
                for event in normalize_event_payload(read_json(folder / 'env_events.json')):
                    if event['activation']['mode'] != 'absolute' or not event['activation_time']:
                        raise ValueError('动态事件缺少绝对生效时间')
                    from shapely.geometry import mapping
                    shape = None
                    if event['app'] == 'LocalServiceApp':
                        try:
                            (_, area) = patch_event(event, services)
                            shape = mapping(area)
                        except ValueError:
                            pass
                    output['overview'].append({'app': event['app'], '_state_key': (event['app'], event['scope']['type'], event['scope']['id'], event['state_field']), 'title': event['state_patch'].get('title') or event['description'] or '事件更新', 'description': event['state_patch'].get('description') or event['description'], 'active_window': {'start': event['activation_time'], 'end': event.get('end_time') or None}, 'geometry': shape})
                    if event['app'] == 'LocalServiceApp':
                        output['events'].append(patch_event(event, services))
                    elif event['app'] == 'WeatherApp' or (event['app'] == 'CityNewsApp' and event['scope']['type'] not in ('route', 'transit', 'route_operation')):
                        output['issues'].append('部分天气或区域事件缺少可计算的服务影响字段')
            except (OSError, KeyError, TypeError):
                output['issues'].append('部分动态记录缺少来源或状态资料')
            except ValueError as exc:
                output['issues'].append(str(exc))
        from datetime import datetime

        def instant(value):
            point = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if point.tzinfo is None:
                raise ValueError('动态事件时间缺少时区')
            return point
        try:
            (visiting, visited) = (set(), set())

            def visit(event_id):
                if event_id in visiting:
                    raise ValueError('动态事件依赖存在循环')
                if event_id in visited:
                    return
                visiting.add(event_id)
                event = seen[event_id]
                at = instant(event['activation_time'])
                if event.get('end_time') and instant(event['end_time']) <= at:
                    raise ValueError('动态事件有效时间错误')
                for parent in event['causal_parents']:
                    if parent not in seen or instant(seen[parent]['activation_time']) > at:
                        raise ValueError('动态事件依赖不完整或时间顺序错误')
                    visit(parent)
                visiting.remove(event_id)
                visited.add(event_id)
            for event_id in seen:
                visit(event_id)
        except (ValueError, TypeError):
            output['issues'].append('动态事件时间或因果依赖无效')
            output['events'] = []
            output['overview'] = []
        unique = {}
        for (event, area) in output['events']:
            unique[json.dumps(event, sort_keys=True, ensure_ascii=False), area.wkb] = (event, area)
        output['events'] = list(unique.values())
        ordered = sorted(output['overview'], key=lambda r: instant(r['active_window']['start']))
        for (index, row) in enumerate(ordered):
            later = next((other for other in ordered[index + 1:] if other['_state_key'] == row['_state_key'] and instant(other['active_window']['start']) > instant(row['active_window']['start'])), None)
            if later and (not row['active_window']['end'] or instant(later['active_window']['start']) < instant(row['active_window']['end'])):
                row['active_window']['end'] = later['active_window']['start']
        for row in ordered:
            row.pop('_state_key', None)
        if output['issues']:
            output['error'] = '；'.join(dict.fromkeys(output['issues']))
    return result
