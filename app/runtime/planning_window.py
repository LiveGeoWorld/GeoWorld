"""Resolve decision-window boundaries without treating a GT schedule as execution."""
from datetime import datetime, timedelta


def itinerary_deadline(context: dict, gt: dict) -> str:
    anchor = datetime.fromisoformat(context["instruction_time"].replace("Z", "+00:00"))
    day_start = anchor.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = set()
    last_day = gt.get("num_days")
    for requirement in gt.get("public_requirements") or []:
        end = (requirement.get("activity_group") or {}).get("end_anchor") or {}
        if requirement.get("day") == last_day and end.get("event_type") == "departure" and end.get("time"):
            candidates.add(str(end["time"]))
    if not candidates:
        dag = gt.get("construction_dag") or (gt.get("scenario") or {}).get("construction_dag") or {}
        for day in dag.get("days") or []:
            if day.get("day") != last_day:
                continue
            for node in day.get("timeline") or []:
                if node.get("block_kind") == "fixed_event" and node.get("event_type") == "departure" and node.get("start"):
                    candidates.add(str(node["start"]))
    if len(candidates) != 1 or type(last_day) is not int or last_day < 1:
        raise ValueError("itinerary_planning_deadline_unresolved: explicit final departure time required")
    from app.assets.clock_precision import clock_seconds
    return (day_start + timedelta(days=last_day - 1, seconds=clock_seconds(candidates.pop()))).isoformat()


def decision_deadline(context: dict, source_kind: str, gt: dict) -> str:
    if source_kind == "itinerary":
        return itinerary_deadline(context, gt)
    return (context.get("planning_window") or {}).get("activity_anchor_time") or context.get("instruction_time", "")
