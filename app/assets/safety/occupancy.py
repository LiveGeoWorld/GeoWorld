from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import unary_union
from shared.maps_client import decode_polyline
from app.assets.safety.map_scene import coordinates, route_paths

def geometry(value):
    if not isinstance(value, dict):
        raise ValueError('Occupancy requires explicit geometry')
    if 'type' in value:
        result = shape(value)
    else:
        (kind, data) = (value.get('kind'), value.get('data', {}))
        if kind == 'point':
            result = Point(coordinates([data])[0])
        elif kind in {'polygon', 'multipolygon'}:
            result = shape({'type': 'Polygon' if kind == 'polygon' else 'MultiPolygon', **data})
        else:
            raise ValueError('Unsupported occupancy geometry')
    if result.is_empty or not result.is_valid:
        raise ValueError('Invalid or empty occupancy geometry')
    if result.geom_type not in {'Point', 'LineString', 'MultiLineString', 'Polygon', 'MultiPolygon'}:
        raise ValueError('Unsupported occupancy geometry type')
    (x0, y0, x1, y1) = result.bounds
    if not (-180 <= x0 <= x1 <= 180 and -90 <= y0 <= y1 <= 90):
        raise ValueError('Occupancy coordinates outside bounds')
    return result

def scope_geometry(anchor, context):
    raw = anchor.get('geometry')
    if raw:
        result = geometry(raw)
        if result.geom_type == 'Point' and any(('/districts/' in r for r in anchor.get('source_refs', []))):
            raise ValueError('Representative district point is not an impact boundary')
        return result
    refs = set(anchor.get('source_refs', []))
    parts = []
    for route in context.get('route_evidence', []):
        if route['source_ref'] in refs:
            parts.extend((LineString(p) for p in route_paths(route['route'])))
        for step in route.get('steps', []):
            encoded = step.get('facts', {}).get('polyline', {}).get('encodedPolyline')
            if step['source_ref'] in refs and encoded:
                points = coordinates(decode_polyline(encoded))
                if len(points) >= 2:
                    parts.append(LineString(points))
    if not parts:
        raise ValueError('Scope lacks a resolvable cached shape')
    return geometry(mapping(unary_union(parts)))
