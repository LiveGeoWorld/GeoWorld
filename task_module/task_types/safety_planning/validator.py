"""Build-time audit for the private safety causal chain."""

from __future__ import annotations

from typing import Any


def run_audit(result: dict[str, Any], risky: dict[str, Any], alternatives: list[dict[str, Any]],
              route_data: dict[str, dict[str, int]]) -> dict[str, Any]:
    gt = result.get("gt", {})
    warnings: list[str] = []
    evidence = gt.get("risk_evidence", [])
    risk_case = gt.get("safety_risk_case", {})
    chain = gt.get("causal_chain", {})
    plan = gt.get("expected_plan", {})
    if not evidence:
        warnings.append("missing observable risk evidence")
    if not isinstance(risk_case, dict) or not risk_case.get("hazard"):
        warnings.append("missing hazard/exposure safety risk case")
    elif not risk_case.get("exposure") or not risk_case.get("observation_contract"):
        warnings.append("safety risk case is missing exposure or observation contract")
    if chain.get("original_goal", {}).get("place_id") != risky.get("place_id"):
        warnings.append("causal chain original goal does not match risky target")
    for card in evidence:
        if not card.get("source") or not card.get("field") or not card.get("observable_via"):
            warnings.append("risk evidence is missing source, field, or observable action")
    if gt.get("expected_safe") is False and plan.get("recommendation") not in {"modify", "postpone", "reject"}:
        warnings.append("unsafe original plan has no protective decision")
    if gt.get("expected_safe") is False and not gt.get("counterfactual", {}).get("decision_changed"):
        warnings.append("risk does not change the baseline decision")
    allowed = {item.get("place_id", "") for item in alternatives}
    for alternative in plan.get("alternatives", []):
        if alternative.get("place_id") not in allowed:
            warnings.append("private alternative is not in sampled alternatives")
    for alternative in plan.get("alternatives", []):
        route_key = f"{gt.get('character_id', '')}__TO__{alternative.get('place_id', '')}"
        if route_key in route_data and not route_data[route_key]:
            warnings.append("selected alternative has no cached route")
    return {"warnings": warnings}
