"""Incremental, stateful DAG construction for itinerary tasks."""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from typing import Any

from task_module.task_types.itinerary.construction_agents import ItineraryConstructionAgents
from task_module.task_types.itinerary.sampler import ItinerarySampler, WEEKDAY_NAMES
from task_module.task_types.itinerary.clock_precision import clock_minutes, format_minutes


class DagBuildError(RuntimeError):
    """Raised when a continuous, fact-grounded DAG cannot be completed."""


@dataclass
class DagBuildResult:
    menus: list[dict[str, Any]]
    all_pois: list[dict[str, Any]]
    route_data: dict[str, dict[str, int]]
    route_queries: list[dict[str, Any]]
    candidate_attempts: list[dict[str, Any]]
    construction_dag: dict[str, Any]


def build_itinerary_dag(
    *,
    sampler: ItinerarySampler,
    agents: ItineraryConstructionAgents,
    character: dict[str, Any],
    hotel: dict[str, Any],
    transit_hubs: list[dict[str, Any]],
    num_days: int,
    scenario: dict[str, Any],
    maps_client: Any = None,
) -> DagBuildResult:
    """Advance one validated node at a time; never request a whole itinerary."""
    if not transit_hubs:
        raise DagBuildError("an arrival hub is required")
    daily_scenarios = list(scenario.get("daily_scenarios") or [])
    if len(daily_scenarios) != num_days:
        raise DagBuildError("scenario sequence does not match trip duration")
    start_weekday_index = int(scenario.get("start_weekday_index", 0)) % 7

    arrival = transit_hubs[0]
    departure = transit_hubs[-1]
    all_pois: list[dict[str, Any]] = [hotel, *transit_hubs]
    route_data: dict[str, dict[str, int]] = {}
    route_queries: list[dict[str, Any]] = []
    candidate_attempts: list[dict[str, Any]] = []
    menus: list[dict[str, Any]] = []
    dag_days: list[dict[str, Any]] = []
    selected_place_ids: set[str] = {str(hotel.get("place_id") or "")}
    routing_candidates = sampler.routing_activity_candidates()
    available_types = sorted({
        str(item.get("service_type") or "")
        for item in routing_candidates
    })
    if not available_types:
        raise DagBuildError("no ordinary POI can form a public activity requirement")

    lodging = {
        "place_id": hotel.get("place_id", ""),
        "name": hotel.get("name", ""),
        "status": "reserved",
    }
    for day in range(1, num_days + 1):
        weekday_index = (start_weekday_index + day - 1) % 7
        fixed = _fixed_events(day, num_days, hotel, arrival, departure, scenario)
        timeline: list[dict[str, Any]] = []
        slots: list[dict[str, Any]] = []
        current_place = arrival if day == 1 else hotel
        cursor = 10 * 60 if day == 1 else 7 * 60 + 30
        day_end = 22 * 60
        boundaries = [*fixed]
        if day < num_days:
            boundaries.append(_boundary_event(day, hotel, day_end))
        elif not any(event.get("event_type") == "departure" for event in boundaries):
            boundaries.append(_boundary_event(day, hotel, day_end))
        boundaries.sort(key=lambda item: _minutes(item.get("earliest", "00:00")))

        for fixed_event in boundaries:
            boundary_start = _minutes(fixed_event.get("earliest", "00:00"))
            target_place = _event_place(fixed_event, hotel, arrival, departure)
            cursor, current_place = _fill_until_boundary(
                sampler=sampler,
                agents=agents,
                character=character,
                scenario_family=daily_scenarios[day - 1],
                weekday_index=weekday_index,
                day=day,
                cursor=cursor,
                current_place=current_place,
                target_place=target_place,
                boundary_start=boundary_start,
                hotel=hotel,
                lodging=lodging,
                available_types=available_types,
                selected_place_ids=selected_place_ids,
                timeline=timeline,
                slots=slots,
                all_pois=all_pois,
                route_data=route_data,
                route_queries=route_queries,
                candidate_attempts=candidate_attempts,
                maps_client=maps_client,
            )
            if cursor > boundary_start:
                raise DagBuildError(f"day {day} crossed fixed boundary {fixed_event.get('event_type')}")
            while cursor < boundary_start:
                _append_personal_block(
                    agents, _state(day, cursor, current_place, timeline, daily_scenarios[day - 1], lodging, fixed_event, character),
                    cursor, boundary_start, current_place, timeline, slots,
                )
                cursor = _minutes(timeline[-1]['end'])

            if fixed_event.get("event_type") == "day_end":
                continue
            duration = int(fixed_event.get("duration_min", 30))
            timeline.append({
                "node_id": fixed_event.get("event_id"),
                "block_kind": "fixed_event",
                "event_type": fixed_event.get("event_type"),
                "place_id": fixed_event.get("place_id", ""),
                "place_name": fixed_event.get("place_name", ""),
                "start": _format(cursor),
                "end": _format(cursor + duration),
            })
            cursor += duration
            current_place = target_place
            if fixed_event.get("event_type") == "hotel_checkin":
                lodging["status"] = "checked_in"
            elif fixed_event.get("event_type") == "checkout":
                lodging["status"] = "checked_out"

        menus.append({
            "day": day,
            "fixed_events": fixed,
            "required_slots": slots,
            "optional_slots": [],
        })
        if day < num_days:
            overnight_state = _state(
                day, day_end, hotel, timeline, daily_scenarios[day - 1], lodging, None, character,
            )
            overnight = agents.complete_overnight(overnight_state, "07:30")
            if not overnight:
                raise DagBuildError("overnight-completion Agent failed to provide a valid decision")
            timeline.append({
                "node_id": f"day{day}_overnight",
                "block_kind": "personal_activity",
                "event_type": "overnight_personal_activity",
                "activity_name": str(overnight["activity_name"]),
                "place_id": hotel.get("place_id", ""),
                "place_name": hotel.get("name", ""),
                "start": "22:00",
                "end": f"day{day + 1} 07:30",
            })
        dag_days.append({
            "day": day,
            "weekday_index": weekday_index,
            "weekday": WEEKDAY_NAMES[weekday_index],
            "scenario_family": daily_scenarios[day - 1],
            "timeline": timeline,
        })

    return DagBuildResult(
        menus=menus,
        all_pois=_unique_pois(all_pois),
        route_data=route_data,
        route_queries=route_queries,
        candidate_attempts=candidate_attempts,
        construction_dag={
            "schema_version": "geoword.itinerary.incremental_dag.v1",
            "daily_scenarios": daily_scenarios,
            "start_weekday_index": start_weekday_index,
            "start_weekday": WEEKDAY_NAMES[start_weekday_index],
            "lodging": lodging,
            "days": dag_days,
            "agent_decisions": agents.decisions,
        },
    )


def _fill_until_boundary(
    *,
    sampler: ItinerarySampler,
    agents: ItineraryConstructionAgents,
    character: dict[str, Any],
    scenario_family: str,
    weekday_index: int,
    day: int,
    cursor: int,
    current_place: dict[str, Any],
    target_place: dict[str, Any],
    boundary_start: int,
    hotel: dict[str, Any],
    lodging: dict[str, Any],
    available_types: list[str],
    selected_place_ids: set[str],
    timeline: list[dict[str, Any]],
    slots: list[dict[str, Any]],
    all_pois: list[dict[str, Any]],
    route_data: dict[str, dict[str, int]],
    route_queries: list[dict[str, Any]],
    candidate_attempts: list[dict[str, Any]],
    maps_client: Any,
) -> tuple[int, dict[str, Any]]:
    while cursor < boundary_start:
        target_route = _route_minutes(
            sampler, current_place, target_place, route_data, route_queries, maps_client,
            preferred_mode=str(character.get("preferred_transit") or ""),
        )
        if current_place.get("place_id") != target_place.get("place_id") and not target_route:
            raise DagBuildError(f"day {day} cannot reach the next fixed boundary")
        reserve = target_route[0] if target_route else 0
        latest_activity_end = boundary_start - reserve
        if latest_activity_end <= cursor:
            if reserve:
                cursor = _append_transit(current_place, target_place, cursor, target_route, timeline)
                current_place = target_place
            return cursor, current_place

        state = _state(
            day, cursor, current_place, timeline, scenario_family, lodging,
            {"event_type": "boundary", "earliest": _format(boundary_start), "place_name": target_place.get("name", "")},
            character,
        )
        direction = agents.choose_direction(state, available_types)
        service_type = str(direction.get("decision") or "")
        if service_type == "personal_activity":
            _append_personal_block(
                agents, state, cursor, latest_activity_end, current_place, timeline, slots,
            )
            cursor = _minutes(timeline[-1]["end"])
            continue

        poi = sampler.sample_poi_for_service_type(
            service_type,
            excluded_place_ids=selected_place_ids,
            weekday_index=weekday_index,
        )
        if not poi:
            _append_personal_block(
                agents, state, cursor, latest_activity_end, current_place, timeline, slots,
            )
            cursor = _minutes(timeline[-1]["end"])
            continue
        route_query_start = len(route_queries)
        attempt_record = _candidate_attempt_record(
            sequence=len(candidate_attempts) + 1,
            day=day,
            weekday_index=weekday_index,
            cursor=cursor,
            current_place=current_place,
            target_place=target_place,
            boundary_start=boundary_start,
            service_type=service_type,
            poi=poi,
        )
        onward_route = _route_minutes(
            sampler, poi, target_place, route_data, route_queries, maps_client,
            preferred_mode=str(character.get("preferred_transit") or ""),
        )
        if poi.get("place_id") != target_place.get("place_id") and not onward_route:
            _finish_candidate_attempt(
                candidate_attempts, attempt_record, route_queries, route_query_start,
                status="rejected", reason="no_route_to_next_boundary",
            )
            selected_place_ids.add(str(poi.get("place_id") or ""))
            continue
        latest_activity_end = boundary_start - (onward_route[0] if onward_route else 0)
        route = _route_minutes(
            sampler, current_place, poi, route_data, route_queries, maps_client,
            preferred_mode=str(character.get("preferred_transit") or ""),
        )
        if current_place.get("place_id") != poi.get("place_id") and not route:
            _finish_candidate_attempt(
                candidate_attempts, attempt_record, route_queries, route_query_start,
                status="rejected", reason="no_route_from_current_location",
            )
            selected_place_ids.add(str(poi.get("place_id") or ""))
            continue
        arrival_cursor = cursor + (route[0] if route else 0)
        factual_window = _factual_activity_window(poi, arrival_cursor, latest_activity_end)
        if factual_window is None:
            _finish_candidate_attempt(
                candidate_attempts, attempt_record, route_queries, route_query_start,
                status="rejected", reason="no_open_interval_in_reachable_window",
                arrival_time=_format(arrival_cursor),
                latest_activity_end=_format(latest_activity_end),
            )
            selected_place_ids.add(str(poi.get("place_id") or ""))
            continue
        transition = agents.complete_transition({
            'current_time': _format(cursor),
            'current_location': {'name': current_place.get('name', '')},
            'traveler': {key: character.get(key) for key in ('name', 'age', 'occupation', 'preferences')},
            'scenario': scenario_family,
            'selected_poi': {'name': poi.get('name', ''), 'service_types': poi.get('required_service_types') or [service_type]},
            'opening_intervals': poi.get('opening_intervals') or [],
            'route': {'route_ref': 'selected_route', 'duration_minutes': route[0] if route else 0,
                      'mode': route[1] if route else 'NONE'},
            'latest_activity_end': _format(latest_activity_end),
            'next_fixed_boundary': {'time': _format(boundary_start), 'place_name': target_place.get('name', ''),
                                    'travel_minutes_from_poi': onward_route[0] if onward_route else 0},
        })
        if not transition:
            raise DagBuildError('completion Agent failed to provide a valid local transition')
        activity_start = _minutes(transition['activity_start'])
        _, latest_activity_end = _factual_activity_window(poi, activity_start, latest_activity_end)
        transition_block_start = len(timeline)
        for block in transition['before_departure']:
            _append_decided_personal(day, current_place, block, timeline, slots)
        departure = _minutes(transition['departure_time'])
        if route:
            _append_transit(current_place, poi, departure, route, timeline)
        for block in transition['after_arrival']:
            _append_decided_personal(day, poi, block, timeline, slots)
        cursor = activity_start
        legal_end_times = _legal_end_times(activity_start, latest_activity_end)
        if not legal_end_times:
            _finish_candidate_attempt(
                candidate_attempts, attempt_record, route_queries, route_query_start,
                status="rejected", reason="no_legal_duration_boundary",
                activity_start=_format(activity_start),
                latest_activity_end=_format(latest_activity_end),
            )
            selected_place_ids.add(str(poi.get("place_id") or ""))
            continue

        duration = agents.choose_duration(
            _state(day, activity_start, poi, timeline, scenario_family, lodging, None, character),
            poi,
            legal_end_times=legal_end_times,
        )
        if not duration:
            raise DagBuildError("duration Agent failed to provide a valid decision")
        end = _minutes(duration["end_time"])

        node_id = f"day{day}_activity_{len(slots) + 1}"
        activity_name = str(duration.get("activity_name") or poi.get("name") or "activity")
        timeline.append({
            "node_id": node_id,
            "block_kind": "poi_activity",
            "event_type": service_type,
            "selected_service_type": service_type,
            "required_service_types": list(poi.get("required_service_types") or [service_type]),
            "activity_name": activity_name,
            "place_id": poi.get("place_id", ""),
            "place_name": poi.get("name", ""),
            "start": _format(cursor),
            "end": _format(end),
            "occupied_time_boundaries": _occupied_boundaries(cursor, end),
            "local_completion": {
                "output": deepcopy(transition),
                "node_ids": [item['node_id'] for item in timeline[transition_block_start:]],
            },
        })
        slots.append(_activity_slot(node_id, service_type, cursor, end, poi, activity_name))
        _finish_candidate_attempt(
            candidate_attempts, attempt_record, route_queries, route_query_start,
            status="accepted", reason="entered_dag",
            activity_start=_format(cursor), activity_end=_format(end),
        )
        all_pois.append(poi)
        selected_place_ids.add(str(poi.get("place_id") or ""))
        cursor = end
        current_place = poi
    return cursor, current_place


def _candidate_attempt_record(
    *, sequence: int, day: int, weekday_index: int, cursor: int,
    current_place: dict[str, Any], target_place: dict[str, Any], boundary_start: int,
    service_type: str, poi: dict[str, Any],
) -> dict[str, Any]:
    return {
        "attempt": sequence,
        "day": day,
        "weekday": WEEKDAY_NAMES[weekday_index],
        "current_time": _format(cursor),
        "current_place_id": current_place.get("place_id", ""),
        "current_place_name": current_place.get("name", ""),
        "next_boundary_time": _format(boundary_start),
        "next_boundary_place_id": target_place.get("place_id", ""),
        "next_boundary_place_name": target_place.get("name", ""),
        "selected_service_type": service_type,
        "candidate_place_id": poi.get("place_id", ""),
        "candidate_place_name": poi.get("name", ""),
        "candidate_opening_intervals": [
            [_format(int(opened)), _format(int(closed))]
            for opened, closed in poi.get("opening_intervals") or []
        ],
    }


def _finish_candidate_attempt(
    attempts: list[dict[str, Any]], record: dict[str, Any],
    route_queries: list[dict[str, Any]], route_query_start: int,
    *, status: str, reason: str, **details: Any,
) -> None:
    checks = [dict(item) for item in route_queries[route_query_start:]]
    record.update({
        "status": status,
        "reason": reason,
        "route_checks": checks,
        "route_check_count": len(checks),
        "api_request_count": sum(int(item.get("api_requests") or 0) for item in checks),
        "coordinate_reuse_count": sum(int(item.get("coordinate_reuse") or 0) for item in checks),
        **details,
    })
    attempts.append(record)


def _append_decided_personal(day, place, block, timeline, slots):
    start, end = _minutes(block['start']), _minutes(block['end'])
    node_id = f'day{day}_personal_{len(slots) + 1}'
    timeline.append({'node_id': node_id, 'block_kind': 'personal_activity',
                     'event_type': 'personal_activity', 'activity_name': block['activity_name'],
                     'place_id': place.get('place_id', ''), 'place_name': place.get('name', ''),
                     'start': block['start'], 'end': block['end'],
                     'occupied_time_boundaries': _occupied_boundaries(start, end),
                     'local_transition': True})
    slots.append(_activity_slot(node_id, 'personal_activity', start, end, place, block['activity_name']))


def _append_personal_block(
    agents: ItineraryConstructionAgents,
    state: dict[str, Any],
    start: int,
    latest_end: int,
    place: dict[str, Any],
    timeline: list[dict[str, Any]],
    slots: list[dict[str, Any]],
) -> None:
    legal_end_times = _legal_end_times(start, latest_end)
    if not legal_end_times:
        raise DagBuildError("no legal boundary remains for timeline completion")
    decision = agents.complete_timeline(
        state, legal_end_times=legal_end_times,
    )
    if not decision:
        raise DagBuildError("timeline-completion Agent failed to provide a valid decision")
    end = _minutes(decision["end_time"])
    node_id = f"day{state['current_day']}_personal_{len(slots) + 1}"
    activity_name = str(decision.get("activity_name") or "")
    if not activity_name:
        raise DagBuildError("timeline-completion Agent omitted activity_name")
    timeline.append({
        "node_id": node_id,
        "block_kind": "personal_activity",
        "event_type": "personal_activity",
        "activity_name": activity_name,
        "place_id": place.get("place_id", ""),
        "place_name": place.get("name", ""),
        "start": _format(start),
        "end": _format(end),
        "occupied_time_boundaries": _occupied_boundaries(start, end),
    })
    slots.append(_activity_slot(
        node_id, "personal_activity", start, end, place, activity_name,
    ))


def _route_minutes(
    sampler: ItinerarySampler,
    origin: dict[str, Any],
    destination: dict[str, Any],
    route_data: dict[str, dict[str, int]],
    route_queries: list[dict[str, Any]],
    maps_client: Any,
    preferred_mode: str = "",
) -> tuple[int, str] | None:
    if origin.get("place_id") == destination.get("place_id"):
        return (0, "NONE")
    sampler._materialize_pair_routes(  # noqa: SLF001 - shared factual route primitive
        [origin], [destination], route_data, route_queries, maps_client,
    )
    modes = route_data.get(f"{origin.get('place_id', '')}__TO__{destination.get('place_id', '')}", {})
    valid = [(int(minutes), mode) for mode, minutes in modes.items() if isinstance(minutes, (int, float))]
    preferred = [item for item in valid if item[1] == preferred_mode]
    return min(preferred) if preferred else min(valid) if valid else None


def _factual_activity_window(
    poi: dict[str, Any], earliest: int, latest: int,
) -> tuple[int, int] | None:
    for opened, closed in poi.get("opening_intervals") or []:
        start = max(earliest, int(opened))
        end = min(latest, int(closed))
        if start < end:
            return start, end
    return None


def _append_transit(
    origin: dict[str, Any], destination: dict[str, Any], start: int,
    route: tuple[int, str], timeline: list[dict[str, Any]],
) -> int:
    minutes, mode = route
    if minutes <= 0:
        return start
    end = start + minutes
    timeline.append({
        "node_id": f"transit_{len(timeline) + 1}",
        "block_kind": "transit",
        "event_type": "transit",
        "from_place_id": origin.get("place_id", ""),
        "to_place_id": destination.get("place_id", ""),
        "mode": mode,
        "start": _format(start),
        "end": _format(end),
    })
    return end


def _state(
    day: int, cursor: int, current_place: dict[str, Any], timeline: list[dict[str, Any]],
    scenario_family: str, lodging: dict[str, Any], next_fixed_event: dict[str, Any] | None,
    character: dict[str, Any],
) -> dict[str, Any]:
    return {
        "current_day": day,
        "current_time": _format(cursor),
        "current_location": {
            "place_id": current_place.get("place_id", ""),
            "name": current_place.get("name", ""),
        },
        "active_scenario": scenario_family,
        "traveler": {
            "name": character.get("name", ""),
            "occupation": character.get("occupation", ""),
            "preferences": character.get("preferences", {}),
        },
        "timeline": list(timeline),
        "lodging": dict(lodging),
        "next_fixed_event": next_fixed_event,
    }


def _fixed_events(
    day: int, num_days: int, hotel: dict[str, Any], arrival: dict[str, Any],
    departure: dict[str, Any], scenario: dict[str, Any],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if day == 1:
        events.extend([
            _event("arrival", arrival, day, "10:00", 60),
            _event("hotel_checkin", hotel, day, "14:00", 30),
        ])
    if day == num_days:
        events.append(_event("checkout", hotel, day, "10:30", 30))
        if scenario.get("departure_ends_task", True):
            events.append(_event("departure", departure, day, "14:00", 60))
    return events


def _event(event_type: str, place: dict[str, Any], day: int, time: str, duration: int) -> dict[str, Any]:
    return {
        "event_id": f"day{day}_{event_type}",
        "event_type": event_type,
        "place_id": place.get("place_id", ""),
        "place_name": place.get("name", ""),
        "location": place.get("location", {}),
        "earliest": time,
        "latest": time,
        "duration_min": duration,
        "is_fixed": True,
    }


def _boundary_event(day: int, hotel: dict[str, Any], time: int) -> dict[str, Any]:
    return {
        "event_id": f"day{day}_end",
        "event_type": "day_end",
        "place_id": hotel.get("place_id", ""),
        "place_name": hotel.get("name", ""),
        "location": hotel.get("location", {}),
        "earliest": _format(time),
        "duration_min": 0,
    }


def _event_place(
    event: dict[str, Any], hotel: dict[str, Any], arrival: dict[str, Any], departure: dict[str, Any]
) -> dict[str, Any]:
    event_type = event.get("event_type")
    if event_type in {"hotel_checkin", "checkout", "day_end"}:
        return hotel
    if event_type == "arrival":
        return arrival
    return departure


def _activity_slot(
    node_id: str, event_type: str, start: int, end: int,
    poi: dict[str, Any], activity_name: str,
) -> dict[str, Any]:
    candidate = dict(poi)
    candidate["duration_typical_min"] = end - start
    return {
        "slot_id": node_id,
        "event_type": event_type,
        "category": poi.get("category", event_type),
        "activity_name": activity_name,
        "time_range": (_format(start), _format(end)),
        "occupied_time_boundaries": _occupied_boundaries(start, end),
        "district_name": poi.get("district_name", ""),
        "candidates": [candidate],
        "forced_candidate_place_id": poi.get("place_id", ""),
        "requirement": "required",
        "route_selected": True,
        "depends_on": [],
    }


def _unique_pois(pois: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for poi in pois:
        place_id = str(poi.get("place_id") or "")
        if not place_id or place_id in seen:
            continue
        seen.add(place_id)
        result.append(poi)
    return result


def _minutes(value: str) -> int:
    return clock_minutes(value)


def _format(value: int) -> str:
    return format_minutes(value)


def _legal_end_times(start: int, latest_end: int) -> list[str]:
    """Return advancing daily boundaries, including a fixed-event clipping edge."""
    if latest_end <= start:
        return []
    daily_edges = (9 * 60, 12 * 60, 14 * 60, 17 * 60, 19 * 60, 22 * 60)
    ends = {edge for edge in daily_edges if start < edge <= latest_end}
    ends.add(latest_end)
    return [_format(edge) for edge in sorted(ends)]


def _occupied_boundaries(start: int, end: int) -> list[str]:
    boundaries = [
        (7 * 60 + 30, 9 * 60, "breakfast"),
        (9 * 60, 12 * 60, "morning_activity"),
        (12 * 60, 14 * 60, "lunch"),
        (14 * 60, 17 * 60, "afternoon_activity"),
        (17 * 60, 19 * 60, "dinner"),
        (19 * 60, 22 * 60, "evening_leisure"),
    ]
    return [name for left, right, name in boundaries if start < right and end > left]
