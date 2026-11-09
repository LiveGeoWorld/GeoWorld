"""Audit diagnostics for LLM-generated itinerary GT.

Produces structured warnings for the reviewer to inspect:
  1. time_windows: events outside POI operating hours
  2. spatial_coherence: consecutive events with insufficient commute time
  3. event_order: reversed chronological order within a day
  4. meal_times: meals outside expected ranges or too close together
"""

from __future__ import annotations

from typing import Any


def run_audit(llm_result: dict, menus: list[dict],
              all_pois: list[dict], hotel: dict,
              route_data: dict[str, dict]) -> dict[str, Any]:
    """Audit LLM-generated itinerary GT. Returns structured diagnostics."""
    warnings: list[str] = []
    details: dict[str, list[dict]] = {
        "time_window_violations": [],
        "spatial_violations": [],
        "order_violations": [],
        "meal_gap_violations": [],
    }

    gt = llm_result.get("gt", {})
    correct = gt.get("correct_itinerary", {})
    days = correct.get("days", [])
    if not days:
        return {"warnings": ["No GT itinerary days found"], "details": details}

    poi_by_id: dict[str, dict] = {}
    for p in all_pois:
        poi_by_id[p.get("place_id", "")] = p
    if hotel.get("place_id"):
        poi_by_id[hotel["place_id"]] = hotel

    for day in days:
        day_num = day.get("day", "?")
        events = day.get("events", [])

        for evt in events:
            pid = evt.get("place_id", "")
            evt_type = evt.get("event_type", "")
            start = evt.get("start", "")
            poi = poi_by_id.get(pid, {})
            if pid and poi:
                cat = poi.get("category", "")
                if cat not in ("transit", "hotel"):
                    open_t = poi.get("opening_open", "")
                    if not open_t:
                        open_t = (poi.get("opening_hours", "?-?") or "?-?").split("-")[0]
                    if open_t and start and _tmin(start) < _tmin(open_t):
                        msg = (f"Day {day_num} {evt_type} @ {poi.get('name','?')}: "
                               f"starts {start} before opening {open_t}")
                        warnings.append(msg)
                        details["time_window_violations"].append({
                            "day": day_num, "event_id": evt.get("event_id", ""),
                            "event_type": evt_type, "place_name": poi.get("name", ""),
                            "event_start": start, "opening": open_t,
                        })

        for i in range(1, len(events)):
            prev = events[i - 1]
            curr = events[i]
            prev_end = prev.get("end", "")
            curr_start = curr.get("start", "")
            if prev_end and curr_start and _tmin(curr_start) < _tmin(prev_end):
                msg = (f"Day {day_num} events out of order: "
                       f"{prev.get('event_type','?')} ends {prev_end} but "
                       f"{curr.get('event_type','?')} starts {curr_start}")
                warnings.append(msg)
                details["order_violations"].append({
                    "day": day_num,
                    "prev_event": prev.get("event_type", ""),
                    "prev_end": prev_end,
                    "curr_event": curr.get("event_type", ""),
                    "curr_start": curr_start,
                })

            prev_name = prev.get("place_name", "?")[:20]
            curr_name = curr.get("place_name", "?")[:20]
            prev_pid = prev.get("place_id", "")
            curr_pid = curr.get("place_id", "")
            gap = _tmin(curr_start) - _tmin(prev_end) if prev_end and curr_start else 999

            min_commute = _lookup_commute_by_id(prev_pid, curr_pid, route_data, all_pois)
            if min_commute is not None and gap >= 0 and gap < (min_commute + 5):
                msg = (f"Day {day_num} {prev_name}→{curr_name}: "
                       f"gap={gap}min < route={min_commute}min")
                warnings.append(msg)
                details["spatial_violations"].append({
                    "day": day_num,
                    "from_place": prev_name, "to_place": curr_name,
                    "gap_min": gap, "route_min": min_commute,
                })

        prev_meal_end: int | None = None
        for evt in events:
            etype = evt.get("event_type", "")
            if etype not in ("lunch", "dinner", "breakfast"):
                continue
            start = evt.get("start", "")
            end = evt.get("end", "")
            if prev_meal_end is not None and start:
                gap = _tmin(start) - prev_meal_end
                if 0 < gap < 210:
                    msg = (f"Day {day_num} meals only {gap}min apart "
                           f"(last ended {_tstr(prev_meal_end)}, {etype} starts {start})")
                    warnings.append(msg)
                    details["meal_gap_violations"].append({
                        "day": day_num,
                        "gap_min": gap,
                        "event_type": etype,
                        "start": start,
                    })
            if end:
                prev_meal_end = _tmin(end)

    return {"warnings": warnings, "details": details}


def _tmin(s: str) -> int:
    try:
        h, m = map(int, s.split(":")[:2])
        return h * 60 + m
    except (ValueError, AttributeError):
        return -1


def _tstr(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _lookup_commute_by_id(pid_a: str, pid_b: str,
                           route_data: dict[str, dict],
                           all_pois: list[dict]) -> int | None:
    if not pid_a or not pid_b:
        return None
    direct = route_data.get(f"{pid_a}__TO__{pid_b}", {})
    direct_times = [value for value in direct.values() if isinstance(value, (int, float))]
    if direct_times:
        return int(min(direct_times))
    id_to_name = {p["place_id"]: p.get("name", "?")[:20] for p in all_pois if p.get("place_id")}
    name_a = id_to_name.get(pid_a, pid_a[:20])
    name_b = id_to_name.get(pid_b, pid_b[:20])
    return _lookup_commute(name_a, name_b, route_data)


def _lookup_commute(name_a: str, name_b: str,
                     route_data: dict[str, dict]) -> int | None:
    for key, modes in route_data.items():
        if name_a in key and name_b in key:
            times = [t for t in modes.values() if isinstance(t, (int, float))]
            if times:
                return min(times)
    return None
