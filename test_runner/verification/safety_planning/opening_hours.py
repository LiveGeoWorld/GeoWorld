from datetime import datetime
from shared.opening_hours import effective_opening_hours as _regular_opening_hours
from test_runner.verification.safety_planning.spacetime import _instant
DAY = 24 * 60
WEEK = 7 * DAY

def _minute(value):
    if not isinstance(value, dict):
        raise ValueError('Missing opening time')
    (day, hour, minute) = (value.get('day'), value.get('hour', 0), value.get('minute', 0))
    if any((type(v) is not int for v in (day, hour, minute))) or not 0 <= day <= 6 or (not 0 <= hour <= 23) or (not 0 <= minute <= 59):
        raise ValueError('Invalid frozen opening time')
    return day * DAY + hour * 60 + minute

def weekly_intervals(place):
    hours = _regular_opening_hours(place)
    if not isinstance(hours, dict) or not isinstance(hours.get('periods'), list) or (not hours['periods']):
        return None
    result = []
    try:
        for period in hours['periods']:
            start = _minute(period['open'])
            if 'close' not in period:
                if len(hours['periods']) == 1 and start == 0:
                    return [(0, WEEK)]
                return None
            end = _minute(period['close'])
            if end == start:
                return None
            if end < start:
                end += WEEK
            result.append((start, end))
    except (KeyError, TypeError, ValueError):
        return None
    return result

def intervals_on_date(place, day):
    periods = weekly_intervals(place)
    if periods is None:
        return None
    beginning = (day.weekday() + 1) % 7 * DAY
    rows = []
    for (start, end) in periods:
        for shift in (-WEEK, 0, WEEK):
            (left, right) = (max(0, start + shift - beginning), min(DAY, end + shift - beginning))
            if left < right:
                rows.append((left, right))
    merged = []
    for (start, end) in sorted(rows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged

def open_at(place, task_time):
    """Evaluate the local task instant, never a visit duration or arrival time."""
    _instant(task_time)
    point = datetime.fromisoformat(task_time.replace('Z', '+00:00'))
    intervals = intervals_on_date(place, point.date())
    if intervals is None:
        return None
    minute = point.hour * 60 + point.minute + point.second / 60 + point.microsecond / 60000000
    return any((start <= minute < end for (start, end) in intervals))
