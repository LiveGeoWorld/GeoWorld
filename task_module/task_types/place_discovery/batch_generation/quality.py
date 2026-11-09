"""Objective quality profiles for place_discovery tasks."""

from __future__ import annotations

from typing import Any

from task_module.task_types.place_discovery.batch_generation.sampler import SampledDiscoveryTarget

RUBRIC_VERSION = "place_discovery.quality.v1"


def legacy_difficulty_label(search_ambiguity_score: int) -> str:
    """Map search_ambiguity_score to easy/medium/hard for curated schema compatibility only."""
    if search_ambiguity_score <= 3:
        return "easy"
    if search_ambiguity_score <= 6:
        return "medium"
    return "hard"


def build_pre_prompt_quality_profile(
    *,
    target: SampledDiscoveryTarget,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    landscape = target.search_landscape or evidence.get("search_landscape", {})
    search_ambiguity = int(landscape.get("search_ambiguity_score", 0))
    constraint_pre = _constraint_complexity_pre(evidence, target)
    return _assemble_quality_profile(
        search_ambiguity_score=search_ambiguity,
        landscape=landscape,
        constraint_complexity=constraint_pre,
        complexity_stage="pre",
        prompt_payload={},
        evidence=evidence,
        target=target,
    )


def build_final_quality_profile(
    *,
    target: SampledDiscoveryTarget,
    evidence: dict[str, Any],
    prompt_payload: dict[str, Any],
) -> dict[str, Any]:
    landscape = target.search_landscape or evidence.get("search_landscape", {})
    search_ambiguity = int(landscape.get("search_ambiguity_score", 0))
    constraint_post = _constraint_complexity_post(evidence, prompt_payload, target)
    return _assemble_quality_profile(
        search_ambiguity_score=search_ambiguity,
        landscape=landscape,
        constraint_complexity=constraint_post,
        complexity_stage="post",
        prompt_payload=prompt_payload,
        evidence=evidence,
        target=target,
    )


def _assemble_quality_profile(
    *,
    search_ambiguity_score: int,
    landscape: dict[str, Any],
    constraint_complexity: int,
    complexity_stage: str,
    prompt_payload: dict[str, Any],
    evidence: dict[str, Any],
    target: SampledDiscoveryTarget,
) -> dict[str, Any]:
    char_constraints = evidence.get("character_constraints") or target.character_constraints or {}
    hard_count = len(char_constraints.get("hard_constraints", []))
    soft_count = len(char_constraints.get("soft_preferences", []))
    confusable_count = len(landscape.get("confusable_candidates", []))

    search_profile = {
        "level": _search_ambiguity_level(search_ambiguity_score),
        "score": search_ambiguity_score,
        "factors": {
            "same_district_service_count": landscape.get("same_district_service_count", 0),
            "nearby_same_service_count_900m": landscape.get("nearby_same_service_count_900m", 0),
            "same_primary_type_count": landscape.get("same_primary_type_count", 0),
            "verifiable_attribute_count": landscape.get("verifiable_attribute_count", 0),
            "confusable_candidate_count": confusable_count,
            "candidates_in_scope_count": len(landscape.get("candidates_in_scope", [])),
        },
    }
    constraint_profile = {
        "level": _constraint_level(constraint_complexity),
        "score": constraint_complexity,
        "stage": complexity_stage,
        "character_hard_constraint_count": hard_count,
        "character_soft_preference_count": soft_count,
        "criteria_count": len(_list(prompt_payload.get("ranking_criteria"))),
        "hard_constraint_count": len(_list(prompt_payload.get("hard_constraints"))),
        "soft_preference_count": len(_list(prompt_payload.get("soft_preferences"))),
    }

    profile: dict[str, Any] = {
        "search_ambiguity_score": search_ambiguity_score,
        "constraint_complexity": constraint_complexity,
        "search_ambiguity": search_profile,
        "character_hard_constraint_count": hard_count,
        "character_soft_preference_count": soft_count,
        "confusable_candidate_count": confusable_count,
        "rubric_version": RUBRIC_VERSION,
    }
    if complexity_stage == "pre":
        profile["constraint_complexity_pre"] = constraint_profile
    else:
        profile["constraint_complexity_post"] = constraint_profile
    return profile


def _search_ambiguity_level(score: int) -> str:
    if score <= 3:
        return "low"
    if score <= 6:
        return "medium"
    return "high"


def _constraint_complexity_pre(evidence: dict[str, Any], target: SampledDiscoveryTarget) -> int:
    score = 0
    char_constraints = evidence.get("character_constraints") or target.character_constraints or {}
    score += min(3, len(char_constraints.get("hard_constraints", [])))
    score += min(3, len(char_constraints.get("soft_preferences", [])))
    if target.character:
        score += 1
    landscape = evidence.get("search_landscape") or target.search_landscape or {}
    score += min(2, len(landscape.get("confusable_candidates", [])) // 2)
    return min(10, max(0, score))


def _constraint_complexity_post(
    evidence: dict[str, Any],
    prompt_payload: dict[str, Any],
    target: SampledDiscoveryTarget,
) -> int:
    score = _constraint_complexity_pre(evidence, target)
    score += min(3, len(_list(prompt_payload.get("hard_constraints"))))
    score += min(3, len(_list(prompt_payload.get("soft_preferences"))))
    score += min(2, len(_list(prompt_payload.get("ranking_criteria"))))
    return min(10, max(0, score))


def _constraint_level(score: int) -> str:
    if score <= 3:
        return "low"
    if score <= 6:
        return "medium"
    return "high"


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
