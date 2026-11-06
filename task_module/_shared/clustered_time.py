"""Source-first date and clock selection, independent of task construction.

Callers supply resolved source dates and available local minutes. This module
does not infer dates from prose or interpret missing business-hours records.
"""

from datetime import date, datetime, time, timedelta, timezone
from collections.abc import Callable, Iterable
from zoneinfo import ZoneInfo


def remap_local_date(stamp: str, target_date: date, timezone_name: str) -> str:
    """Move an absolute timestamp to ``target_date`` while preserving local time.

    Date assignment must not silently change the task's clock.  The round-trip
    check also rejects a target date whose local clock time does not exist at a
    DST transition.
    """
    original = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if original.tzinfo is None:
        raise ValueError("Task timestamp must carry a timezone")
    zone = ZoneInfo(timezone_name)
    local = original.astimezone(zone)
    candidate = datetime.combine(target_date, local.timetz().replace(tzinfo=None), zone)
    if candidate.astimezone(timezone.utc).astimezone(zone) != candidate:
        raise ValueError("Candidate date contains a nonexistent local clock time")
    return candidate.isoformat()


def overlapping_events(
    events: Iterable[dict],
    *,
    candidate_start: str,
    candidate_end: str | None = None,
    scope_matches: Callable[[dict], bool] | None = None,
) -> list[dict]:
    """Return structured events active in a task dependency scope.

    The caller supplies the spatial/dependency relation through ``scope_matches``;
    this helper only applies the canonical half-open time-window rule.  Events
    with an absent or invalid end are reported as conflicts rather than treated
    as already finished.
    """
    start = datetime.fromisoformat(candidate_start.replace("Z", "+00:00"))
    point_query = candidate_end is None
    end = datetime.fromisoformat((candidate_end or candidate_start).replace("Z", "+00:00"))
    if start.tzinfo is None or end.tzinfo is None or end < start:
        raise ValueError("Candidate interval must contain ordered timezone-aware timestamps")
    matches: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if scope_matches is not None and not scope_matches(event):
            continue
        window = event.get("active_window")
        if not isinstance(window, dict) or not window.get("start"):
            continue
        event_start = datetime.fromisoformat(str(window["start"]).replace("Z", "+00:00"))
        raw_end = window.get("end")
        event_end = (datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
                     if raw_end else None)
        if event_start.tzinfo is None or (event_end is not None and event_end.tzinfo is None):
            raise ValueError("Event active_window must be timezone-aware")
        if point_query:
            if event_start <= start and (event_end is None or start < event_end):
                matches.append(event)
        elif event_end is None:
            if event_start < end:
                matches.append(event)
        elif event_start < end and start < event_end:
            matches.append(event)
    return matches


def holiday_neighbor_dates(calendar):
    """Visit all preceding boundaries, then following boundaries, per distance."""
    year = calendar["year"]
    first, last = date(year, 1, 1), date(year, 12, 31)
    periods = [(date.fromisoformat(row["start"]), date.fromisoformat(row["end"]))
               for row in calendar["core_holidays"]]
    if not periods or any(not first <= start <= end <= last for start, end in periods):
        raise ValueError("Core holiday intervals must belong to the calendar year")
    seen = set()
    for distance in range(1, (last - first).days + 1):
        for before in (True, False):
            for start, end in periods:
                day = start - timedelta(days=distance) if before else end + timedelta(days=distance)
                if first <= day <= last and day not in seen:
                    seen.add(day)
                    yield day


def assign_clustered_time(*, calendar, previous_times, source_dates,
                          available_minutes, rng, preferred_source_date=None,
                          required_minute=None, required_weekday=None,
                          candidate_checker=None):
    """Return (point context, private audit) without recording an allocation.

    source_dates is explicitly None only when no source date restriction applies;
    otherwise it contains resolved allowed dates (including seasonal ranges).
    available_minutes(day) supplies source-backed local minutes, or None if unknown.
    previous_times must already be scoped by the caller to the applicable world/city.
    """
    year = calendar["year"]
    zone = ZoneInfo(calendar["timezone"])
    allowed = None if source_dates is None else set(source_dates)
    if allowed is not None and any(type(day) is not date or day.year != year for day in allowed):
        raise ValueError("Source dates must belong to the calendar year")
    if preferred_source_date is not None and (
        allowed is None or preferred_source_date not in allowed
    ):
        raise ValueError("Preferred source date must be an allowed source date")
    if required_minute is not None and (
        type(required_minute) is not int or not 0 <= required_minute < 1440
    ):
        raise ValueError("Required local minute must be in [0, 1440)")
    if required_weekday is not None and (
        type(required_weekday) is not int or not 0 <= required_weekday < 7
    ):
        raise ValueError("Required local weekday must be in [0, 7)")
    previous = {}
    for value in previous_times:
        point = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if point.tzinfo is None:
            raise ValueError("Previous task times must carry a timezone")
        point = point.astimezone(zone)
        if point.year == year:
            previous.setdefault(point.date(), []).append(point)

    dates = []
    if preferred_source_date is not None:
        dates.append((preferred_source_date, "source_preference"))
    dates.extend((day, "reused_date") for day in previous)
    dates.extend((day, "holiday_neighbor") for day in holiday_neighbor_dates(calendar))
    if allowed is not None:
        # A source date must remain eligible even if it is not a holiday neighbor.
        dates.extend((day, "source_date") for day in sorted(allowed))
    seen, attempts = set(), []
    for day, origin in dates:
        if day in seen or (allowed is not None and day not in allowed):
            continue
        if required_weekday is not None and day.weekday() != required_weekday:
            continue
        seen.add(day)
        minutes = available_minutes(day)
        if minutes is None:
            raise ValueError(f"Unresolved source clock availability for {day}")
        minutes = sorted(set(minutes))
        if any(type(value) is not int or not 0 <= value < 1440 for value in minutes):
            raise ValueError("Available local minutes must be integers in [0, 1440)")
        if required_minute is not None:
            minutes = [value for value in minutes if value == required_minute]
        # Exclude nonexistent local times at a daylight-saving clock transition.
        points = {}
        for minute in minutes:
            point = datetime.combine(day, time(minute // 60, minute % 60), zone)
            if point.astimezone(timezone.utc).astimezone(zone) == point:
                points[minute] = point
        reused = next((point for point in previous.get(day, [])
                       if point.hour * 60 + point.minute in points
                       and point.second == 0 and point.microsecond == 0), None)
        if not points:
            attempts.append({"date": day.isoformat(), "reason": "no_available_clock"})
            continue
        point = reused if reused is not None else points[rng.choice(list(points))]
        candidate = {"timezone": calendar["timezone"], "instruction_time": point.isoformat(),
                     "episode_start": point.isoformat(), "source": "clustered_source_time.v1"}
        if candidate_checker is not None:
            checked = candidate_checker(candidate)
            if isinstance(checked, dict) and checked.get("status") == "rejected":
                attempts.append({"date": day.isoformat(), "reason": checked.get("reason", "candidate_rejected")})
                continue
            if isinstance(checked, dict) and checked.get("status") == "unresolved":
                # An unresolved event scope/geometry is not evidence of a
                # conflict-free date.  Do not auto-pass it; try the next
                # clustered candidate and let the caller report that no
                # auditable date was available when all candidates remain
                # unresolved.
                attempts.append({"date": day.isoformat(),
                                 "reason": checked.get("reason", "candidate_unresolved")})
                continue
        return (candidate, {"date_origin": origin, "clock_origin": "reused" if reused else "seeded_available",
                 "calendar_sha256": calendar.get("calendar_sha256"),
                 "skipped_dates": attempts})
    raise ValueError("No source-compatible date and clock in the calendar year")
