"""Resolve explicit public route submissions against observed route facts only.

No GT route selection, shortest-path search, or free-text answer completion.
The GT is used only to name the required places for subsequent constraint checks.
"""
from __future__ import annotations
import json
import math
import unicodedata
from app.apps.map_app import MapAppSandbox

def _name(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())

def _location(value):
    if not isinstance(value, dict):
        return None
    try:
        (lat, lng) = (float(value['lat']), float(value['lng']))
        if not (math.isfinite(lat) and math.isfinite(lng)):
            return None
        return (round(lat, 6), round(lng, 6))
    except (KeyError, ValueError, TypeError):
        return None

def _identity(value):
    if not isinstance(value, dict):
        return None
    name = _name(value.get('name') or value.get('place_name'))
    location = _location(value.get('location'))
    return (name, location) if name and location is not None else None

def _mode(value):
    mode = MapAppSandbox._normalize_route_mode(value)
    return mode if mode in {'WALK', 'DRIVE', 'TRANSIT'} else None

def _submitted_legs(answer):
    """Accept explicit leg lists and ordered public-place lists; never read explanation."""
    plan = answer.get('plan')
    if not isinstance(plan, dict):
        return None
    default_mode = plan.get('mode') or answer.get('mode')
    for key in ('legs', 'segments', 'route_segments'):
        if key not in plan:
            continue
        if not isinstance(plan[key], list):
            return None
        legs = []
        for item in plan[key]:
            if not isinstance(item, dict):
                return None
            origin = item.get('origin', item.get('from'))
            destination = item.get('destination', item.get('to'))
            if origin is None and destination is None and (item.get('at') or item.get('stop')):
                continue
            if origin is None or destination is None:
                return None
            legs.append((origin, destination, item.get('mode') or default_mode))
        return legs or None
    if 'pickup_order' in plan:
        points = plan['pickup_order']
        if not isinstance(points, list) or len(points) < 2:
            return None
        return [(a, b, default_mode) for (a, b) in zip(points, points[1:])]
    if isinstance(plan.get('days'), list):
        points = []
        for day in plan['days']:
            if not isinstance(day, dict) or not isinstance(day.get('events'), list):
                return None
            for event in day['events']:
                if not isinstance(event, dict):
                    return None
                if str(event.get('event_type', '')).casefold() in {'travel', 'transit'}:
                    continue
                name = event.get('place_name') or event.get('name')
                if not name:
                    return None
                point = {'name': name}
                if event.get('location') is not None:
                    point['location'] = event['location']
                if not points or points[-1] != point:
                    points.append(point)
        return [(a, b, default_mode) for (a, b) in zip(points, points[1:])] if len(points) > 1 else None
    origin = plan.get('origin', plan.get('start'))
    destination = plan.get('destination', plan.get('end'))
    if origin is not None and destination is not None:
        return [(origin, destination, default_mode)]
    return None

def public_route_plan(result, gt_data):
    answer = result.final_answer
    if not isinstance(answer, dict):
        return None
    legs = _submitted_legs(answer)
    if not legs:
        return None
    observed = []
    identities = set()
    for call in result.trace:
        if call.tool not in {'MapApp__query_routes', 'query_routes'}:
            continue
        origin = _identity(call.args.get('origin'))
        destination = _identity(call.args.get('destination'))
        if origin is None or destination is None or (not isinstance(call.result, dict)):
            continue
        identities.update((origin, destination))
        for route in call.result.get('routes', []):
            if not isinstance(route, dict):
                continue
            observed.append((origin, destination, route))

    def resolve(value):
        if isinstance(value, str):
            (name, location) = (_name(value), None)
        elif isinstance(value, dict):
            name = _name(value.get('name') or value.get('place_name'))
            location = _location(value.get('location'))
            if 'location' in value and location is None:
                return None
        else:
            return None
        matches = [point for point in identities if point[0] == name and (location is None or location == point[1])]
        return matches[0] if len(matches) == 1 else None

    def private_identity(point):
        matches = [p for p in gt_data.get('core_places', []) if _name(p.get('name')) == point[0] and (not p.get('location') or _location(p['location']) == point[1])]
        if len(matches) == 1 and (matches[0].get('location') or sum((p[0] == point[0] for p in identities)) == 1):
            return matches[0].get('place_id', '')
        return 'observed:' + json.dumps(point, ensure_ascii=False)
    (route_ids, directory) = ([], {})
    for (origin_value, dest_value, mode_value) in legs:
        (origin, destination) = (resolve(origin_value), resolve(dest_value))
        mode = _mode(mode_value)
        if origin is None or destination is None or mode is None:
            return None
        if origin == destination:
            continue
        candidates = {}
        for (ro, rd, route) in observed:
            if ro != origin or rd != destination or _mode(route.get('mode')) != mode:
                continue
            fact = {k: v for (k, v) in route.items() if k not in {'steps', 'polyline_points', 'encoded_polyline'}}
            candidates[json.dumps(fact, sort_keys=True, ensure_ascii=False)] = route
        if len(candidates) != 1:
            return None
        route = next(iter(candidates.values()))
        rid = f'observed_leg_{len(route_ids)}'
        directory[rid] = {'origin_id': private_identity(origin), 'dest_id': private_identity(destination), 'origin_name': origin[0], 'dest_name': destination[0], 'mode': mode, **{k: route[k] for k in ('duration_s', 'distance_m', 'walking_distance_m', 'available') if k in route}}
        route_ids.append(rid)
    if not route_ids:
        return None
    return {'route_ids': route_ids, 'rd': directory, 'core': {p.get('role'): p.get('place_id') for p in gt_data.get('core_places', [])}, 'wp_ids': [p.get('place_id') for p in gt_data.get('core_places', []) if p.get('role') == 'waypoint'], 'constraints': gt_data.get('constraints', {})}
