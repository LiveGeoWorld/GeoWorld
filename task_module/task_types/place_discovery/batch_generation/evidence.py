"""Evidence package construction for place_discovery batch generation."""

from __future__ import annotations

from typing import Any

from app.runtime.local_service import LocalServiceAppRuntime
from task_module.task_types.place_discovery.batch_generation.sampler import SampledDiscoveryTarget, _factual_place_aliases


def build_evidence_package(target: SampledDiscoveryTarget, world: dict[str, Any]) -> dict[str, Any]:
    runtime = LocalServiceAppRuntime(
        services=world.get("services", []) or _factual_place_aliases(world),
        places=world.get("places", []),
        cities=world.get("cities", []),
    )
    candidate_details = []
    for service in target.candidate_services:
        detail = runtime.call("get_service_detail", {"service_id": service.get("service_id", "")}).get("detail", {})
        if detail:
            candidate_details.append(detail)
    gold_detail = runtime.call("get_service_detail", {"service_id": target.gold_service.get("service_id", "")}).get("detail", {})
    return {
        "start": target.start,
        "service_type": target.service_type,
        "gold_service": gold_detail,
        "candidate_services": candidate_details,
        "search_landscape": target.search_landscape,
        "distractors": target.distractors,
        "character": _character_evidence(target.character) if target.character else None,
        "character_constraints": target.character_constraints,
        "generation_notes": [
            "Discovery examples are generated from existing Bench world places/services; no synthetic merchant is inserted.",
            "Search ambiguity and confusable candidates are computed from the in-scope service panorama.",
            "Character preferences drive task constraints; the gold service satisfies the character's hard constraints.",
        ],
    }


def _character_evidence(character: dict[str, Any]) -> dict[str, Any]:
    return {
        "character_id": character.get("character_id", ""),
        "name": character.get("name", ""),
        "age": character.get("age"),
        "gender": character.get("gender"),
        "occupation": character.get("occupation", ""),
        "personality_traits": character.get("personality_traits", []),
        "preferences": character.get("preferences", {}),
        "mobility": character.get("mobility", {}),
        "availability": character.get("availability", {}),
        "communication": character.get("communication", {}),
        "constraints": character.get("constraints", []),
    }


def service_reason_lines(detail: dict[str, Any]) -> list[str]:
    lines = []
    if detail.get("district_id"):
        lines.append(f"位于 {detail.get('district_id')}")
    if detail.get("place_type"):
        lines.append(f"类型为 {detail.get('place_type')}")
    if detail.get("rating") is not None:
        lines.append(f"评分 {detail.get('rating')}")
    if detail.get("price_band"):
        lines.append(f"价格档位 {detail.get('price_band')}")
    if detail.get("queue_time_min") is not None:
        lines.append(f"预计排队 {detail.get('queue_time_min')} 分钟")
    if detail.get("crowding_level"):
        lines.append(f"拥挤度 {detail.get('crowding_level')}")
    if detail.get("noise_level"):
        lines.append(f"噪声水平 {detail.get('noise_level')}")
    if detail.get("indoor_available") is not None:
        lines.append("可室内停留" if detail.get("indoor_available") else "偏户外场景")
    return lines[:5]


def prompt_evidence_map_from_criteria(criteria: list[str]) -> dict[str, list[str]]:
    return {
        criterion: ["gold_service", "candidate_services", "service_ambiguity"]
        for criterion in criteria
    }
