"""Objective quality profiles for place_grounding tasks."""

from __future__ import annotations

from typing import Any

from task_module.task_types.place_grounding.batch_generation.ambiguity import display_difficulty_from_ambiguity
from task_module.task_types.place_grounding.batch_generation.sampler import SampledTarget

RUBRIC_VERSION = "place_grounding.quality.v1"


def build_pre_prompt_quality_profile(
    *,
    target: SampledTarget,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    landscape = target.ambiguity_landscape or evidence.get("ambiguity_landscape", {})
    ambiguity_score = int(landscape.get("ambiguity_score", 0))
    complexity_pre = _complexity_pre(evidence)
    return _assemble_quality_profile(
        ambiguity_score=ambiguity_score,
        landscape=landscape,
        complexity_score=complexity_pre,
        complexity_stage="pre",
        prompt_payload={},
        evidence=evidence,
    )


def build_final_quality_profile(
    *,
    target: SampledTarget,
    evidence: dict[str, Any],
    prompt_payload: dict[str, Any],
) -> dict[str, Any]:
    landscape = target.ambiguity_landscape or evidence.get("ambiguity_landscape", {})
    ambiguity_score = int(landscape.get("ambiguity_score", 0))
    complexity_post = _complexity_post(evidence, prompt_payload)
    return _assemble_quality_profile(
        ambiguity_score=ambiguity_score,
        landscape=landscape,
        complexity_score=complexity_post,
        complexity_stage="post",
        prompt_payload=prompt_payload,
        evidence=evidence,
    )


def _assemble_quality_profile(
    *,
    ambiguity_score: int,
    landscape: dict[str, Any],
    complexity_score: int,
    complexity_stage: str,
    prompt_payload: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    display_difficulty = display_difficulty_from_ambiguity(ambiguity_score)
    confusable_count = len(landscape.get("confusable_candidates", []))
    place_ambiguity = {
        "level": display_difficulty,
        "score": ambiguity_score,
        "factors": {
            "same_type_within_300m": landscape.get("same_type_within_300m", 0),
            "same_type_within_500m": landscape.get("same_type_within_500m", 0),
            "same_type_within_1000m": landscape.get("same_type_within_1000m", 0),
            "all_places_within_500m": landscape.get("all_places_within_500m", 0),
            "nearest_same_type_m": landscape.get("nearest_same_type_m"),
            "confusable_candidate_count": confusable_count,
        },
    }
    task_complexity = {
        "level": _complexity_level(complexity_score),
        "score": complexity_score,
        "stage": complexity_stage,
        "mapped_evidence_count": _mapped_evidence_count(prompt_payload),
        "attribute_coverage": sorted(_attribute_coverage(evidence=evidence, prompt_payload=prompt_payload)),
    }
    profile: dict[str, Any] = {
        "ambiguity_score": ambiguity_score,
        "complexity_score": complexity_score,
        "display_difficulty": display_difficulty,
        "place_ambiguity": place_ambiguity,
        "rubric_version": RUBRIC_VERSION,
        "difficulty": display_difficulty,
        "info_count": _info_count(prompt_payload),
        "attribute_coverage": task_complexity["attribute_coverage"],
        "confusable_candidate_count": confusable_count,
    }
    if complexity_stage == "pre":
        profile["task_complexity_pre"] = task_complexity
    else:
        profile["task_complexity_post"] = task_complexity
    return profile


def _complexity_pre(evidence: dict[str, Any]) -> int:
    score = 0
    assets = [a for a in evidence.get("image_assets", []) if a.get("quality") != "fetch_failed"]
    score += min(3, len(assets))
    place = evidence.get("place_evidence", {})
    if place.get("primary_type"):
        score += 1
    if place.get("district_id"):
        score += 1
    score += min(2, len(place.get("nearby_anchors", [])))
    landscape = evidence.get("ambiguity_landscape", {})
    score += 1 if landscape.get("confusable_candidates") else 0
    return min(10, max(0, score))


def _complexity_post(evidence: dict[str, Any], prompt_payload: dict[str, Any]) -> int:
    score = _complexity_pre(evidence)
    score += min(3, len(_list(prompt_payload.get("visual_clues"))))
    score += min(3, len(_list(prompt_payload.get("text_clues"))))
    score += min(2, len(_list(prompt_payload.get("negative_clues"))))
    score += min(2, _mapped_evidence_count(prompt_payload) // 2)
    scope = prompt_payload.get("candidate_scope") if isinstance(prompt_payload.get("candidate_scope"), dict) else {}
    score += min(1, len(scope.get("area_hints", []) or []))
    return min(10, max(0, score))


def _complexity_level(score: int) -> str:
    if score <= 3:
        return "low"
    if score <= 6:
        return "medium"
    return "high"


def _mapped_evidence_count(prompt_payload: dict[str, Any]) -> int:
    prompt_map = prompt_payload.get("prompt_evidence_map")
    if not isinstance(prompt_map, dict):
        return 0
    return sum(len(value) for value in prompt_map.values() if isinstance(value, list))


def _info_count(prompt_payload: dict[str, Any]) -> int:
    scope = prompt_payload.get("candidate_scope") if isinstance(prompt_payload.get("candidate_scope"), dict) else {}
    return (
        len(_list(prompt_payload.get("visual_clues")))
        + len(_list(prompt_payload.get("text_clues")))
        + len(scope.get("area_hints", []) or [])
        + len(scope.get("nearby_anchors", []) or [])
    )


def _attribute_coverage(*, evidence: dict[str, Any], prompt_payload: dict[str, Any]) -> set[str]:
    place = evidence.get("place_evidence", {})
    scope = prompt_payload.get("candidate_scope") if isinstance(prompt_payload.get("candidate_scope"), dict) else {}
    coverage: set[str] = set()
    if prompt_payload.get("visual_clues") or evidence.get("image_assets"):
        coverage.add("appearance")
    if place.get("types") or scope.get("allowed_place_types"):
        coverage.add("place_type")
    if place.get("district_id") or scope.get("area_hints"):
        coverage.add("district")
    if place.get("address") or scope.get("address_hints"):
        coverage.add("address_hint")
    if place.get("nearby_anchors") or scope.get("nearby_anchors"):
        coverage.add("nearby_anchor")
    if prompt_payload.get("negative_clues"):
        coverage.add("negative_constraint")
    return coverage


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
