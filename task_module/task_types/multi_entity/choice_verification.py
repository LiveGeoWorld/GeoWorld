"""Candidate-local proof using only the facts supplied to the task writer.

No POI expansion, route requests, GT rematerialization or semantic inference.
Missing route facts are unresolved, never an exclusion certificate.
"""
from __future__ import annotations

import math
from typing import Any

from .solver import _clock_minutes, _format_clock
from .constraint_protocol import validate_constraints, route_options, start_interval
from .type_facts import matches_place_type


def early_arrival_leads(public_facts):
    if 'early_arrival' not in public_facts:
        return {}
    arrangement = public_facts['early_arrival']
    if not isinstance(arrangement, dict) or set(arrangement) != {'participants', 'lead_minutes', 'purpose'}:
        raise ValueError('Invalid early_arrival fields')
    names = arrangement['participants']
    participants = {p['name'] for p in public_facts['participants']}
    if (not isinstance(names, list) or not names or
            any(not isinstance(n, str) or n not in participants for n in names) or len(set(names)) != len(names)):
        raise ValueError('early_arrival needs distinct names from the supplied participants')
    lead = arrangement['lead_minutes']
    if type(lead) not in (int, float) or not 0 < lead < 1440:
        raise ValueError('early_arrival lead_minutes must be a positive duration within one day')
    if not isinstance(arrangement['purpose'], str) or not arrangement['purpose'].strip():
        raise ValueError('early_arrival requires a specific purpose')
    return dict.fromkeys(names, lead)


def verify_candidate_choice(
    public_facts: dict[str, Any], venues: list[dict[str, Any]],
    route_data: dict[str, dict[str, int]], target_place_id: str,
) -> dict[str, Any]:
    activity = public_facts["activity"]
    participants = public_facts["participants"]
    required_type = activity["required_place_type"]
    duration = activity["duration_min"]
    added_c = public_facts.get('additional_constraints', [])
    leads = early_arrival_leads(public_facts)
    validate_constraints(added_c, participants)
    if activity["arrival_mode"] != "independent" or not participants:
        raise ValueError("Unsupported public coordination contract")
    ids = [venue.get("place_id") for venue in venues]
    if not all(ids) or len(set(ids)) != len(ids) or target_place_id not in ids:
        raise ValueError("Candidate scope needs distinct place IDs and the fixed GT")
    results = []
    for venue in venues:
        failures, missing, arrivals, travel = [], [], [], []
        for constraint in added_c:
            if constraint['object'] != 'place':
                continue
            types = venue.get('types')
            if not isinstance(types, list) or not types:
                missing.append({'field': 'google_types', 'constraint': constraint})
            elif constraint['value'] not in types:
                failures.append({'constraint': constraint, 'actual_google_types': types})
        if not matches_place_type(venue, required_type):
            failures.append({"constraint": "required_place_type", "expected": required_type,
                             "actual": venue.get("primary_type"), "types": venue.get("types", [])})
        elif 'activity_opening_intervals' in venue and not any(
                closed - opened >= duration for opened, closed in venue['activity_opening_intervals'] or []):
            intervals = venue['activity_opening_intervals']
            failures.append({'constraint': 'venue_opening_window',
                             'reason': 'missing_opening_hours' if intervals is None else 'no_full_activity_window',
                             'opening_intervals': intervals, 'duration_min': duration})
        else:
            for person in participants:
                key = f"{person['departure_area']}→{venue['name']}"
                modes = route_data.get(key, {})
                # Only the existing route facts; private mode limits are not C.
                available = {mode: value for mode, value in modes.items()
                             if mode in ('WALK', 'TRANSIT', 'DRIVE') and
                             isinstance(value, (int, float)) and not isinstance(value, bool)
                             and math.isfinite(value) and value >= 0}
                eligible, gap, route_failures = route_options(person, activity, added_c, available)
                failures.extend(route_failures)
                if gap:
                    missing.append({**gap, 'route_key': key})
                    continue
                if not eligible:
                    continue
                chosen, minutes = min(eligible.items(), key=lambda item: item[1])
                arrival = _clock_minutes(person["available_from"]) + minutes
                arrivals.append(arrival + leads.get(person['name'], 0))
                travel.append({"participant": person["name"], "route_key": key,
                               "travel_mode": chosen, "travel_min": minutes, "arrival_min": arrival})
                if person['name'] in leads:
                    travel[-1]['early_arrival_min'] = leads[person['name']]
            if not missing and not failures:
                earliest = max(arrivals)
                latest = min(_clock_minutes(p["must_leave_by"]) - duration for p in participants)
                earliest, latest, start_conditions = start_interval(added_c, earliest, latest)
                if 'activity_opening_intervals' in venue:
                    intervals = venue['activity_opening_intervals']
                    starts = [math.ceil(max(earliest, opened)) for opened, closed in intervals or []
                              if math.ceil(max(earliest, opened)) <= min(latest, closed - duration)]
                    if starts:
                        earliest = min(starts)
                    else:
                        failures.append({'constraint': 'venue_opening_window',
                                         'reason': 'no_full_activity_window',
                                         'opening_intervals': intervals, 'earliest_start_min': earliest,
                                         'latest_start_min': latest, 'duration_min': duration})
                # Keep the route precision in the interval; only the emitted
                # HH:MM start needs to land on a whole minute.
                if math.ceil(earliest) > latest:
                    failures.append({"constraint": "joint_time_window",
                                     "earliest_start_min": earliest, "latest_start_min": latest,
                                     "duration_min": duration, 'start_conditions': start_conditions})
        status = "excluded" if failures else "unresolved" if missing else "feasible"
        row = {"place_id": venue["place_id"], "name": venue["name"], "status": status,
               "failures": failures, "missing_evidence": missing, "travel_evidence": travel}
        if status == "feasible":
            start = math.ceil(earliest)
            row["meeting_start"] = _format_clock(start)
            row["meeting_end"] = _format_clock(start + duration)
        results.append(row)
    accepted = [row["place_id"] for row in results if row["status"] == "feasible"]
    unresolved = [row["place_id"] for row in results if row["status"] == "unresolved"]
    preference = public_facts.get('commute_preference')
    if preference and accepted:
        scores = {}
        for row in results:
            if row['status'] != 'feasible':
                continue
            evidence = next(e for e in row['travel_evidence'] if e['participant'] == preference['participant'])
            if evidence['travel_mode'] != preference['mode']:
                raise ValueError('Commute preference requires its specified travel mode')
            seconds = round(evidence['travel_min'] * 60)
            target = preference.get('target_minutes')
            score = seconds if target is None else abs(seconds - round(target * 60))
            row['commute_ranking'] = {'duration_s': seconds, 'score_s': score}
            scores[row['place_id']] = score
        best = min(scores.values())
        accepted = [place_id for place_id in accepted if scores[place_id] == best]
    return {"scope": "task_candidate_places", "candidate_place_ids": ids,
            "public_constraints": public_facts, "candidate_results": results,
            "accepted_place_ids": accepted, "unresolved_place_ids": unresolved,
            "valid": accepted == [target_place_id] and not unresolved}
