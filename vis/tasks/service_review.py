import json
from shapely.geometry import Point
from shared.io import read_json
from app.assets.benchmark_layout import latest_batch, task_dirs
from app.assets.safety.event_contract import apply_event, public_event
from app.assets.safety.occupancy import scope_geometry
from app.assets.safety.task_time import applies
from vis.tasks.app_events import static_events
from world_module.service_detail_snapshot import identity_key, public_identity
'Author-only service impact projection. Never installed as an Agent tool.'

def _key(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)

def load_review_events(root, world, cities):
    public = static_events(root, world, cities)
    result = {}
    for (city, source) in zip(cities, public['sources']):
        city_id = city['city_id']
        result[city_id] = {'events': [], 'protections': [], 'error': None}
        try:
            if source['status'] == 'error':
                raise ValueError(source['error'])
            if source['status'] == 'no_batch':
                continue
            expected = {_key(row['content']) for rows in public['apps'].values() for row in rows if row['city'] == source['city_name'] and row['batch'] == source['batch']}
            batch = latest_batch(root, 'safety_planning', world, city_id)
            folders = list(task_dirs(batch))
            owners = {read_json(folder / 'task.json')['task_id'] for folder in folders}
            seen = {}
            protection_seen = {}
            for folder in folders:
                history = read_json(folder / 'safety_evidence_history.json')
                if history['world_binding'] != f"{world}:{city_id}:v{city['active_version']}":
                    raise ValueError('事件与当前城市版本不一致')
                for entry in history['entries']:
                    if entry['owner'] not in owners:
                        continue
                    context = entry['context']
                    anchors = {a['scope_ref']: a for a in context['geographic_anchors']}
                    bundle = entry['event']
                    for event in bundle.get('events', [bundle]):
                        anchor = anchors[event['scope_ref']]
                        key = _key(public_event(event, anchor))
                        if key not in expected:
                            raise ValueError('结构化事件与共享公开视图不一致')
                        signature = _key({'event': event, 'anchor': anchor})
                        if key in seen and seen[key][0] != signature:
                            raise ValueError('结构化事件副本不一致')
                        seen[key] = (signature, (event, scope_geometry(anchor, context)))
                    for protection in context.get('compiled_gt_protection', []):
                        for scope_ref in protection.get('scope_refs', []):
                            anchor = anchors.get(scope_ref)
                            if not anchor:
                                continue
                            geometry = scope_geometry(anchor, context)
                            row = {'task_id': entry['owner'], 'candidate_ref': protection.get('candidate_ref', ''), 'dependency_kind': protection.get('dependency_kind', 'place'), 'source_refs': protection.get('source_refs', []), 'active_window': protection.get('active_window'), 'scope_ref': scope_ref, 'geometry': geometry}
                            key = _key({k: row[k] for k in ('task_id', 'scope_ref', 'active_window')})
                            protection_seen[key] = row
            if set(seen) != expected:
                raise ValueError('结构化事件记录不完整')
            result[city_id]['events'] = [item[1] for item in seen.values()]
            result[city_id]['protections'] = list(protection_seen.values())
        except (OSError, KeyError, TypeError):
            result[city_id] = {'events': [], 'protections': [], 'error': '结构化事件资料缺失或格式不完整'}
        except ValueError as exc:
            result[city_id] = {'events': [], 'protections': [], 'error': str(exc)}
    return result

def project_status(row, place, events, time, error=None):
    if not time:
        return
    row['review_impacts'] = []
    if error:
        row['status_note'] = error
    location = row['location']
    point = Point(location['lng'], location['lat'])
    projection = place.get('map_projection') or {}
    facts = {**place, **projection, **(projection.get('google_place_details') or {})}
    unavailable = False
    unknown = bool(error)
    patches = {}
    from datetime import datetime

    def instant(value):
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError('事件时间必须包含时区')
        return result
    current = instant(time)
    for (event, area) in events:
        if 'review_patch' in event:
            window = event['active_window']
            if event['review_identity'] != identity_key(row) or current < instant(window['start']):
                continue
            if window.get('end') and current >= instant(window['end']):
                continue
            for (field, value) in event['review_patch'].items():
                previous = patches.get(field)
                at = instant(window['start'])
                if previous and at == previous[0] and (value != previous[1]):
                    unknown = True
                    row['status_note'] = '同一时刻存在冲突的服务状态'
                if previous is None or at > previous[0]:
                    patches[field] = (at, value, event)
            continue
        if not applies(event['active_window'], time) or not area.intersects(point):
            continue
        impact = apply_event(event, [{'candidate_ref': 'review', 'facts': facts}], scope_members={'review'}, activity_window=time)['review']
        unavailable |= impact['unavailable']
        unknown |= bool(impact['unknown_effects'])
        for index in impact['matched_effects']:
            effect = event['effects'][index]
            label = '服务不可用' if effect['kind'] == 'unavailable' else '通行延误'
            row['review_impacts'].append({'title': event['title'], 'status': label, 'reason': effect.get('reason', ''), 'active_window': event['active_window']})
    labels = {'open_status': {'closed': '暂停营业', 'open': '营业'}, 'availability': {'unavailable': '服务不可用', 'available': '服务可用'}}
    for (field, (_, value, event)) in patches.items():
        if field in labels:
            label = labels[field].get(value)
            if label is None:
                unknown = True
                continue
            unavailable |= value in ('closed', 'unavailable')
        elif field == 'queue_time_min':
            label = f'排队约 {value} 分钟'
        else:
            label = f'拥挤程度：{value}'
        row['review_impacts'].append({'title': event['title'], 'status': label, 'reason': '', 'active_window': event['active_window']})
    row['service_status'] = '事件状态无法确认' if unknown else '受事件影响，服务不可用' if unavailable else row.get('regular_status')

def add_static_review(observation, root, world, cities, world_paths, mode='static'):
    time = observation.get('query_time')
    if not time:
        return
    if mode == 'dynamic':
        from vis.tasks.dynamic_review import load_dynamic_review
        sources = load_dynamic_review(root, world, cities, world_paths)
    else:
        sources = load_review_events(root, world, cities)
    places = {}
    for path in world_paths:
        city_id = next((c['city_id'] for c in cities if c['city_id'] in path.parts), None)
        for place in read_json(path).get('places', []):
            identity = public_identity(place)
            if identity:
                places[identity_key(identity)] = (city_id, place)
    for row in observation['results']:
        (city_id, place) = places.get(identity_key(row), (None, {}))
        source = sources.get(city_id, {'events': [], 'error': '未找到同版本城市绑定'})
        project_status(row, place, source['events'], time, source['error'])
