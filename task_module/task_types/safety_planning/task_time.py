"""Point-time semantics with read-only support for archived activity windows."""

from .spacetime import TimeWindow, _instant

OCCUPANCY_CONTRACTS = {"safety_occupancy.v1", "safety_occupancy.v2", "safety_occupancy.v3", "safety_occupancy.v4", "safety_occupancy.v5", "safety_occupancy.v6"}


def moment(context):
    return context["task_time"] if "task_time" in context else context["activity_window"]


def time_fields(context):
    key = "task_time" if "task_time" in context else "activity_window"
    return {key: context[key]}


def applies(window, task):
    period = TimeWindow(**window)
    if isinstance(task, str):
        return _instant(period.start) <= _instant(task) < _instant(period.end)
    return period.overlaps(TimeWindow(**task))


def record_applies(record, task):
    if "task_time" in record:
        if isinstance(task, str):
            return _instant(record["task_time"]) == _instant(task)
        return applies(task, record["task_time"])
    return applies(record["active_window"], task)
