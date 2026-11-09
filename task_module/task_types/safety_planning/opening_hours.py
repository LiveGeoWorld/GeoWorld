"""Frozen weekly opening intervals, with unknown kept distinct from closed."""

from datetime import datetime

from shared.opening_hours import intervals_on_date
from .spacetime import _instant

def open_at(place, task_time):
    """Evaluate the local task instant, never a visit duration or arrival time."""
    _instant(task_time)
    point = datetime.fromisoformat(task_time.replace("Z", "+00:00"))
    intervals = intervals_on_date(place, point.date())
    if intervals is None:
        return None
    minute = point.hour * 60 + point.minute + point.second / 60 + point.microsecond / 60000000
    return any(start <= minute < end for start, end in intervals)
