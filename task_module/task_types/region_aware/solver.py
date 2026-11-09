"""Private deterministic oracle for verified region-aware constraints."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def solve_region_plan(constraints: list[dict[str, Any]], main_pois: list[dict[str, Any]],
                      distractors: list[dict[str, Any]], route_data: dict[str, Any] | None = None,
                      character: dict[str, Any] | None = None,
                      operational_state: dict[str, Any] | None = None,
                      planning_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    plan = _solve_core(constraints, main_pois, distractors, route_data, character, operational_state, planning_profile)
    decision_contract = _decision_contract(
        constraints, main_pois, distractors, route_data, character, operational_state, plan, planning_profile,
    )
    plan["decision_contract"] = decision_contract
    return plan


def _solve_core(constraints: list[dict[str, Any]], main_pois: list[dict[str, Any]],
                distractors: list[dict[str, Any]], route_data: dict[str, Any] | None,
                character: dict[str, Any] | None, operational_state: dict[str, Any] | None,
                planning_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    requirements = _requirements(constraints, planning_profile)
    unavailable_place_ids = {
        str(place_id) for place_id in (operational_state or {}).get("unavailable_place_ids", []) or []
    }
    compliant = [
        poi for poi in main_pois
        if not (set(poi.get("cultural_tags", [])) & set(requirements["avoid_place_tags"]))
        and (not requirements["goal_place_types"] or str(poi.get("primary_type") or "") in requirements["goal_place_types"])
        and str(poi.get("place_id") or "") not in unavailable_place_ids
        and _reachable_from_actor(poi, route_data or {}, character or {})
    ]
    # Region static construction is target-first: P0 is one concrete initial
    # decision, not an unordered list of every compliant candidate. Other
    # compliant locations remain private acceptable alternatives and can later
    # become a genuine P1 under a Dynamic overlay.
    selected = [compliant[0].get("place_id", "")] if compliant and compliant[0].get("place_id") else []
    start = _safe_start(requirements["forbidden_time_ranges"])
    events = ([{"place_id": compliant[0].get("place_id", ""), "place_name": compliant[0].get("name", ""), "start": start}]
              if compliant else [])
    wrong = [
        {"place_id": poi.get("place_id", ""), "description": poi.get("name", "")}
        for poi in distractors if set(poi.get("cultural_tags", [])) & set(requirements["avoid_place_tags"])
    ]
    acceptable = _acceptable_plans(compliant, start, requirements)
    return {
        "expected_feasible": bool(selected),
        "expected_plan": {"selected_place_ids": selected, "events": events,
                          "cultural_actions": requirements["required_protocol_actions"],
                          "avoid_actions": requirements["forbidden_actions"], "rationale": "culturally appropriate plan"},
        "acceptable_plans": acceptable,
        "wrong_plans": wrong,
        "cultural_requirements": requirements,
        "explanation_keywords": requirements["explanation_keywords"],
        "cultural_decisions": [{"constraint_id": rule.get("id", ""), "rule_type": rule.get("type", "")}
                               for rule in constraints],
        "oracle_source": "wikivoyage_verified_region_solver.v3",
    }


def _acceptable_plans(compliant: list[dict[str, Any]], start: str, requirements: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only culturally and route-validated plan alternatives.

    Each entry is one alternative target selected from already filtered
    compliant POIs. It is private solver output and does not broaden the
    public task or disclose regional rules to the tested Agent.
    """
    if not compliant:
        return []
    plans: list[dict[str, Any]] = []
    for item in compliant:
        place_id = item.get("place_id", "")
        if not place_id:
            continue
        plans.append({
            "selected_place_ids": [place_id],
            "events": [{"place_id": place_id, "place_name": item.get("name", ""), "start": start}],
            "cultural_actions": requirements["required_protocol_actions"],
            "avoid_actions": requirements["forbidden_actions"],
        })
    return plans


def _requirements(constraints: list[dict[str, Any]], planning_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    actions: set[str] = set()
    ranges: list[dict] = []
    tags: set[str] = set()
    keywords: set[str] = set()
    goal_place_types: set[str] = set()
    protocol_actions: set[str] = set()
    for constraint in constraints:
        rule = constraint.get("hidden_rule", {})
        actions.update(rule.get("forbidden_actions", []))
        ranges.extend(rule.get("forbidden_time_ranges", []))
        tags.update(rule.get("avoid_place_tags", []))
        tags.update(rule.get("formal_business_venue_filter", {}).get("exclude_venue_type", []))
        goal_place_types.update(str(item) for item in rule.get("required_place_types", []) or [])
        effect = rule.get("decision_effect", {}) or {}
        protocol_actions.update(str(item) for item in effect.get("required_action_ids", []) or [])
        if not effect.get("required_action_ids"):
            protocol_actions.update(_protocol_action_forbidden(action) for action in rule.get("forbidden_actions", []) or [])
        keywords.update(constraint.get("keywords", []))
        keywords.update(action.replace("_", " ") for action in rule.get("forbidden_actions", []))
    profile_types = set((planning_profile or {}).get("allowed_primary_types", []) or [])
    if profile_types:
        goal_place_types = profile_types
    return {"forbidden_actions": sorted(actions), "forbidden_time_ranges": ranges,
            "avoid_place_tags": sorted(tags), "goal_place_types": sorted(goal_place_types),
            "required_protocol_actions": sorted(action for action in protocol_actions if action),
            "explanation_keywords": sorted(keywords)}


def _decision_contract(constraints: list[dict[str, Any]], main_pois: list[dict[str, Any]],
                       distractors: list[dict[str, Any]], route_data: dict[str, Any] | None,
                       character: dict[str, Any] | None, operational_state: dict[str, Any] | None,
                       baseline: dict[str, Any], planning_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    """Privately prove which *planning* dimension each rule changes.

    Protocol actions are retained as execution requirements, but they do not
    make a Region planning task material on their own.  Formal admission needs
    a counterfactual change in a place, schedule, or future itinerary dimension.
    """
    baseline_plan = baseline.get("expected_plan", {}) or {}
    material: list[dict[str, Any]] = []
    for index, constraint in enumerate(constraints):
        ablated_constraints = [item for item_idx, item in enumerate(constraints) if item_idx != index]
        ablated = _solve_core(
            ablated_constraints, main_pois, distractors, route_data, character, operational_state, planning_profile,
        )
        ablated_plan = ablated.get("expected_plan", {}) or {}
        changed: list[str] = []
        if list(baseline_plan.get("selected_place_ids", []) or []) != list(ablated_plan.get("selected_place_ids", []) or []):
            changed.append("selected_places")
        if [item.get("start", "") for item in baseline_plan.get("events", []) or []] != [
            item.get("start", "") for item in ablated_plan.get("events", []) or []
        ]:
            changed.append("schedule")
        execution_changed = set(baseline_plan.get("cultural_actions", []) or []) != set(
            ablated_plan.get("cultural_actions", []) or [],
        )
        planning_changed = [
            dimension for dimension in changed
            if dimension in {"selected_places", "schedule", "itinerary_structure", "route", "travel_mode", "duration", "participants", "service_assignment"}
        ]
        if not planning_changed:
            continue
        effect = (constraint.get("hidden_rule", {}) or {}).get("decision_effect", {}) or {}
        material.append({
            "constraint_id": str(constraint.get("id") or ""),
            "dimension": str(effect.get("dimension") or planning_changed[0]),
            "activity": str(effect.get("activity") or "public_outing"),
            "changed_dimensions": planning_changed,
            "execution_requirements_changed": execution_changed,
            "with_constraint": {
                "selected_place_ids": list(baseline_plan.get("selected_place_ids", []) or []),
                "starts": [item.get("start", "") for item in baseline_plan.get("events", []) or []],
                "cultural_actions": list(baseline_plan.get("cultural_actions", []) or []),
            },
            "without_constraint": {
                "selected_place_ids": list(ablated_plan.get("selected_place_ids", []) or []),
                "starts": [item.get("start", "") for item in ablated_plan.get("events", []) or []],
                "cultural_actions": list(ablated_plan.get("cultural_actions", []) or []),
            },
        })
    return {
        "schema_version": "geoword.region_decision_contract.v1",
        "material": bool(material),
        "material_constraints": material,
    }


def region_decision_identity(task_sandbox: dict[str, Any]) -> str:
    """Identify a private Region decision without exposing it in the prompt."""
    profile = task_sandbox.get("planning_profile") or {}
    plan = task_sandbox.get("expected_plan") or {}
    payload = {
        "constraint_ids": sorted(str(value) for value in task_sandbox.get("constraint_ids", []) if value),
        "activity": str(profile.get("activity") or ""),
        "target_primary_type": str(profile.get("target_primary_type") or ""),
        "character_id": str((task_sandbox.get("character") or {}).get("character_id") or ""),
        "selected_place_ids": sorted(str(value) for value in plan.get("selected_place_ids", []) if value),
        "starts": [str(item.get("start") or "") for item in plan.get("events", []) if isinstance(item, dict)],
        "cultural_actions": sorted(str(value) for value in plan.get("cultural_actions", []) if value),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _protocol_action_forbidden(action: Any) -> str:
    value = str(action or "")
    aliases = {
        "eat_with_left_hand": "eat_with_right_hand",
        "stick_chopsticks_vertically_in_rice": "place_chopsticks_horizontally",
        "enter_place_of_worship_without_removing_shoes": "remove_shoes_before_entry",
        "point_at_deity_with_index_finger": "avoid_pointing_at_deities",
        "public_display_of_affection": "avoid_public_display_of_affection",
        "smoke_outside_designated_area": "use_designated_smoking_area_only",
    }
    return aliases.get(value, f"avoid_{value}" if value else "")


def _safe_start(ranges: list[dict[str, Any]]) -> str:
    if not ranges:
        return "10:00"
    ends = [item.get("end", "00:00") for item in ranges if item.get("end")]
    if not ends:
        return "10:00"
    hour, minute = map(int, max(ends).split(":")[:2])
    total = hour * 60 + minute + 30
    return f"{total // 60:02d}:{total % 60:02d}"


def _reachable_from_actor(poi: dict[str, Any], route_data: dict[str, Any], character: dict[str, Any]) -> bool:
    """Require a factual cached actor-to-POI route when route precheck ran."""
    if not route_data:
        return True
    actor_id = character.get("character_id", "")
    if not actor_id:
        return True
    # Character assets conventionally persist their source geometry as
    # home_lat/home_lng.  Treat it as a real location before falling back to
    # legacy nested variants; otherwise empty precheck edges silently become
    # feasible because the solver believes no actor geometry exists.
    location = character.get("location") or character.get("home_location") or {
        "lat": character.get("home_lat"),
        "lng": character.get("home_lng"),
    }
    if not isinstance(location, dict) or location.get("lat") is None or location.get("lng") is None:
        # A source task without any actor geometry cannot claim route-backed
        # feasibility.  This is a construction prerequisite failure, not an
        # assertion that arbitrary candidates are reachable.
        return False
    edge = route_data.get(f"character:{actor_id}->{poi.get('place_id', '')}", {})
    if not edge:
        return False
    for value in edge.values():
        if isinstance(value, dict) and value.get("duration_min", 0) > 0:
            return True
        if isinstance(value, (int, float)) and value > 0:  # legacy route data
            return True
    return False
