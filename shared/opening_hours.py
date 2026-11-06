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
