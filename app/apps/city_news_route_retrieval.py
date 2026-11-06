import math
from geographiclib.geodesic import Geodesic
from shapely.geometry import Point, mapping
from shapely.ops import transform
from app.assets.safety.occupancy import scope_geometry
RADIUS_M = 2200

def route_notice_geometry(entry, routes):
    """Resolve the event footprint, preserving authored local-step boundaries."""
    route_id = str((entry.get('scope') or {}).get('id') or '')
    route = routes.get(route_id)
    if route is None:
        raise ValueError(f'city_news_route_retrieval_missing_route:{route_id}')
    anchor = {'geometry': entry.get('geometry'), 'source_refs': []}
    definition = entry.get('spatial_scope') or {}
    if not anchor['geometry'] and definition:
        indices = definition.get('step_indices')
        if indices is None and 'step_index' in definition:
            indices = [definition['step_index']]
        if not isinstance(indices, list) or not indices:
            raise ValueError('city_news_route_retrieval_missing_local_geometry')
        anchor['source_refs'] = [f'route:{route_id}/steps/{i}' for i in indices]
    elif not anchor['geometry']:
        anchor['source_refs'] = [f'route:{route_id}']
    evidence = {'source_ref': f'route:{route_id}', 'route': route.get('route') or {}, 'steps': [{'source_ref': f'route:{route_id}/steps/{i}', 'facts': step} for (i, step) in enumerate((route.get('route') or {}).get('steps') or [])]}
    shape = scope_geometry(anchor, {'route_evidence': [evidence]})
    if shape.geom_type not in {'LineString', 'MultiLineString'}:
        raise ValueError('city_news_route_retrieval_requires_line')
    return mapping(shape)

def route_districts(entry, routes, places, districts):
    from shapely.geometry import shape as read_shape
    shape = read_shape(route_notice_geometry(entry, routes))
    route_id = str((entry.get('scope') or {}).get('id') or '')
    route = routes[route_id]
    origin = route.get('origin_place_id')
    city_ids = {str(p.get('city_id') or p.get('_city_id')) for p in places if p.get('place_id') == origin and (p.get('city_id') or p.get('_city_id'))}
    if not city_ids:
        destination = route.get('dest_place_id')
        city_ids = {str(p.get('city_id') or p.get('_city_id')) for p in places if p.get('place_id') == destination and (p.get('city_id') or p.get('_city_id'))}
    if not city_ids:
        city_ids = {str(d['city_id']) for d in districts if d.get('city_id')}
    if len(city_ids) != 1:
        raise ValueError('city_news_route_retrieval_city_unresolved')
    city_id = next(iter(city_ids))
    distances = {}
    for district in districts:
        g = district.get('geometry') or {}
        c = g.get('data') or {}
        if district.get('city_id') != city_id or g.get('kind') != 'point' or (not district.get('district_id')):
            continue
        if not all((type(c.get(k)) in (int, float) and math.isfinite(c[k]) for k in ('lat', 'lng'))):
            continue

        def project(x, y, z=None):

            def one(lon, lat):
                value = Geodesic.WGS84.Inverse(c['lat'], c['lng'], lat, lon)
                angle = math.radians(value['azi1'])
                return (value['s12'] * math.sin(angle), value['s12'] * math.cos(angle))
            if hasattr(x, '__iter__'):
                points = [one(a, b) for (a, b) in zip(x, y)]
                return (tuple((p[0] for p in points)), tuple((p[1] for p in points)))
            return one(x, y)
        distances[str(district['district_id'])] = transform(project, shape).distance(Point(0, 0))
    if not distances:
        raise ValueError('city_news_route_retrieval_district_points_missing')
    matched = {key for (key, distance) in distances.items() if distance <= RADIUS_M}
    return matched or {min(distances, key=distances.get)}
