"""Public fact projection and LLM prompt writer for Multi-Entity tasks."""

from __future__ import annotations

import json
import re
from typing import Any


SYSTEM_MULTI_ENTITY_NATURAL = """你是多主体地理协同任务的文字编辑。

根据“允许写入题面事实”生成一段自然、清晰的中文用户请求。只能陈述其中出现的参与者、共同活动、地点类型、时间窗、会面时长和协同方式；不要添加任何新约束、职业背景、偏好、候选地点、时间、车辆或路线事实。不要输出标准答案、评分字段、内部 ID、字段名或任何 GT。

只输出 JSON：{"user_prompt": "..."}。"""

_INTERNAL_IDENTIFIER = re.compile(r"\b(?:service|place|char|district)_[A-Za-z0-9_]+\b")


def build_public_fact_projection(
    participants: list[dict[str, Any]],
    venues: list[dict[str, Any]],
    scenario: dict[str, Any],
) -> dict[str, Any]:
    """Return the exact id-free facts allowed in the public task prose.

    These fields are deliberately a subset of the task-static inputs consumed
    by ``solve_coordination``. Free-text character profiles remain source
    material for world construction, not public hard constraints for this
    benchmark episode.
    """
    del venues
    return {
        "participants": [
            {
                "name": str(person.get("name") or ""),
                "departure_area": str(person.get("home_place_name") or ""),
                "available_from": str(person.get("available_from") or ""),
                "must_leave_by": str(person.get("must_leave_by") or ""),
            }
            for person in participants
        ],
        "activity": {
            "activity_type": str(scenario.get("activity_type") or "shared_activity"),
            "required_place_type": str(scenario.get("required_primary_type") or ""),
            "duration_min": max(1, int(scenario.get("meeting_duration_min", 60) or 60)),
            "arrival_mode": "independent",
        },
    }


def build_natural_user_input(
    participants: list[dict[str, Any]], venues: list[dict[str, Any]],
    scenario: dict[str, Any], route_data: dict[str, dict[str, int]],
) -> str:
    """Serialize only solver-backed public facts for the prose-writing LLM.

    ``route_data`` remains part of the construction oracle but is intentionally
    omitted from the prompt. The tested Agent obtains route observations from
    the public MapApp instead of receiving a private construction cache.
    """
    del route_data
    projection = build_public_fact_projection(participants, venues, scenario)
    return "允许写入题面事实：\n" + json.dumps(projection, ensure_ascii=False, indent=2)


def validate_natural_user_prompt(prompt: str, participants: list[dict[str, Any]]) -> list[str]:
    """Reject internal identifiers and direct leakage of raw character profiles."""
    errors: list[str] = []
    text = str(prompt or "")
    if _INTERNAL_IDENTIFIER.search(text):
        errors.append("public prompt contains an internal world identifier")
    if any(token in text for token in ("target_service_id", "expected_place_id", "expected_feasible", "acceptable_plan")):
        errors.append("public prompt contains a private construction field")

    # The LLM no longer receives these fields. This guard protects against an
    # accidental future widening of its input projection or direct copying.
    for person in participants:
        raw_values: list[str] = []
        for constraint in person.get("constraints") or []:
            if isinstance(constraint, dict):
                raw_values.append(str(constraint.get("content") or ""))
            elif isinstance(constraint, str):
                raw_values.append(constraint)
        preferences = person.get("preferences") or {}
        if isinstance(preferences, dict):
            raw_values.extend(str(value) for value in preferences.get("prefer") or [])
            raw_values.extend(str(value) for value in preferences.get("avoid") or [])
        raw_values.extend([str(person.get("occupation") or ""), str(person.get("personality") or "")])
        if any(value and len(value) >= 4 and value in text for value in raw_values):
            errors.append("public prompt leaks an unmodelled character-profile fact")
            break
    return errors
