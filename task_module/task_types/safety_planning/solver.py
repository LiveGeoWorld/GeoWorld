"""Deterministic oracle for evidence-grounded safety-aware planning."""

from __future__ import annotations

from typing import Any


def solve_safety_plan(character: dict[str, Any], risky_target: dict[str, Any],
                      evidence: list[dict[str, Any]], alternatives: list[dict[str, Any]],
                      distractors: list[dict[str, Any]], route_data: dict[str, dict[str, int]],
                      safety_risk_case: dict[str, Any] | None = None) -> dict[str, Any]:
    """Derive hazard -> exposure -> observation -> mitigation privately."""
    safety_risk_case = safety_risk_case or {}
    origin_id = character.get("home_place_id", "")
    viable: list[dict[str, Any]] = []
    for alternative in alternatives:
        route = _best_route(origin_id, alternative.get("place_id", ""), route_data)
        if route is None:
            continue
        viable.append({**alternative, "route": route})
    viable.sort(key=lambda item: (
        item.get("goal_relation") != "goal_preserving", item["route"]["minutes"], item.get("place_id", ""),
    ))

    baseline_route = _best_route(origin_id, risky_target.get("place_id", ""), route_data)
    baseline_plan = {
        "recommendation": "accept",
        "place_id": risky_target.get("place_id", ""),
        "name": risky_target.get("name", ""),
        "route": baseline_route,
    }
    original_plan_safe = not bool(evidence)
    best = viable[0] if viable else None
    if original_plan_safe:
        recommendation = "accept"
    elif best:
        recommendation = "modify"
    else:
        recommendation = "postpone"

    risk_factors = [_factor_text(card) for card in evidence]
    # Preserve every solver-verified safe alternative privately.  The first is
    # still the static task's deterministic preferred plan; later entries are
    # not used to relax static judging, but may support a Dynamic source only
    # after the Dynamic proof gate validates a complete replacement chain.
    expected_alternatives = [{
        "place_id": item.get("place_id", ""), "name": item.get("name", ""),
        "goal_relation": item.get("goal_relation", "goal_degrading"),
        "route_mode": item["route"]["mode"], "commute_min": item["route"]["minutes"],
    } for item in viable]
    causal_chain = {
        "original_goal": {"place_id": risky_target.get("place_id", ""), "service_type": risky_target.get("service_type", "")},
        "hazard": safety_risk_case.get("hazard", ""),
        "exposure": safety_risk_case.get("exposure", {}),
        "vulnerability": safety_risk_case.get("vulnerability", {}),
        "observations": evidence,
        "rule": "an observed hazard that affects this traveller's exposure requires a mitigated reachable plan",
        "decision": recommendation,
        "alternatives": expected_alternatives,
    }
    counterfactual = {
        "without_static_risk": baseline_plan,
        "with_static_risk": {
            "recommendation": recommendation,
            "place_id": best.get("place_id", "") if best else "",
            "name": best.get("name", "") if best else "",
        },
        "decision_changed": bool(not original_plan_safe and (
            recommendation != "accept" or (best and best.get("place_id") != risky_target.get("place_id"))
        )),
    }
    must_avoid = [risky_target.get("place_id", "")]
    must_avoid.extend(item.get("place_id", "") for item in distractors)
    return {
        "character_id": character.get("home_place_id", ""),
        # ``expected_safe`` remains for legacy readers and means the original
        # plan is safe. It is not the feasibility of the mitigated plan.
        "expected_safe": original_plan_safe,
        "original_plan_safe": original_plan_safe,
        "safe_plan_feasible": bool(best) if not original_plan_safe else bool(baseline_route),
        "expected_plan": {
            "recommendation": recommendation,
            "alternatives": expected_alternatives,
            "rejected": ([{"place_id": risky_target.get("place_id", ""), "name": risky_target.get("name", "")} ] if not original_plan_safe else []),
            "rationale": " | ".join(risk_factors),
        },
        "acceptable_plans": expected_alternatives,
        "risk_factors": risk_factors,
        "risk_evidence": evidence,
        "safety_risk_case": safety_risk_case,
        "causal_chain": causal_chain,
        "counterfactual": counterfactual,
        "constraints": {
            "original_place_id": risky_target.get("place_id", ""),
            "original_goal_service_type": risky_target.get("service_type", ""),
            "must_avoid_place_ids": [place_id for place_id in must_avoid if place_id],
            "required_safe_alternative": not original_plan_safe and bool(best),
            "required_observations": [card.get("observable_via", {}) for card in evidence],
            "required_safety_checks": [
                {
                    "app": item.get("observation_contract", {}).get("app", ""),
                    "tool": item.get("observation_contract", {}).get("tool", ""),
                }
                for item in evidence
            ],
            "alternative_goal_relation": best.get("goal_relation") if best else None,
        },
        "oracle_source": "deterministic_safety_causal_solver.v2",
    }


def _best_route(origin_id: str, destination_id: str, route_data: dict[str, dict[str, int]]) -> dict[str, Any] | None:
    modes = route_data.get(f"{origin_id}__TO__{destination_id}", {})
    valid = [(mode, int(minutes)) for mode, minutes in modes.items() if isinstance(minutes, (int, float)) and minutes >= 0]
    if not valid:
        return None
    mode, minutes = min(valid, key=lambda item: item[1])
    return {"mode": mode, "minutes": minutes}


def _factor_text(card: dict[str, Any]) -> str:
    field, value = card.get("field", "risk"), card.get("value", "")
    if field == "open_status":
        return f"service status is {value}"
    if field == "queue_time_min":
        return f"queue is {value} minutes"
    if field == "crowding_level":
        return f"crowding level is {value}"
    return f"{field}={value}"
