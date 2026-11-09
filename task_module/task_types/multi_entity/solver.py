"""Deterministic constraint-graph oracle for multi-entity coordination."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def solve_coordination(participants: list[dict[str, Any]], venues: list[dict[str, Any]],
                       route_data: dict[str, dict[str, int]], scenario: dict[str, Any],
                       target_place_id: str = "") -> dict[str, Any]:
    """Verify the preselected factual GT from character routes and time windows."""
    if 'dynamic_public_requirements' in scenario:
        from task_module.task_types.dynamic_replanning.multi_entity_c1_conditions import joint_check
        requirements = scenario['dynamic_public_requirements']
        proof = joint_check(requirements, venues, route_data, target_place_id,
                            unavailable_ids=scenario.get('unavailable_place_ids', []))
        row = next((r for r in proof['candidate_results'] if r['place_id'] == target_place_id), {})
        feasible = row.get('status') == 'feasible'
        place = next((p for p in venues if p.get('place_id') == target_place_id), {})
        plan = {'place_id': target_place_id, 'place_name': place.get('name'), 'location': place.get('location'),
                'meeting_start': row.get('meeting_start'), 'meeting_end': row.get('meeting_end'),
                'assignments': [{'character_id': p.get('character_id'), 'arrival_mode': 'independent'} for p in participants],
                'pickup_order': []}
        return {'expected_feasible': feasible, 'expected_place_id': target_place_id if feasible else '',
                'constraints': deepcopy(requirements), 'acceptable_plans': [plan] if feasible else [],
                'infeasibility_certificate': [] if feasible else [row],
                'oracle_source': 'dynamic_multi_entity_C1.v1'}
    party_size = len(participants)
    common_start = max((p.get("available_from", "18:00") for p in participants), default="18:00")
    common_end = min((p.get("must_leave_by", "21:00") for p in participants), default="21:00")
    meeting_duration_min = max(1, int(scenario.get("meeting_duration_min", 60) or 60))
    public_c = scenario.get('public_activity_C', {})
    plans = []
    target = next((venue for venue in venues if venue.get("place_id") == target_place_id), None)
    if target and str(target.get("primary_type") or "") == str(scenario.get("required_primary_type") or ""):
        effective_routes = route_data
        if public_c:
            allowed = public_c.get('allowed_travel_modes', ['WALK', 'TRANSIT', 'DRIVE'])
            cap = public_c.get('max_commute_min', float('inf'))
            effective_routes = {key: {mode: value for mode, value in modes.items()
                                     if mode in allowed and isinstance(value, (int, float))
                                     and not isinstance(value, bool) and 0 <= value <= cap}
                                for key, modes in route_data.items()}
        projection = scenario.get('public_fact_projection')
        if projection:
            from .choice_verification import verify_candidate_choice
            row = verify_candidate_choice(projection, [target], route_data, target_place_id)['candidate_results'][0]
            if row['status'] == 'feasible':
                values = [entry['travel_min'] for entry in row['travel_evidence']]
                plan = {'total_commute_min': sum(values), 'max_individual_commute_min': max(values),
                        'assignments': [{'character_id': p.get('character_id', ''), 'arrival_mode': 'independent'} for p in participants],
                        'pickup_order': [], '_meeting_start': row['meeting_start'], '_meeting_end': row['meeting_end']}
            else:
                plan = None
        else:
            plan = _independent_plan(participants, target, effective_routes, meeting_duration_min)
        if plan is None:
            plans = []
        else:
            meeting_start = plan.pop("_meeting_start", common_start)
            meeting_end = plan.pop("_meeting_end", common_end)
            plan.update({
                "place_id": target.get("place_id", ""),
                "place_name": target.get("name", ""),
                "location": target.get("location", {}),
                "activity_type": scenario.get("activity_type", "shared_activity"),
                "meeting_start": meeting_start,
                "meeting_end": meeting_end,
                "scenario_family": "independent_meeting",
            })
            plans.append(plan)
    feasible = bool(plans)
    best = plans[0] if plans else {}
    return {
        "expected_feasible": feasible,
        "expected_place_id": best.get("place_id", ""),
        "constraints": {
            "party_size": party_size,
            "max_commute_min": public_c.get('max_commute_min', best.get("max_individual_commute_min", 0)),
            "common_time_window": {"start": common_start, "end": common_end},
            "minimum_meeting_duration_min": meeting_duration_min,
            "activity_type": scenario.get("activity_type", "shared_activity"),
            "required_primary_type": scenario.get("required_primary_type", ""),
            "allowed_travel_modes": public_c.get('allowed_travel_modes', scenario.get("allowed_travel_modes", [])),
            "scenario_family": "independent_meeting",
            "relations": scenario.get("relations", []),
            "resources": scenario.get("resources", []),
            **({'additional_constraints': scenario['public_fact_projection'].get('additional_constraints', [])}
               if 'public_fact_projection' in scenario else {}),
        },
        "acceptable_plans": plans,
        "infeasibility_certificate": [] if feasible else [_infeasibility_reason(participants, venues, scenario)],
        "oracle_source": "deterministic_multi_entity_constraint_graph.v3",
    }


def _independent_plan(
    participants: list[dict[str, Any]],
    venue: dict[str, Any],
    route_data: dict[str, dict[str, int]],
    meeting_duration_min: int,
) -> dict[str, Any] | None:
    commutes = [_route_minutes(p, venue, route_data) for p in participants]
    if any(minutes is None for minutes in commutes):
        return None
    values = [int(minutes) for minutes in commutes if minutes is not None]
    schedule = _meeting_schedule(participants, values, meeting_duration_min)
    if schedule is None:
        return None
    return {
        "total_commute_min": sum(values), "max_individual_commute_min": max(values),
        "assignments": [{"character_id": p.get("character_id", ""), "arrival_mode": "independent"} for p in participants],
        "pickup_order": [],
        "_meeting_start": schedule[0],
        "_meeting_end": schedule[1],
    }


def _vehicle_plan(
    participants: list[dict[str, Any]],
    venue: dict[str, Any],
    route_data: dict[str, dict[str, int]],
    scenario: dict[str, Any],
    meeting_duration_min: int,
) -> dict[str, Any] | None:
    resource = next((r for r in scenario.get("resources", []) if r.get("resource_type") == "private_vehicle"), None)
    if not resource or resource.get("capacity", 0) < len(participants):
        return None
    driver = next((p for p in participants if p.get("character_id") == resource.get("owner_character_id")), None)
    if not driver:
        return None
    riders = [p for p in participants if p is not driver]
    if scenario.get("family") == "pickup_sequence_meeting":
        # A deterministic ordering keeps the oracle reproducible; every leg is
        # validated against cached driving data.
        riders.sort(key=lambda p: p.get("character_id", ""))
        chain = [driver, *riders]
        total = 0
        leg_minutes = []
        for origin, destination in zip(chain, chain[1:]):
            minutes = _route_minutes(origin, destination, route_data)
            if minutes is None:
                return None
            total += minutes
            leg_minutes.append(minutes)
        last_leg = _route_minutes(chain[-1], venue, route_data)
        if last_leg is None:
            return None
        total += last_leg
        max_individual = total
        pickup_order = [p.get("character_id", "") for p in riders]
    else:
        # Shared-vehicle meeting is represented as one owner-driven group trip.
        total = _route_minutes(driver, venue, route_data)
        if total is None:
            return None
        max_individual = total
        pickup_order = []
    schedule = _meeting_schedule(participants, [max_individual] * len(participants), meeting_duration_min)
    if schedule is None:
        return None
    return {
        "total_commute_min": total, "max_individual_commute_min": max_individual,
        "pickup_order": pickup_order,
        "assignments": [
            {"character_id": p.get("character_id", ""),
             "arrival_mode": "driver" if p is driver else "shared_vehicle",
             "resource_owner": driver.get("character_id", "")}
            for p in participants
        ],
        "_meeting_start": schedule[0],
        "_meeting_end": schedule[1],
    }


def _route_minutes(origin: dict[str, Any], destination: dict[str, Any], route_data: dict[str, dict[str, int]]) -> int | None:
    origin_name = str(origin.get("home_place_name", origin.get("place_name", origin.get("name", ""))))[:15]
    destination_name = str(destination.get("place_name", destination.get("name", "")))[:15]
    modes = next((value for key, value in route_data.items() if origin_name and destination_name and origin_name in key and destination_name in key), {})
    values = [minutes for minutes in modes.values() if isinstance(minutes, (int, float))]
    return int(min(values)) if values else None


def _meeting_schedule(
    participants: list[dict[str, Any]],
    arrival_minutes: list[int],
    meeting_duration_min: int,
) -> tuple[str, str] | None:
    """Return a real meeting interval after travel, or ``None`` if impossible."""
    if len(participants) != len(arrival_minutes):
        return None
    earliest_start = max(
        (_clock_minutes(person.get("available_from", "18:00")) + travel
         for person, travel in zip(participants, arrival_minutes)),
        default=18 * 60,
    )
    latest_start = min(
        (_clock_minutes(person.get("must_leave_by", "21:00")) - meeting_duration_min
         for person in participants),
        default=21 * 60 - meeting_duration_min,
    )
    if earliest_start > latest_start:
        return None
    return _format_clock(earliest_start), _format_clock(earliest_start + meeting_duration_min)


def _clock_minutes(value: Any) -> int:
    try:
        hour, minute = str(value).split(":", 1)
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return 18 * 60


def _format_clock(minutes: int) -> str:
    hour, minute = divmod(max(0, int(minutes)), 60)
    return f"{hour:02d}:{minute:02d}"


def _infeasibility_reason(participants: list[dict[str, Any]], venues: list[dict[str, Any]], scenario: dict[str, Any]) -> str:
    return "preselected_gt_fails_type_time_or_character_route_constraints"
