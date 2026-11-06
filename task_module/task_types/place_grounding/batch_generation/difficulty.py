"""Difficulty rubric for generated place_grounding tasks.

Difficulty has two separate layers:

1. ``place_difficulty``: intrinsic ambiguity of the target in the Bench map
   world, independent of how much information the user prompt gives.
2. ``task_difficulty``: how much useful information the generated task gives to
   the Agent, including information quantity, quality, attribute coverage and
   conflicts.

The final ``difficulty`` is derived from both layers and kept explainable for
review UI and quality gates.
"""

from __future__ import annotations

from typing import Any

from app.runtime.geo import distance_m
from task_module.task_types.place_grounding.batch_generation.sampler import SampledTarget


LEVEL_TO_SCORE = {"easy": 1, "medium": 2, "hard": 3}
SCORE_TO_LEVEL = {1: "easy", 2: "medium", 3: "hard"}


def build_pre_prompt_difficulty_profile(
    *,
    world: dict[str, Any],
    target: SampledTarget,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Build the profile sent to the prompt-writing LLM."""
    place_profile = compute_place_difficulty(world=world, target=target, evidence=evidence)
    task_profile = compute_task_difficulty(
        evidence=evidence,
        prompt_payload={},
        requested_difficulty=target.difficulty,
    )
    return combine_difficulty(place_profile=place_profile, task_profile=task_profile)


def build_final_difficulty_profile(
    *,
    world: dict[str, Any],
    target: SampledTarget,
    evidence: dict[str, Any],
    prompt_payload: dict[str, Any],
) -> dict[str, Any]:
    """Build the final difficulty profile after the prompt is generated."""
    place_profile = compute_place_difficulty(world=world, target=target, evidence=evidence)
    task_profile = compute_task_difficulty(
        evidence=evidence,
        prompt_payload=prompt_payload,
        requested_difficulty=target.difficulty,
    )
    return combine_difficulty(place_profile=place_profile, task_profile=task_profile)


def compute_place_difficulty(
    *,
    world: dict[str, Any],
    target: SampledTarget,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Difficulty caused by the target's map context."""
    place = target.place or {}
    loc = _target_location(target)
    target_type = place.get("primary_type", "")
    district_id = place.get("district_id", "")
    candidates = [
        item
        for item in world.get("places", [])
        if item.get("place_id") and item.get("location") and item.get("place_id") != place.get("place_id")
    ]
    same_type_city = [item for item in candidates if item.get("primary_type") == target_type]
    same_district = [item for item in candidates if district_id and item.get("district_id") == district_id]
    nearby_300 = [item for item in candidates if distance_m(loc, item.get("location", {})) <= 300]
    nearby_800 = [item for item in candidates if distance_m(loc, item.get("location", {})) <= 800]
    same_type_nearby = [item for item in same_type_city if distance_m(loc, item.get("location", {})) <= 1000]
    nearest_same_type_m = _nearest_distance(loc, same_type_city)
    distractor_count = len(evidence.get("distractors", []))

    factors = {
        "same_type_city_count": len(same_type_city),
        "same_district_count": len(same_district),
        "nearby_300m_count": len(nearby_300),
        "nearby_800m_count": len(nearby_800),
        "same_type_nearby_1000m_count": len(same_type_nearby),
        "nearest_same_type_m": round(nearest_same_type_m) if nearest_same_type_m is not None else None,
        "distractor_count": distractor_count,
    }
    score = 0
    score += _bucket(len(same_type_city), medium=8, hard=20)
    score += _bucket(len(same_district), medium=5, hard=12)
    score += _bucket(len(nearby_300), medium=4, hard=8)
    score += _bucket(len(same_type_nearby), medium=2, hard=5)
    score += 2 if nearest_same_type_m is not None and nearest_same_type_m <= 150 else 0
    score += 1 if distractor_count >= 2 else 0
    score += 1 if distractor_count >= 4 else 0
    level = _score_level(score, easy_max=2, hard_min=7)
    return {
        "level": level,
        "score": score,
        "factors": factors,
        "rationale": _place_rationale(level, factors),
    }


def compute_task_difficulty(
    *,
    evidence: dict[str, Any],
    prompt_payload: dict[str, Any],
    requested_difficulty: str,
) -> dict[str, Any]:
    """Difficulty caused by the information given to the Agent."""
    place = evidence.get("place_evidence", {})
    visual_clues = _list(prompt_payload.get("visual_clues"))
    text_clues = _list(prompt_payload.get("text_clues"))
    negative_clues = _list(prompt_payload.get("negative_clues"))
    scope = prompt_payload.get("candidate_scope") if isinstance(prompt_payload.get("candidate_scope"), dict) else {}
    attribute_coverage = _attribute_coverage(evidence, prompt_payload)
    evidence_map = prompt_payload.get("prompt_evidence_map")
    mapped_evidence_count = sum(len(value) for value in evidence_map.values() if isinstance(value, list)) if isinstance(evidence_map, dict) else 0
    info_count = len(visual_clues) + len(text_clues) + len(scope.get("area_hints", []) or []) + len(scope.get("nearby_anchors", []) or [])
    distractor_count = len(evidence.get("distractors", []))

    # More usable information makes the task easier; conflicts and ambiguity make
    # it harder. ``requested_difficulty`` is treated as generation intent, not the
    # final label.
    score = LEVEL_TO_SCORE.get(requested_difficulty, 2) - 1
    score += _bucket(distractor_count, medium=2, hard=4)
    score += _bucket(len(negative_clues), medium=1, hard=3)
    score += 1 if place.get("primary_type") and len(scope.get("allowed_place_types", []) or []) == 0 else 0
    score -= 1 if info_count >= 6 else 0
    score -= 1 if len(attribute_coverage) >= 4 else 0
    score -= 1 if mapped_evidence_count >= 4 else 0
    score += 1 if len(attribute_coverage) <= 2 else 0
    score = max(0, score)
    level = _score_level(score, easy_max=1, hard_min=4)
    return {
        "level": level,
        "score": score,
        "requested_difficulty": requested_difficulty,
        "info_count": info_count,
        "attribute_coverage": sorted(attribute_coverage),
        "info_quality": _info_quality(mapped_evidence_count, len(attribute_coverage), prompt_payload),
        "conflict_level": _conflict_level(distractor_count, len(negative_clues), requested_difficulty),
        "candidate_ambiguity": {
            "distractor_count": distractor_count,
            "same_type_distractors": sum(
                1 for item in evidence.get("distractors", []) if item.get("type") == place.get("primary_type")
            ),
        },
        "mapped_evidence_count": mapped_evidence_count,
        "rationale": _task_rationale(level, info_count, attribute_coverage, distractor_count),
    }


def combine_difficulty(place_profile: dict[str, Any], task_profile: dict[str, Any]) -> dict[str, Any]:
    """Combine place and task layers into a final reviewable profile."""
    place_score = LEVEL_TO_SCORE.get(place_profile.get("level"), 2)
    task_score = LEVEL_TO_SCORE.get(task_profile.get("level"), 2)
    # Place ambiguity matters, but a very informative task can reduce final
    # difficulty; conflict-heavy prompts can raise it.
    combined = round((place_score * 0.55) + (task_score * 0.45))
    combined = min(3, max(1, combined))
    final_level = SCORE_TO_LEVEL[combined]
    return {
        "difficulty": final_level,
        "place_difficulty": place_profile,
        "task_difficulty": task_profile,
        # Backward-compatible fields used by existing review UI.
        "info_count": task_profile.get("info_count", 0),
        "attribute_coverage": task_profile.get("attribute_coverage", []),
        "info_quality": task_profile.get("info_quality", "medium"),
        "conflict_level": task_profile.get("conflict_level", "medium"),
        "candidate_ambiguity": task_profile.get("candidate_ambiguity", {}),
        "rubric_version": "place_grounding.difficulty.v2",
    }


def _target_location(target: SampledTarget) -> dict[str, Any]:
    if target.target_type == "observer_location" and target.character:
        mobility = target.character.get("mobility", {})
        return {"lat": mobility.get("lat"), "lng": mobility.get("lng")}
    return (target.place or {}).get("location", {})


def _attribute_coverage(evidence: dict[str, Any], prompt_payload: dict[str, Any]) -> set[str]:
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
    if place.get("observer"):
        coverage.add("human_context")
    if evidence.get("distractors") or prompt_payload.get("negative_clues"):
        coverage.add("negative_constraint")
    return coverage


def _nearest_distance(loc: dict[str, Any], places: list[dict[str, Any]]) -> float | None:
    distances = [distance_m(loc, item.get("location", {})) for item in places if item.get("location")]
    return min(distances) if distances else None


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


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _info_quality(mapped_count: int, coverage_count: int, prompt_payload: dict[str, Any]) -> str:
    if prompt_payload and mapped_count >= 4 and coverage_count >= 4:
        return "high"
    if mapped_count <= 1 or coverage_count <= 2:
        return "low"
    return "medium"


def _conflict_level(distractor_count: int, negative_count: int, requested: str) -> str:
    if requested == "hard" or distractor_count >= 4 or negative_count >= 3:
        return "high"
    if requested == "medium" or distractor_count >= 2 or negative_count >= 1:
        return "medium"
    return "low"


def _place_rationale(level: str, factors: dict[str, Any]) -> str:
    return (
        f"{level}: same_type_city={factors['same_type_city_count']}, "
        f"same_district={factors['same_district_count']}, "
        f"nearby_300m={factors['nearby_300m_count']}, "
        f"same_type_nearby={factors['same_type_nearby_1000m_count']}"
    )


def _task_rationale(level: str, info_count: int, coverage: set[str], distractor_count: int) -> str:
    return (
        f"{level}: info_count={info_count}, "
        f"attributes={','.join(sorted(coverage))}, "
        f"distractors={distractor_count}"
    )
