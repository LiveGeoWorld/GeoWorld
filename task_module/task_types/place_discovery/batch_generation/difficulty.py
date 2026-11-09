"""Difficulty rubric for generated place_discovery tasks."""

from __future__ import annotations

from typing import Any

LEVEL_TO_SCORE = {"easy": 1, "medium": 2, "hard": 3}
SCORE_TO_LEVEL = {1: "easy", 2: "medium", 3: "hard"}


def build_difficulty_profile(
    *,
    evidence: dict[str, Any],
    prompt_payload: dict[str, Any] | None,
    requested_difficulty: str,
) -> dict[str, Any]:
    service_profile = compute_service_difficulty(evidence)
    task_profile = compute_constraint_difficulty(evidence, prompt_payload or {}, requested_difficulty)
    return combine_difficulty(service_profile, task_profile)


def compute_service_difficulty(evidence: dict[str, Any]) -> dict[str, Any]:
    ambiguity = evidence.get("service_ambiguity", {})
    same_district = int(ambiguity.get("same_district_service_count", 0) or 0)
    nearby = int(ambiguity.get("nearby_same_service_count_900m", 0) or 0)
    same_primary = int(ambiguity.get("same_primary_type_count", 0) or 0)
    attrs = int(ambiguity.get("verifiable_attribute_count", 0) or 0)
    distractors = len(evidence.get("distractors", []))
    score = 0
    score += _bucket(same_district, medium=4, hard=9)
    score += _bucket(nearby, medium=2, hard=5)
    score += _bucket(same_primary, medium=5, hard=12)
    score += 1 if distractors >= 2 else 0
    score += 1 if distractors >= 4 else 0
    score += 1 if attrs >= 5 else 0
    level = _score_level(score, easy_max=2, hard_min=7)
    return {
        "level": level,
        "score": score,
        "factors": {
            "same_district_service_count": same_district,
            "nearby_same_service_count_900m": nearby,
            "same_primary_type_count": same_primary,
            "verifiable_attribute_count": attrs,
            "distractor_count": distractors,
        },
        "rationale": f"服务候选天然歧义为 {level}：同区/近邻/同类候选越多，越需要比较排除。",
    }


def compute_constraint_difficulty(
    evidence: dict[str, Any],
    prompt_payload: dict[str, Any],
    requested_difficulty: str,
) -> dict[str, Any]:
    criteria = prompt_payload.get("ranking_criteria") or []
    hard_constraints = prompt_payload.get("hard_constraints") or []
    soft_preferences = prompt_payload.get("soft_preferences") or []
    distractor_count = len(evidence.get("distractors", []))
    score = LEVEL_TO_SCORE.get(requested_difficulty, 2) - 1
    score += _bucket(len(criteria), medium=4, hard=7)
    score += _bucket(len(hard_constraints), medium=2, hard=4)
    score += _bucket(distractor_count, medium=2, hard=4)
    score -= 1 if len(soft_preferences) <= 2 else 0
    score = max(0, score)
    level = _score_level(score, easy_max=1, hard_min=5)
    return {
        "level": level,
        "score": score,
        "requested_difficulty": requested_difficulty,
        "criteria_count": len(criteria),
        "hard_constraint_count": len(hard_constraints),
        "soft_preference_count": len(soft_preferences),
        "distractor_count": distractor_count,
        "rationale": f"任务输入难度为 {level}：约束越多、越硬、干扰越强，推荐排序越难。",
    }


def combine_difficulty(service_profile: dict[str, Any], task_profile: dict[str, Any]) -> dict[str, Any]:
    service_score = LEVEL_TO_SCORE.get(service_profile.get("level"), 2)
    task_score = LEVEL_TO_SCORE.get(task_profile.get("level"), 2)
    final = round(service_score * 0.6 + task_score * 0.4)
    final = min(3, max(1, final))
    return {
        "difficulty": SCORE_TO_LEVEL[final],
        "service_difficulty": service_profile,
        "constraint_difficulty": task_profile,
        "rubric_version": "place_discovery.difficulty.v1",
    }


def _bucket(value: int, *, medium: int, hard: int) -> int:
    if value >= hard:
        return 2
    if value >= medium:
        return 1
    return 0


def _score_level(score: int, *, easy_max: int, hard_min: int) -> str:
    if score <= easy_max:
        return "easy"
    if score >= hard_min:
        return "hard"
    return "medium"
