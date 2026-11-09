"""Offline, source-bound annual dates; never runtime Safety notices."""

import copy
import hashlib
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from shared.io import read_json


CALENDAR_ROOT = Path(__file__).resolve().parents[3] / "app" / "assets" / "calendars" / "2026"


def load_calendar(city_id, *, root=CALENDAR_ROOT):
    root = Path(root).resolve()
    path = root / "calendar.json"
    data = read_json(path)
    if data.get("schema_version") != "city_calendar.v1" or data.get("year") != 2026:
        raise ValueError("Safety requires the reviewed 2026 city calendar")
    if city_id not in data["cities"]:
        raise ValueError(f"No reviewed 2026 official calendar for {city_id}")
    city = copy.deepcopy(data["cities"][city_id])
    zone = ZoneInfo(city["timezone"])
    sources = {}
    for key, relative in city["sources"].items():
        source_path = (root / relative).resolve()
        if not source_path.is_relative_to(root):
            raise ValueError("Calendar source must stay in the calendar directory")
        source = read_json(source_path)
        if not all(source.get(k) for k in ("url", "retrieved_at", "body_sha256", "text")):
            raise ValueError("Incomplete official calendar source snapshot")
        sources[key] = {"source_ref": str(source_path), "url": source["url"],
                        "retrieved_at": source["retrieved_at"],
                        "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest()}
    periods = []
    for number, row in enumerate(city["periods"]):
        start, end = date.fromisoformat(row["start"]), date.fromisoformat(row["end"])
        if start.year != 2026 or end.year != 2026 or start > end or row["source"] not in sources:
            raise ValueError("Invalid or unsourced 2026 calendar period")
        kind = row.get("kind", "public_holiday")
        if kind not in {"public_holiday", "regional_event", "working_day"}:
            raise ValueError("Unsupported calendar date annotation")
        periods.append({**row, "kind": kind, "source_ref": f"{path}#/cities/{city_id}/periods/{number}",
                        "source_url": sources[row["source"]]["url"],
                        "active_window": {
                            "start": datetime.combine(start, time.min, zone).isoformat(),
                            "end": datetime.combine(end + timedelta(days=1), time.min, zone).isoformat()}})
    core = city.get("core_holidays", [])
    periods_by_name = {row["name"]: row for row in periods}
    core_periods = [periods_by_name[name] for name in core]
    return {"schema_version": "city_calendar_projection.v1", "year": 2026,
            "city_id": city_id, "timezone": city["timezone"], "coverage": data["coverage"],
            "calendar_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "sources": sources, "periods": periods, "core_holidays": core_periods}


def load_policy(city_id, *, root=CALENDAR_ROOT, point_time=False):
    path = Path(root) / ("safety_point_time_policy.json" if point_time else "safety_time_policy.json")
    value = read_json(path)
    value.update(value.pop("city_overrides", {}).get(city_id, {}))
    expected = "safety_time_policy.v2" if point_time else "safety_time_policy.v1"
    if value.get("schema_version") != expected or value.get("year") != 2026:
        raise ValueError("Safety requires an explicit 2026 time policy")
    required = ["preferred_days_before", "preferred_days_after", "expansion_step_days",
                "protection_attempts_per_cluster"]
    if not point_time:
        required.append("regional_event_target")
    for key in required:
        minimum = 0 if key.startswith("preferred_") else 1
        if type(value.get(key)) is not int or value[key] < minimum:
            raise ValueError(f"Missing or invalid explicit time-policy value: {key}")
    if not point_time:
        times = [time.fromisoformat(value[k]) for k in ("event_start", "activity_start", "activity_end", "event_end")]
        if any(t.tzinfo is not None for t in times) or not times[0] <= times[1] < times[2] <= times[3]:
            raise ValueError("Configured local activity must fit the same-day event envelope")
    if value.get("anchor_date") and date.fromisoformat(value["anchor_date"]).year != 2026:
        raise ValueError("Configured Safety anchor must be in 2026")
    value["source_ref"] = str(path.resolve())
    value["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return value
