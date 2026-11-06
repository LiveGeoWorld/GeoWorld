"""Quality gates for batch-generated grounding examples."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.runtime import MapAppRuntime
from app.runtime.geo import distance_m
from task_module.task_types.place_grounding.batch_generation.config import pipeline_root
from task_module.task_types.place_grounding.batch_generation.llm_writer import validate_prompt_payload
from task_module.task_types.place_grounding.batch_generation.schema import validate_batch_example


def evaluate_example_quality(example: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    """Return pass/fail quality checks for one generated example."""
    checks: list[dict[str, Any]] = []
    checks.extend(_schema_checks(example))
    checks.append(_image_exists_check(example))
    checks.append(_streetview_source_check(example))
    checks.append(_prompt_reference_separation_check(example))
    checks.append(_streetview_location_check(example))
    checks.append(_prompt_leakage_check(example))
    checks.append(_prompt_generation_method_check(example))
    checks.append(_prompt_quality_check(example))
    checks.append(_quality_profile_check(example))
    checks.append(_ambiguity_range_check(example))
    checks.append(_evidence_coverage_check(example))
    checks.append(_confusable_candidates_check(example))
    checks.append(_judge_flow_check(example))
    checks.append(_mapapp_recall_check(example, world))
    checks.append(_mapapp_named_anchor_recall_check(example, world))
    passed = all(item["passed"] for item in checks if item.get("severity") == "hard")
    return {
        "passed": passed,
        "checks": checks,
        "hard_failed": [item["name"] for item in checks if item.get("severity") == "hard" and not item["passed"]],
        "soft_failed": [item["name"] for item in checks if item.get("severity") == "soft" and not item["passed"]],
    }


def _schema_checks(example: dict[str, Any]) -> list[dict[str, Any]]:
    issues = validate_batch_example(example)
    return [
        {
            "name": "batch_schema",
            "passed": not issues,
            "severity": "hard",
            "message": "; ".join(issues) if issues else "ok",
        }
    ]


def _image_exists_check(example: dict[str, Any]) -> dict[str, Any]:
    raw = example.get("image_path", "")
    if not raw:
        return {
            "name": "image_exists",
            "passed": False,
            "severity": "hard",
            "message": "missing image_path",
        }
    path = Path(raw)
    resolved = path if path.is_absolute() else pipeline_root() / path
    return {
        "name": "image_exists",
        "passed": resolved.is_file(),
        "severity": "hard",
        "message": str(resolved),
    }


def _streetview_source_check(example: dict[str, Any]) -> dict[str, Any]:
    assets = _image_assets(example)
    valid = [
        asset
        for asset in assets
        if asset.get("source") == "google_streetview_static"
        and asset.get("path")
        and asset.get("quality") != "fetch_failed"
    ]
    return {
        "name": "google_streetview_source",
        "passed": bool(valid),
        "severity": "hard",
        "message": f"{len(valid)}/{len(assets)} valid Google Street View assets",
    }


def _prompt_reference_separation_check(example: dict[str, Any]) -> dict[str, Any]:
    assets = _image_assets(example)
    evaluation_path = example.get("image_path", "")
    reference_paths = [
        asset.get("path", "")
        for asset in assets
        if asset.get("role") == "prompt_reference"
        and asset.get("path")
        and asset.get("quality") != "fetch_failed"
    ]
    distinct = [path for path in reference_paths if path and path != evaluation_path]
    if not evaluation_path:
        message = "missing evaluation image_path"
    elif not reference_paths:
        message = "missing prompt_reference image asset"
    elif not distinct:
        message = "prompt_reference image must differ from evaluation image_path"
    else:
        message = f"evaluation={evaluation_path}; prompt_reference={distinct[0]}"
    return {
        "name": "prompt_reference_image_separation",
        "passed": bool(evaluation_path and distinct),
        "severity": "hard",
        "message": message,
    }


def _streetview_location_check(example: dict[str, Any]) -> dict[str, Any]:
    assets = _image_assets(example)
    distances: list[float] = []
    for asset in assets:
        pano = (asset.get("viewpoint") or {}).get("pano_location") or {}
        gold = asset.get("gold_coordinate") or (example.get("gold") or {}).get("coordinate") or {}
        if pano and gold:
            distances.append(distance_m(gold, pano))
    if not distances:
        return {
            "name": "streetview_gold_distance",
            "passed": False,
            "severity": "hard",
            "message": "missing pano/gold coordinate metadata",
        }
    min_distance = min(distances)
    return {
        "name": "streetview_gold_distance",
        "passed": min_distance <= 80,
        "severity": "hard",
        "message": f"nearest pano distance={round(min_distance)}m",
    }


def _prompt_leakage_check(example: dict[str, Any]) -> dict[str, Any]:
    prompt = str(example.get("user_prompt", ""))
    gold_name = str(example.get("gold", {}).get("place_name", ""))
    granularity = example.get("acceptable_granularity", "")
    allowed = granularity != "exact_poi"
    leaked = bool(gold_name and gold_name in prompt)
    return {
        "name": "prompt_gold_name_leakage",
        "passed": allowed or not leaked,
        "severity": "soft" if allowed else "hard",
        "message": "gold name present" if leaked else "ok",
    }


def _prompt_generation_method_check(example: dict[str, Any]) -> dict[str, Any]:
    method = example.get("prompt_generation") or {}
    is_llm = method.get("type") == "llm"
    return {
        "name": "llm_prompt_generation",
        "passed": is_llm,
        "severity": "hard" if not method else "soft",
        "message": json_safe_message(method) if method else "missing prompt_generation metadata",
    }


def _prompt_quality_check(example: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "prompt_language": example.get("prompt_language", ""),
        "user_prompt": example.get("user_prompt", ""),
        "review_translation": example.get("review_translation", {}),
        "visual_clues": example.get("visual_clues", []),
        "text_clues": example.get("text_clues", []),
        "negative_clues": example.get("negative_clues", []),
        "grounding_constraints": example.get("grounding_constraints", []),
        "candidate_scope": example.get("candidate_scope", {}),
        "evaluation": example.get("evaluation", {}),
        "prompt_evidence_map": example.get("prompt_evidence_map", {}),
    }
    issues = validate_prompt_payload(
        payload,
        evidence=example.get("evidence_ledger", {}),
        allow_gold_name=False,
        expected_language=example.get("prompt_language") or None,
        review_translation_language=example.get("review_language") or None,
    )
    return {
        "name": "prompt_style_and_evidence",
        "passed": not issues,
        "severity": "hard",
        "message": "; ".join(issues) if issues else "ok",
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
        for key in ["ambiguity_score", "complexity_score", "display_difficulty", "rubric_version"]
        if key not in profile
    ]
    mismatch = example.get("difficulty") != profile.get("display_difficulty")
    return {
        "name": "quality_profile_valid",
        "passed": not missing and not mismatch,
        "severity": "hard",
        "message": (
            f"missing={missing}; example={example.get('difficulty')} profile={profile.get('display_difficulty')}"
            if missing or mismatch
            else "ok"
        ),
    }


def _ambiguity_range_check(example: dict[str, Any]) -> dict[str, Any]:
    landscape = example.get("ambiguity_landscape") or {}
    score = landscape.get("ambiguity_score")
    if score is None:
        profile = example.get("quality_profile") or {}
        score = profile.get("ambiguity_score")
    passed = isinstance(score, int) and 2 <= score <= 8
    return {
        "name": "ambiguity_range_valid",
        "passed": passed,
        "severity": "hard",
        "message": f"ambiguity_score={score}",
    }


def _confusable_candidates_check(example: dict[str, Any]) -> dict[str, Any]:
    landscape = example.get("ambiguity_landscape") or example.get("evidence_ledger", {}).get("ambiguity_landscape", {})
    confusable = landscape.get("confusable_candidates", [])
    return {
        "name": "confusable_candidates_ok",
        "passed": len(confusable) >= 2,
        "severity": "soft",
        "message": f"{len(confusable)} confusable candidates",
    }

def _judge_flow_check(example: dict[str, Any]) -> dict[str, Any]:
    flow = example.get("judge_sequence_override", {})
    actions = [
        node.get("agent_action", "")
        for stage in flow.get("stages", [])
        for node in stage.get("nodes", [])
    ]
    required = {
        "MapApp.search_grounding_candidates",
        "MapApp.select_grounded_location",
        "UserInterfaceApp.send_message_to_user",
    }
    missing = sorted(required - set(actions))
    return {
        "name": "judge_flow_required_actions",
        "passed": not missing,
        "severity": "hard",
        "message": "missing " + ", ".join(missing) if missing else "ok",
    }


def _mapapp_recall_check(example: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    gold = example.get("gold", {})
    target = gold.get("candidate_id") or gold.get("place_id")
    cities = world.get("cities", [])
    runtime = MapAppRuntime(places=world.get("places", []), characters=[], cities=cities)
    scope = example.get("candidate_scope", {})
    args = {
        "keyword": (gold.get("anchor_places") or [gold.get("place_name", "")])[0],
        "city_id": scope.get("city_id", gold.get("city_id", "")),
        "limit": 20,
    }
    if scope.get("allowed_place_types"):
        args["category"] = scope["allowed_place_types"][0]
    if scope.get("area_hints"):
        args["area_hint"] = scope["area_hints"][0]
    response = runtime.call("search_grounding_candidates", args)
    candidate_ids = [item.get("candidate_id") for item in response.get("candidates", [])]
    return {
        "name": "mapapp_gold_recall",
        "passed": target in candidate_ids if target else False,
        "severity": "soft",
        "message": f"target={target}; recalled={target in candidate_ids if target else False}",
    }


def _mapapp_named_anchor_recall_check(example: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    """Require every structured named POI clue to be publicly searchable."""
    gold = example.get("gold", {})
    anchors = list(dict.fromkeys(
        str(name).strip() for name in gold.get("anchor_places", []) if str(name or "").strip()
    ))
    if not anchors:
        return {
            "name": "mapapp_named_anchor_recall",
            "passed": False,
            "severity": "hard",
            "message": "missing gold.anchor_places",
        }

    runtime = MapAppRuntime(
        places=world.get("places", []),
        characters=[],
        cities=world.get("cities", []),
    )
    city_id = str((example.get("candidate_scope") or {}).get("city_id") or gold.get("city_id") or "")
    missing = []
    for anchor in anchors:
        response = runtime.call("search_grounding_candidates", {
            "keyword": anchor,
            "city_id": city_id,
            "limit": 20,
        })
        if not response.get("candidates"):
            missing.append(anchor)
    return {
        "name": "mapapp_named_anchor_recall",
        "passed": not missing,
        "severity": "hard",
        "message": "ok" if not missing else "unsearchable named anchors: " + ", ".join(missing),
    }


def _image_assets(example: dict[str, Any]) -> list[dict[str, Any]]:
    assets = (example.get("evidence_ledger") or {}).get("image_assets", [])
    return assets if isinstance(assets, list) else []


def json_safe_message(value: dict[str, Any]) -> str:
    return ", ".join(f"{key}={val}" for key, val in value.items() if key != "fallback_reason") or "ok"
