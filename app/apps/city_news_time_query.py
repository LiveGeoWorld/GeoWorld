from datetime import datetime, timedelta
from app.assets.safety.spacetime import _instant

class FutureQuery(ValueError):
    pass

def query_window(query, now):
    local_now = datetime.fromisoformat(now.replace('Z', '+00:00')) if isinstance(now, str) else now
    current = _instant(local_now.isoformat())
    if not query:
        return (current, current, True)
    query = query.strip()
    if '/' in query:
        (start, end) = map(_instant, query.split('/', 1))
    elif len(query) == 10:
        day = datetime.fromisoformat(query).replace(tzinfo=local_now.tzinfo)
        (start, end) = (_instant(day.isoformat()), _instant((day + timedelta(days=1)).isoformat()))
    else:
        point = _instant(query)
        if point > current:
            raise FutureQuery()
        return (point, point, True)
    if start >= end:
        raise ValueError('Time interval must have positive duration')
    if start > current:
        raise FutureQuery()
    return (start, min(end, current), end > current)

def effective_window(record):
    window = record.get('active_window') or {}
    start = window.get('start') or record.get('activation_time') or record.get('timestamp') or record.get('timestep')
    end = window.get('end') or record.get('end_time')
    return (saved_instant(start) if start else None, saved_instant(end) if end else None)

def saved_instant(value):
    from app.runtime.time_manager import TimeManager
    return _instant(TimeManager._parse_time(value).isoformat())

def matches_effective_window(record, query, now, overrides=()):
    (start, end) = effective_window(record)
    current = _instant(now.isoformat() if hasattr(now, 'isoformat') else now)
    published = record.get('timestamp') or record.get('timestep') or record.get('activation_time')
    if published and saved_instant(published) > current:
        return False
    (qstart, qend, include_end) = query

    def at(point):
        return (start is None or start <= point) and (end is None or point < end) and (not any(((a is None or a <= point) and (b is None or point < b) for (a, b) in overrides)))
    if qstart == qend:
        return at(qstart)
    left = max(qstart, start) if start else qstart
    right = min(qend, end) if end else qend
    remaining = [(left, right)] if left < right else []
    for (a, b) in overrides:
        pieces = []
        for (lo, hi) in remaining:
            if b is not None and b <= lo or (a is not None and a >= hi):
                pieces.append((lo, hi))
                continue
            if a is not None and lo < a:
                pieces.append((lo, min(a, hi)))
            if b is not None and b < hi:
                pieces.append((max(b, lo), hi))
        remaining = pieces
    return bool(remaining) or (include_end and at(qend))
