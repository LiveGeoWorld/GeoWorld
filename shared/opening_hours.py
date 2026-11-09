"""One effective weekly schedule; provenance stays in world-build audit files."""

FIELDS = ("regular_opening_hours", "agent_official_opening_hours", "agent_inferred_opening_hours")


def details_for(place):
    projection = place.get("map_projection") or {}
    return projection.get("google_place_details") or place.get("google_place_details") or {}


def valid_periods(hours):
    if not isinstance(hours, dict) or not isinstance(hours.get("periods"), list) or not hours["periods"]:
        return False
    for period in hours["periods"]:
        if not isinstance(period, dict) or not isinstance(period.get("open"), dict):
            return False
        for key in ("open", "close"):
            if key == "close" and key not in period:
                opened = period["open"]
                if len(hours["periods"]) == 1 and opened.get("day") == 0 and opened.get("hour", 0) == 0 and opened.get("minute", 0) == 0:
                    continue
                return False
            value = period.get(key)
            if not isinstance(value, dict):
                return False
            parts = (value.get("day"), value.get("hour", 0), value.get("minute", 0))
            if any(type(v) is not int for v in parts) or not (0 <= parts[0] < 7 and 0 <= parts[1] < 24 and 0 <= parts[2] < 60):
                return False
        if "close" in period and tuple(period["close"].get(k, 0) for k in ("day", "hour", "minute")) == tuple(period["open"].get(k, 0) for k in ("day", "hour", "minute")):
            return False
    return True


def effective_hours_from_details(details):
    for field in FIELDS:
        value = details.get(field)
        if valid_periods(value):
            return value
    # Preserve legacy display-only Google schedules without claiming computability.
    value = details.get(FIELDS[0])
    return value if isinstance(value, dict) and value else None


def effective_opening_hours(place):
    return effective_hours_from_details(details_for(place))


DAY = 24 * 60
WEEK = 7 * DAY


def _minute(value):
    if not isinstance(value, dict):
        raise ValueError("Missing opening time")
    day, hour, minute = value.get("day"), value.get("hour", 0), value.get("minute", 0)
    if (any(type(v) is not int for v in (day, hour, minute))
            or not 0 <= day <= 6 or not 0 <= hour <= 23 or not 0 <= minute <= 59):
        raise ValueError("Invalid frozen opening time")
    return day * DAY + hour * 60 + minute


def weekly_intervals(place):
    hours = effective_opening_hours(place)
    if not isinstance(hours, dict) or not isinstance(hours.get("periods"), list) or not hours["periods"]:
        return None
    result = []
    try:
        for period in hours["periods"]:
            start = _minute(period["open"])
            if "close" not in period:
                # Google encodes always-open as Sunday 00:00 without a close.
                if len(hours["periods"]) == 1 and start == 0:
                    return [(0, WEEK)]
                return None
            end = _minute(period["close"])
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
    beginning = ((day.weekday() + 1) % 7) * DAY
    rows = []
    for start, end in periods:
        for shift in (-WEEK, 0, WEEK):
            left, right = max(0, start + shift - beginning), min(DAY, end + shift - beginning)
            if left < right:
                rows.append((left, right))
    merged = []
    for start, end in sorted(rows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged
