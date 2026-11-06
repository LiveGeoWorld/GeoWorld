import math
from shared.maps_client import decode_polyline

def coordinates(values):
    result = []
    for point in values:
        (lat, lng) = (point.get('lat'), point.get('lng')) if isinstance(point, dict) else point
        if not all((type(v) in (int, float) and math.isfinite(v) for v in (lat, lng))):
            raise ValueError('Invalid map coordinate')
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError('Map coordinate outside bounds')
        result.append((lng, lat))
    return result

def route_paths(route):
    decoded = coordinates(decode_polyline(route['encoded_polyline'])) if route.get('encoded_polyline') else []
    points = coordinates(route['polyline_points']) if route.get('polyline_points') else []
    paths = [points] if points else []
    if decoded and decoded != points:
        paths.append(decoded)
    return [p for p in paths if len(p) >= 2]
