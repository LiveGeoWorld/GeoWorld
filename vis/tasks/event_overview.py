import json
from datetime import datetime
from shapely.geometry import mapping
from vis.tasks.service_review import load_review_events
from vis.tasks.dynamic_review import load_dynamic_review
'Read-only cross-App event map with an optional author protection overlay.'

def overview(root, world, cities, city_id='', mode='static', time='', include_protection=False):
    if mode not in ('static', 'dynamic'):
        raise ValueError('无效事件模式')
    selected = [c for c in cities if c['city_id'] == city_id]
    if city_id and (not selected):
        raise ValueError('城市不属于当前世界')

    def instant(value):
        point = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if point.tzinfo is None:
            raise ValueError('查看时间需要时区')
        return point
    at = instant(time) if time else None
    (rows, protection_rows, warnings) = ([], [], [])
    if selected:
        sources = load_review_events(root, world, selected) if mode == 'static' else load_dynamic_review(root, world, selected)
        source = sources[city_id]
        if source.get('error'):
            warnings.append(source['error'])
        for (event, area) in source['events']:
            if 'review_patch' in event:
                continue
            rows.append({'app': event.get('app', 'CityNewsApp'), 'title': event['title'], 'description': event.get('description', ''), 'active_window': event['active_window'], 'geometry': mapping(area)})
        rows.extend(source.get('overview', []))
        if include_protection:
            for protection in source.get('protections', []):
                protection_rows.append({'kind': 'protection', 'task_id': protection['task_id'], 'candidate_ref': protection.get('candidate_ref', ''), 'dependency_kind': protection.get('dependency_kind', 'place'), 'source_refs': protection.get('source_refs', []), 'scope_ref': protection.get('scope_ref', ''), 'active_window': protection.get('active_window'), 'geometry': mapping(protection['geometry'])})
    all_rows = rows + protection_rows
    windows = {json.dumps(row['active_window'], sort_keys=True): row['active_window'] for row in all_rows}
    unique = {}
    for row in all_rows:
        window = row['active_window']
        if at and (instant(window['start']) > at or (window.get('end') and at >= instant(window['end']))):
            continue
        unique[json.dumps(row, sort_keys=True, ensure_ascii=False)] = row
    filtered = sorted(unique.values(), key=lambda r: (r['active_window']['start'], r.get('title', r.get('task_id', ''))))
    rows = [row for row in filtered if row.get('kind') != 'protection']
    protection_rows = [row for row in filtered if row.get('kind') == 'protection']
    return {'world_id': world, 'mode': mode, 'city_id': city_id, 'time': time, 'cities': [{'city_id': c['city_id'], 'name': c.get('city_name', c['city_id']), 'version': c['active_version']} for c in cities], 'events': rows, 'protections': protection_rows if include_protection else [], 'available_windows': list(windows.values()), 'warnings': warnings}
