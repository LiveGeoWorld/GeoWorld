"""Quality gates for generated place_discovery examples."""

from __future__ import annotations

from typing import Any

from app.runtime.local_service import LocalServiceAppRuntime
from task_module.curated.schema import validate_curated_payload, CuratedValidationError
from task_module.task_types.place_discovery.batch_generation.llm_writer import validate_prompt_payload
from task_module.task_types.place_discovery.batch_generation.schema import build_curated_payload
from task_module.task_types.place_discovery.batch_generation.sampler import _factual_place_aliases


def evaluate_example_quality(example: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    checks = [
        _schema_check(example),
        _prompt_generation_method_check(example),
        _prompt_quality_check(example),
        _gold_detail_check(example, world),
        _top3_ranking_check(example),
        _search_recall_check(example, world),
        _evidence_coverage_check(example),
        _confusable_candidates_check(example),
        _quality_profile_check(example),
        _search_ambiguity_range_check(example),
        _judge_flow_check(example),
        _character_consistency_check(example),
        _discrimination_check(example),
    ]
    passed = all(item["passed"] for item in checks if item.get("severity") == "hard")
    return {
        "passed": passed,
        "checks": checks,
        "hard_failed": [item["name"] for item in checks if item.get("severity") == "hard" and not item["passed"]],
        "soft_failed": [item["name"] for item in checks if item.get("severity") == "soft" and not item["passed"]],
    }


def _schema_check(example: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_curated_payload(
            build_curated_payload(
                city_id=example.get("candidate_scope", {}).get("city_id", ""),
                variant="complex",
                schema_version="v2.curated.place_discovery.batch.v1",
                examples=[example],
            ),
            "place_discovery",
        )
        return {"name": "curated_schema", "passed": True, "severity": "hard", "message": "ok"}
    except CuratedValidationError as exc:
        return {"name": "curated_schema", "passed": False, "severity": "hard", "message": str(exc)}


def _prompt_quality_check(example: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "prompt_language": example.get("prompt_language", ""),
        "user_prompt": example.get("user_prompt", ""),
        "review_translation": example.get("review_translation", {}),
        "hard_constraints": example.get("hard_constraints", []),
        "soft_preferences": example.get("soft_preferences", []),
        "ranking_criteria": example.get("ranking_criteria", []),
        "evaluation": example.get("evaluation", {}),
        "prompt_evidence_map": example.get("prompt_evidence_map", {}),
    }
    issues = validate_prompt_payload(payload, evidence=example.get("evidence_ledger", {}), expected_language=example.get("prompt_language"))
    return {
        "name": "prompt_constraints_and_style",
        "passed": not issues,
        "severity": "hard",
        "message": "; ".join(issues) if issues else "ok",
    }


def _gold_detail_check(example: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    runtime = _runtime(world)
    gold = (example.get("gold", {}).get("candidates") or [{}])[0]
    detail = runtime.call("get_service_detail", {"place_id": gold.get("place_id", "")}).get("detail", {})
    return {
        "name": "gold_service_detail",
        "passed": bool(detail and detail.get("name")),
        "severity": "hard",
        "message": detail.get("name", "missing detail"),
    }


def _top3_ranking_check(example: dict[str, Any]) -> dict[str, Any]:
    gold = example.get("gold", {})
    candidates = gold.get("candidates", [])
    ids = [candidate.get("service_id", "") for candidate in candidates]
    expected_ranks = list(range(1, 4))
    ranks = [candidate.get("rank") for candidate in candidates]
    acceptable_top1 = gold.get("acceptable_top1", [])
    passed = (
        len(candidates) == 3
        and len(set(ids)) == 3
        and ranks == expected_ranks
        and candidates[0].get("service_id") in acceptable_top1
        and len(gold.get("ranking_tiers", [])) == 3
    )
    return {
        "name": "top3_ranking_contract",
        "passed": passed,
        "severity": "hard",
        "message": f"candidate_count={len(candidates)} ranks={ranks}",
    }


def _search_recall_check(example: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    runtime = _runtime(world)
    gold = (example.get("gold", {}).get("candidates") or [{}])[0]
    scope = example.get("candidate_scope", {})
    response = runtime.call("search_services", {
        "city_id": scope.get("city_id", ""),
        "district_id": scope.get("district_id", ""),
        "service_type": example.get("service_type", ""),
        "filters": {"constraints": example.get("ranking_criteria", [])},
        "limit": 100,
    })
    results = response.get("results", [])
    gold_ids = {gold.get("service_id", ""), gold.get("place_id", "")} - {""}
    recalled = any(
        item.get("service_id") in gold_ids
        or item.get("place_id") in gold_ids
        or (gold.get("name") and item.get("name") == gold.get("name"))
        for item in results
    )
    return {
        "name": "service_gold_recall",
        "passed": recalled,
        "severity": "soft",
        "message": f"target={gold.get('name')} recalled={recalled}; result_count={len(results)}",
    }


def _confusable_candidates_check(example: dict[str, Any]) -> dict[str, Any]:
    landscape = example.get("search_landscape") or example.get("evidence_ledger", {}).get("search_landscape", {})
    confusable = landscape.get("confusable_candidates", [])
    return {
        "name": "confusable_candidates_ok",
        "passed": len(confusable) >= 1,
        "severity": "soft",
        "message": f"{len(confusable)} confusable candidates",
    }


def _quality_profile_check(example: dict[str, Any]) -> dict[str, Any]:
    profile = example.get("quality_profile") or example.get("difficulty_profile")
    if not isinstance(profile, dict):
        return {
            "name": "quality_profile_valid",
            "passed": False,
            "severity": "hard",
            "message": "missing quality_profile",
        }
    missing = [
        key
        for key in [
            "search_ambiguity_score",
            "constraint_complexity",
            "rubric_version",
        ]
        if key not in profile
    ]
    return {
        "name": "quality_profile_valid",
        "passed": not missing,
        "severity": "hard",
        "message": f"missing={missing}" if missing else "ok",
    }


def _search_ambiguity_range_check(example: dict[str, Any]) -> dict[str, Any]:
    landscape = example.get("search_landscape") or {}
    score = landscape.get("search_ambiguity_score")
    if score is None:
        profile = example.get("quality_profile") or {}
        score = profile.get("search_ambiguity_score")
    passed = isinstance(score, int) and 2 <= score <= 8
    return {
        "name": "search_ambiguity_range_valid",
        "passed": passed,
        "severity": "hard",
        "message": f"search_ambiguity_score={score}",
    }


def _character_consistency_check(example: dict[str, Any]) -> dict[str, Any]:
    batch_meta = example.get("batch_metadata", {})
    char_constraints = batch_meta.get("character_constraints") or example.get("evidence_ledger", {}).get("character_constraints") or {}
    character_id = example.get("character_id", "")

    if not char_constraints:
        return {
            "name": "character_consistency",
            "passed": True,
            "severity": "hard",
            "message": "no character constraints (character disabled)",
        }

    issues = []
    if not character_id:
        issues.append("character_id empty but character_constraints present")

    satisfied_hard = char_constraints.get("satisfied_hard", [])
    if not satisfied_hard and char_constraints.get("hard_constraints"):
        issues.append("gold hard constraints not recorded in satisfied_hard")

    landscape = example.get("search_landscape") or {}
    confusable = landscape.get("confusable_candidates", [])
    with_reasons = sum(1 for item in confusable if item.get("exclusion_reasons"))
    if confusable and with_reasons < len(confusable):
        issues.append(f"{len(confusable) - with_reasons} confusable candidates lack exclusion_reasons")

    return {
        "name": "character_consistency",
        "passed": not issues,
        "severity": "hard",
        "message": "; ".join(issues) if issues else "ok",
    }


_VAGUE_EXCLUSION_KEYWORDS = ("综合偏好", "弱于", "区域内可见")


def _discrimination_check(example: dict[str, Any]) -> dict[str, Any]:
    """Verify confusable candidates carry concrete exclusion reasons."""
    landscape = example.get("search_landscape") or example.get("evidence_ledger", {}).get("search_landscape", {})
    confusable = landscape.get("confusable_candidates", [])
    if not confusable:
        return {
            "name": "discrimination_check",
            "passed": False,
            "severity": "soft",
            "message": "no confusable candidates",
        }

    concrete_count = 0
    for item in confusable:
        reasons = item.get("exclusion_reasons", [])
        has_concrete = any(
            not any(vague in reason for vague in _VAGUE_EXCLUSION_KEYWORDS)
            for reason in reasons
        )
        if has_concrete:
            concrete_count += 1

    passed = concrete_count >= 1
    return {
        "name": "discrimination_check",
        "passed": passed,
        "severity": "soft",
        "message": f"{concrete_count}/{len(confusable)} candidates have concrete exclusion reasons",
    }


def _prompt_generation_method_check(example: dict[str, Any]) -> dict[str, Any]:
    method = example.get("prompt_generation", {})
    is_llm = method.get("type") == "llm"
    return {
        "name": "llm_prompt_generation",
        "passed": is_llm,
        "severity": "hard" if not method else "soft",
        "message": _json_safe_message(method) if method else "missing prompt_generation metadata",
    }


def _evidence_coverage_check(example: dict[str, Any]) -> dict[str, Any]:
    prompt_map = example.get("prompt_evidence_map", {})
    total = sum(len(value) for value in prompt_map.values() if isinstance(value, list))
    return {
        "name": "prompt_evidence_coverage",
        "passed": total >= 2,
        "severity": "hard",
        "message": f"{total} mapped evidence references",
    }


def _judge_flow_check(example: dict[str, Any]) -> dict[str, Any]:
    flow = example.get("judge_sequence_override", {})
    actions = [
        node.get("agent_action", "")
        for stage in flow.get("stages", [])
        for node in stage.get("nodes", [])
    ]
    required = {
        "LocalServiceApp.search_services",
        "LocalServiceApp.get_service_detail",
        "UserInterfaceApp.send_message_to_user",
    }
    missing = sorted(required - set(actions))
    return {
        "name": "judge_flow_required_actions",
        "passed": not missing,
        "severity": "hard",
        "message": "missing " + ", ".join(missing) if missing else "ok",
    }


def _runtime(world: dict[str, Any]) -> LocalServiceAppRuntime:
    return LocalServiceAppRuntime(
        services=world.get("services", []) or _factual_place_aliases(world),
        places=world.get("places", []),
        cities=world.get("cities", []),
    )


def _json_safe_message(value: dict[str, Any]) -> str:
    return ", ".join(f"{key}={val}" for key, val in value.items() if key != "fallback_reason") or "ok"
