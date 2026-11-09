"""Audit diagnostics for region_aware GT."""

from __future__ import annotations
from typing import Any


def run_audit(llm_result: dict, constraint_ids: list[str],
              planning_profile: dict[str, Any] | None = None,
              constraints: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    gt = llm_result.get("gt", {})
    plan = gt.get("expected_plan", {})
    contract = gt.get("decision_contract", {}) or {}
    material = contract.get("material_constraints", []) or []
    if not contract.get("material") or not material:
        errors.append("no source-backed cultural constraint changes a plan dimension")
    material_ids = {str(item.get("constraint_id") or "") for item in material}
    if constraint_ids and not material_ids.intersection(set(constraint_ids)):
        errors.append("decision contract is not bound to the sampled constraints")
    planning_dimensions = {
        "selected_places", "schedule", "itinerary_structure", "route", "travel_mode",
        "duration", "participants", "service_assignment",
    }
    if not any(planning_dimensions.intersection(item.get("changed_dimensions", []) or []) for item in material):
        errors.append("material decision has no place, schedule, or itinerary effect")

    # Do not let a direct generator invocation bypass the sampler's formal
    # admission rule.  The current implementation can only turn a private
    # forbidden_time_range into a fixed start time; it has no public temporal
    # decision kernel or scoreable decision class.  Such a rule may remain a
    # prototype/legacy artifact, but cannot be formalized.
    for constraint in constraints or []:
        effect = (constraint.get("hidden_rule", {}) or {}).get("decision_effect", {}) or {}
        if str(effect.get("dimension") or "") == "schedule":
            errors.append("schedule-based Region rule lacks a formal public temporal decision kernel")
            break

    prompt = str(llm_result.get("user_prompt") or "")
    # Asking the Agent to list etiquette directly turns the task into recall.
    # The public prompt may request a plan that naturally fits local context.
    direct_request_markers = ("得体行为", "应遵循", "礼仪要求", "具体要求", "如何持筷", "取食顺序", "摆放位置")
    if any(marker in prompt for marker in direct_request_markers):
        errors.append("user prompt directly requests cultural-rule recitation")

    destination_label = str((planning_profile or {}).get("destination_label") or "").strip()
    if destination_label and destination_label not in prompt:
        errors.append("user prompt omitted the selected public destination category")

    wrong = gt.get("wrong_plans", [])
    if not wrong and not gt.get("expected_feasible", True):
        warnings.append("infeasible task but no wrong_plans provided")

    if not plan.get("events"):
        errors.append("private plan has no executable events")

    return {"errors": errors, "warnings": warnings}
