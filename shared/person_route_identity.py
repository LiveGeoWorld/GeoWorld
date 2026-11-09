"""Identity checks for legacy person endpoints in frozen route records."""
import math


def person_route_ids(character_id: str) -> tuple[str, str]:
    bare = character_id.removeprefix("character:")
    return bare, "character:" + bare


def route_endpoint_matches(row, side, location, name=""):
    """Check the recorded endpoint, using the cache's six-decimal precision."""
    try:
        recorded = tuple(float(row[f"{side}_{axis}"]) for axis in ("lat", "lng"))
        requested = tuple(float(location[axis]) for axis in ("lat", "lng"))
    except (KeyError, TypeError, ValueError):
        return False
    if not all(math.isfinite(value) for value in (*recorded, *requested)):
        return False
    if tuple(round(value, 6) for value in recorded) != tuple(round(value, 6) for value in requested):
        return False
    actual = " ".join(str(row.get(f"{side}_name") or "").casefold().split())
    name = str(name or "").strip()
    expected = {" ".join(value.casefold().split()) for value in (name, name[:15])}
    return not name or actual in expected
