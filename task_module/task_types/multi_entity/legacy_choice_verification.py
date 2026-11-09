"""Read-only legacy writer-input compatibility; no prose semantic inference.

Evaluate only the original input C, with historical budget-grade rules.
Absent activity duration is not an extra C. Prose alignment remains human review.
"""
from copy import deepcopy
import ast
import math

from .solver import _clock_minutes, _format_clock

LEGACY_BUDGET_CAP = {"low": 80, "medium": 130, "high": 10000}


def recover_legacy_raw_input(text):
    """Parse literal historical input lines only; never execute log content."""
    values = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in ("参与者", "候选地点", "资源", "协同关系", "路线分钟数"):
            values[key] = ast.literal_eval(value)
    participants = values.get("参与者", [])
    venues = values.get("候选地点", [])
    return {"source": "original_llm_in_literal_records",
            "participants": [dict({k: p[k] for k in ("name", "available_from", "must_leave_by", "dietary", "budget") if k in p},
                                  departure_area=p.get("home_place_name", p.get("name"))) for p in participants],
            "candidate_venues": [dict({k: v[k] for k in ("name", "dietary_options", "max_party", "open_until") if k in v},
                                      **({"price_per_person": v["price_pp"]} if "price_pp" in v else {})) for v in venues],
            "relations": values.get("协同关系"), "resources": values.get("资源"),
            "route_data": values.get("路线分钟数", {}),
            "meeting": {"arrival_mode": "independent" if values.get("协同关系") ==
                        ["joint_meeting", "individual_time_windows"] and not values.get("资源") else "requires_review"},
            "note": "No duration C added; unrelated character fields are not constraints"}


def verify_legacy_candidate_choice(public_facts, venues, route_data, target_service_id, expected_feasible=True):
    people = public_facts["participants"]
    meeting = public_facts["meeting"]
    if meeting.get("arrival_mode") != "independent" or not people:
        return {"valid": False, "reason": "legacy_coordination_requires_review", "candidate_results": [],
                "accepted_place_ids": [], "unresolved_place_ids": []}
    duration = meeting.get("duration_min")
    # Zero is arithmetic only: no minimum duration is imposed when C omits it.
    span = duration if duration is not None else 0
    ids = [v.get("service_id") for v in venues]
    if not all(ids) or len(ids) != len(set(ids)) or (expected_feasible and target_service_id not in ids):
        raise ValueError("Legacy scope needs distinct service identities and the saved GT")
    by_name = {}
    for fact in public_facts["candidate_venues"]:
        if fact["name"] in by_name:
            raise ValueError("Ambiguous legacy public venue name")
        by_name[fact["name"]] = fact
    rows = []
    for identity in venues:
        failures, missing, travel, arrivals = [], [], [], []
        fact = by_name.get(identity["name"])
        row = {"place_id": identity.get("place_id"), "service_id": identity["service_id"],
               "name": identity["name"], "failures": failures, "missing_evidence": missing,
               "travel_evidence": travel}
        if fact is None:
            missing.append({"field": "writer_candidate_venue"})
        else:
            capacity = fact.get("max_party")
            if "max_party" not in fact:
                pass  # No capacity condition supplied by the historical input.
            elif not isinstance(capacity, (int, float)):
                missing.append({"field": "max_party"})
            elif capacity < len(people):
                failures.append({"constraint": "party_size", "required": len(people), "capacity": capacity})
            dietary = {p.get("dietary") for p in people if p.get("dietary") not in (None, "", "none", "无")}
            options = fact.get("dietary_options")
            if dietary and not isinstance(options, list):
                missing.append({"field": "dietary_options"})
            elif dietary and not dietary.issubset(set(options)):
                failures.append({"constraint": "dietary", "required": sorted(dietary), "provided": options})
            price = fact.get("price_per_person")
            for person in people:
                budget = person.get("budget")
                cap = budget if type(budget) in (int, float) else LEGACY_BUDGET_CAP.get(budget)
                if cap is not None:
                    if type(price) not in (int, float):
                        missing.append({"field": "price_per_person"})
                    elif price > cap:
                        failures.append({"constraint": "budget", "participant": person["name"],
                                         "price": price, "maximum": cap, "original_budget": budget,
                                         "rule_source": "original_legacy_budget_grade"})
            if not failures:
                for person in people:
                    key = f"{person['departure_area']}→{fact['name']}"
                    values = [v for v in route_data.get(key, {}).values()
                              if type(v) in (int, float) and math.isfinite(v) and v >= 0]
                    if not values:
                        missing.append({"field": "route_duration", "participant": person["name"], "route_key": key})
                        continue
                    minutes = min(values)
                    arrivals.append(_clock_minutes(person["available_from"]) + minutes)
                    travel.append({"participant": person["name"], "route_key": key, "travel_min": minutes})
                if len(arrivals) == len(people):
                    earliest = max(arrivals)
                    latest = min(_clock_minutes(p["must_leave_by"]) - span for p in people)
                    if fact.get("open_until"):
                        latest = min(latest, _clock_minutes(fact["open_until"]) - span)
                    if earliest > latest:
                        failures.append({"constraint": "joint_time_and_open_window", "earliest_start_min": earliest,
                                         "latest_start_min": latest, "duration_min": duration})
                    else:
                        row.update(meeting_start=_format_clock(int(earliest)),
                                   meeting_end=_format_clock(int(earliest + span)) if duration is not None else None,
                                   latest_start=_format_clock(int(latest)), duration_constraint=duration)
        row["status"] = "excluded" if failures else "unresolved" if missing else "feasible"
        rows.append(row)
    accepted = [r["service_id"] for r in rows if r["status"] == "feasible"]
    unresolved = [r["service_id"] for r in rows if r["status"] == "unresolved"]
    return {"scope": "legacy_task_candidate_restaurants", "public_constraints": deepcopy(public_facts),
            "candidate_results": rows, "accepted_place_ids": accepted, "unresolved_place_ids": unresolved,
            "identity_kind": "legacy_service_id", "expected_feasible": expected_feasible,
            "valid": (accepted == [target_service_id] if expected_feasible else not accepted) and not unresolved,
            "prompt_alignment": "requires_human_review_not_script_semantic_check"}
