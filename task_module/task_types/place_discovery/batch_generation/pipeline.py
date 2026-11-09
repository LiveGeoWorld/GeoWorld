"""End-to-end batch generation pipeline for place_discovery drafts."""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.io import read_json, write_json
from task_module.curated.exporter import _resolve_judge_payload
from task_module.task_types.place_discovery.batch_generation.config import (
    DiscoveryBatchGenerationConfig,
    as_jsonable_config,
    infer_characters_path,
    infer_world_path,
)
from task_module.task_types.place_discovery.batch_generation.evidence import build_evidence_package, service_reason_lines
from task_module.task_types.place_discovery.batch_generation.llm_writer import (
    deterministic_write_discovery_prompt,
    write_discovery_prompt_with_llm,
)
from task_module.task_types.place_discovery.batch_generation.quality import (
    build_final_quality_profile,
    build_pre_prompt_quality_profile,
    legacy_difficulty_label,
)
from task_module.task_types.place_discovery.batch_generation.quality_gate import evaluate_example_quality
from task_module.task_types.place_discovery.batch_generation.sampler import (
    SampledDiscoveryTarget,
    sample_targets,
)
from task_module.task_types.place_discovery.batch_generation.schema import build_curated_payload


def weighted_city(config, worlds, task_index):
    from .sampler import weighted_choice
    return weighted_choice({c: config.city_weights.get(c, 1.0) for c in sorted(worlds)},
                           random.Random(f"{config.seed}|city|{task_index}"))


def generate_batch(config: DiscoveryBatchGenerationConfig) -> dict[str, Any]:
    worlds = _load_worlds(config)
    characters_by_city = _load_characters(config)
    examples, failures, quality_reports = generate_discovery_examples(
        config=config,
        worlds=worlds,
        characters_by_city=characters_by_city,
        count=config.count,
        require_quality_pass=False,
        max_attempts_per_slot=1,
    )

    output_dir = config.output_dir
    generated_at = datetime.now(timezone.utc).isoformat()
    if examples:
        generated_at = examples[0].get("batch_metadata", {}).get("generated_at", generated_at)
    write_json(
        output_dir / "drafts.json",
        {
            "schema_version": config.schema_version,
            "task_type": "place_discovery",
            "generated_at": generated_at,
            "config": as_jsonable_config(config),
            "examples": examples,
        },
    )
    curated_paths = _write_curated_by_city(config, examples)
    report = {
        "generated_at": generated_at,
        "count": len(examples),
        "output_dir": str(output_dir),
        "curated_paths": curated_paths,
        "quality": quality_reports,
        "passed_count": sum(1 for item in quality_reports if item["passed"]),
        "failure_count": len(failures),
        "failures": failures,
    }
    write_json(output_dir / "generation_report.json", report)
    return report


def generate_discovery_examples(
    *,
    config: DiscoveryBatchGenerationConfig,
    worlds: dict[str, dict[str, Any]],
    characters_by_city: dict[str, dict[str, Any]],
    count: int,
    require_quality_pass: bool = True,
    max_attempts_per_slot: int = 3,
    start_index: int = 1,
    excluded_service_ids: set[str] | None = None,
    cost_slot_callback: Any = None,
    attempt_rng_callback: Any = None,
    route_cache: Any = None,
    maps_client: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Generate discovery examples from world data with optional quality filtering."""
    rng = random.Random(config.seed)
    generated_at = datetime.now(timezone.utc).isoformat()
    examples: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    quality_reports: list[dict[str, Any]] = []
    reserved_service_ids = set(excluded_service_ids or set())
    from .factual_construction import sample_target, compute_contract, write_selected_prompt
    from shared.maps_client import GoogleMapsClient
    from shared.config_loader import PipelineConfig
    from task_module._shared.route_cache import RouteCache
    from task_module._shared.batch_state import task_seed, attempt_seed
    caches = {}
    client = maps_client
    attempt_numbers = {}

    max_slots = count * 3
    slot = 0
    while len(examples) < count and slot < max_slots:
        slot += 1
        if cost_slot_callback is not None:
            cost_slot_callback(slot)
        success = False
        for attempt in range(1, max_attempts_per_slot + 1):
            task_index = start_index + len(examples)
            if task_index not in attempt_numbers:
                prior = config.output_dir / "attempts" / f"task_{task_index:04d}"
                attempt_numbers[task_index] = max(
                    (int(p.name.split('_')[1]) for p in prior.glob('attempt_*') if p.is_dir()), default=0)
            attempt_numbers[task_index] += 1
            attempt = attempt_numbers[task_index]
            if attempt_rng_callback is not None:
                rng = attempt_rng_callback(start_index + len(examples), attempt)
            else:
                # Standalone diagnostics also derive per-task/attempt states;
                # do not share a mutable batch RNG across successful tasks.
                city_id = sorted(worlds)[0] if len(worlds) == 1 else weighted_city(config, worlds, task_index)
                rng = random.Random(attempt_seed(task_seed(config.seed, "", city_id, "place_discovery", task_index), attempt))
            index = start_index + len(examples)
            audit_root = config.output_dir / "attempts" / f"task_{index:04d}" / f"attempt_{attempt:04d}"
            write_json(audit_root / "reservation.json", {"batch_seed": config.seed,
                "task_index": task_index, "attempt_index": attempt,
                "status": "reserved", "rng_state": repr(rng.getstate())})
            try:
                if attempt_rng_callback is not None:
                    city_id = sorted(worlds)[0] if len(worlds) == 1 else weighted_city(config, worlds, task_index)
                world = worlds[city_id]
                target, pool = sample_target(world, characters_by_city[city_id], rng, reserved_service_ids)
                cache = route_cache
                if cache is None:
                    if city_id not in caches:
                        caches[city_id] = RouteCache(city_id)
                    cache = caches[city_id]
                if client is None:
                    client = GoogleMapsClient(PipelineConfig(config.config_yaml))
                contract = compute_contract(world, target, pool, cache, client, rng)
                write_json(audit_root / "construction.json", contract)
                output, selected, audit = write_selected_prompt(contract=contract, target=target,
                    config=config, audit_path=audit_root / "writer.json")
                evidence = build_evidence_package(target, world)
                contract["selected_option_id"] = selected["option_id"]
                contract["selected_constraints"] = [*selected["base_constraints"], *selected["constraints"]]
                contract["constraint_spans"] = output["constraint_spans"]
                evidence["construction_contract"] = contract
                evidence["generation_notes"] = ["Script computes C from frozen facts; writer selects a verified option and narrates it."]
                payload = {"prompt_language": "en", "user_prompt": output["user_prompt"],
                    "hard_constraints": output["constraint_spans"], "soft_preferences": [],
                    "ranking_criteria": output["constraint_spans"], "evaluation": {},
                    "prompt_evidence_map": {span: [f"construction_contract.selected_constraints.{i}"]
                                            for i, span in enumerate(output["constraint_spans"])},
                    "generation_method": {"type": "llm", "contract": "discovery.factual_C.v2", "audit": audit}}
                profile = build_final_quality_profile(target=target, evidence=evidence, prompt_payload=payload)
                example = _build_example(target=target, evidence=evidence, prompt_payload=payload,
                    quality_profile=profile, index=index, generated_at=generated_at, review_status=config.review_status)
                for review_field in ("review_language", "user_prompt_translation", "review_translation"):
                    example.pop(review_field, None)
                from .quality_gate import _schema_check, _top3_ranking_check
                checks = [_schema_check(example), _top3_ranking_check(example)]
                checks.append({"name": "selected_C_unique", "severity": "hard",
                               "passed": selected["matching_place_ids"] == [target.gold_place["place_id"]]})
                quality = {"passed": all(c["passed"] for c in checks), "checks": checks,
                           "hard_failed": [c["name"] for c in checks if not c["passed"]], "soft_failed": []}
                example["quality_gate"] = quality
                example["selected_constraints"] = contract["selected_constraints"]
                write_json(audit_root / "construction.json", contract)
                write_json(audit_root / "example.json", example)
                write_json(audit_root / "quality.json", quality)
            except Exception as exc:  # noqa: BLE001
                failures.append({
                    "slot": slot,
                    "attempt": attempt,
                    "city_id": city_id,
                    "reason": "generation_error",
                    "detail": str(exc),
                })
                write_json(audit_root / "failure.json", failures[-1])
                continue

            quality_reports.append({
                "example_id": example["example_id"],
                "city_id": target.city_id,
                "passed": quality["passed"],
                "hard_failed": quality["hard_failed"],
                "soft_failed": quality["soft_failed"],
            })
            if require_quality_pass and not quality["passed"]:
                failures.append({
                    "slot": slot,
                    "attempt": attempt,
                    "example_id": example["example_id"],
                    "city_id": target.city_id,
                    "place_name": target.gold_place.get("name", ""),
                    "reason": "quality_gate_failed",
                    "hard_failed": quality["hard_failed"],
                })
                continue

            examples.append(example)
            example["_seed_lineage"] = {
                "task_index": start_index + len(examples) - 1, "attempt_index": attempt,
            }
            example["_cost_generation_unit_id"] = f"slot_{slot:04d}"
            gold_service_id = ((example.get("gold") or {}).get("candidates") or [{}])[0].get("service_id", "")
            if gold_service_id:
                reserved_service_ids.add(str(gold_service_id))
            # Evidence is constructed before canonical task folders are
            # exported; publish a truthful intermediate progress signal.
            print(
                f"[BuildProgress] phase=evidence accepted={len(examples)} requested={count}",
                flush=True,
            )
            success = True
            break

        if not success:
            failures.append({
                "slot": slot,
                "attempt": max_attempts_per_slot,
                "reason": "max_retries_exhausted",
            })

    return examples, failures, quality_reports


def _generate_example_for_target(
    *,
    config: DiscoveryBatchGenerationConfig,
    target: SampledDiscoveryTarget,
    world: dict[str, Any],
    index: int,
    generated_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = build_evidence_package(target, world)
    pre_quality_profile = build_pre_prompt_quality_profile(target=target, evidence=evidence)
    prompt_payload = _write_prompt_payload(
        config=config,
        target=target,
        evidence=evidence,
        quality_profile=pre_quality_profile,
        index=index,
    )
    final_quality_profile = build_final_quality_profile(
        target=target,
        evidence=evidence,
        prompt_payload=prompt_payload,
    )
    prompt_payload["quality_profile"] = final_quality_profile
    example = _build_example(
        target=target,
        evidence=evidence,
        prompt_payload=prompt_payload,
        quality_profile=final_quality_profile,
        index=index,
        generated_at=generated_at,
        review_status=config.review_status,
    )
    quality = evaluate_example_quality(example, world)
    example["quality_gate"] = quality
    return example, quality


def _load_worlds(config: DiscoveryBatchGenerationConfig) -> dict[str, dict[str, Any]]:
    return {city_id: read_json(infer_world_path(city_id)) for city_id in config.city_weights}


def _load_characters(config: DiscoveryBatchGenerationConfig) -> dict[str, dict[str, Any]]:
    characters: dict[str, dict[str, Any]] = {}
    for city_id in config.city_weights:
        path = infer_characters_path(city_id, config.variant)
        characters[city_id] = read_json(path) if path and path.exists() else {"characters": []}
    return characters


def _write_prompt_payload(
    *,
    config: DiscoveryBatchGenerationConfig,
    target: SampledDiscoveryTarget,
    evidence: dict[str, Any],
    quality_profile: dict[str, Any],
    index: int = 1,
) -> dict[str, Any]:
    prompt_language = config.language_for_city(target.city_id)
    if config.use_llm_prompt:
        try:
            return write_discovery_prompt_with_llm(
                evidence=evidence,
                quality_profile=quality_profile,
                config_yaml=config.config_yaml,
                stage=config.prompt_model_stage,
                temperature=config.prompt_temperature,
                prompt_language=prompt_language,
                review_translation_language=config.review_translation_language,
                scenario_hint=_scenario_hint(index, target.service_type),
                character=target.character,
                character_constraints=target.character_constraints,
            )
        except Exception as exc:  # noqa: BLE001
            if config.fail_on_llm_error or not config.allow_deterministic_fallback:
                raise
            payload = deterministic_write_discovery_prompt(
                evidence=evidence,
                quality_profile=quality_profile,
                prompt_language=prompt_language,
                review_translation_language=config.review_translation_language,
                character=target.character,
                character_constraints=target.character_constraints,
            )
            payload["generation_method"] = {
                "type": "deterministic_fallback",
                "fallback_reason": str(exc),
            }
            return payload
    if not config.allow_deterministic_fallback:
        raise ValueError(
            "place_discovery batch generation requires LLM prompt writing. "
            "Use --allow-deterministic-fallback only for local pipeline debugging."
        )
    return deterministic_write_discovery_prompt(
        evidence=evidence,
        quality_profile=quality_profile,
        prompt_language=prompt_language,
        review_translation_language=config.review_translation_language,
        character=target.character,
        character_constraints=target.character_constraints,
    )


def _scenario_hint(index: int, service_type: str) -> str:
    if service_type == "hotel_booking":
        options = [
            "tourist_itinerary_planning",
            "area_selection_discovery",
            "budget_experience_tradeoff",
            "time_or_weather_sensitive_plan",
        ]
    elif service_type == "attraction_visit":
        options = [
            "tourist_itinerary_planning",
            "time_or_weather_sensitive_plan",
            "group_or_family_constraints",
            "area_selection_discovery",
        ]
    elif service_type == "cafe_reservation":
        options = [
            "work_or_study_break",
            "advance_reservation",
            "group_or_family_constraints",
            "current_area_immediate_need",
        ]
    else:
        options = [
            "advance_reservation",
            "group_or_family_constraints",
            "work_or_study_break",
            "budget_experience_tradeoff",
            "current_area_immediate_need",
        ]
    return options[(index - 1) % len(options)]


def _build_example(
    *,
    target: SampledDiscoveryTarget,
    evidence: dict[str, Any],
    prompt_payload: dict[str, Any],
    quality_profile: dict[str, Any],
    index: int,
    generated_at: str,
    review_status: str,
) -> dict[str, Any]:
    gold_detail = evidence.get("gold_service", {})
    character = target.character or {}
    prompt_language = prompt_payload.get("prompt_language", "")
    review_translation = prompt_payload.get("review_translation") or {}
    review_language = review_translation.get("language", "")
    user_prompt_translation = review_translation.get("user_prompt", "")
    if not user_prompt_translation and prompt_language == review_language:
        user_prompt_translation = prompt_payload["user_prompt"]
    ranked_candidates = _build_top3_candidates(target, evidence)
    gold_candidate = ranked_candidates[0]
    example_id = f"pd_batch_{target.city_id}_{index:04d}_{_slug(gold_candidate['name'])}"
    example = {
        "example_id": example_id,
        "task_type": "place_discovery",
        "difficulty": legacy_difficulty_label(int(quality_profile.get("search_ambiguity_score", 0))),
        "character_id": character.get("character_id", ""),
        "character_name": character.get("name", ""),
        "service_type": target.service_type,
        "start": target.start,
        "prompt_language": prompt_language,
        "review_language": review_language,
        "user_prompt": prompt_payload["user_prompt"],
        "user_prompt_translation": user_prompt_translation,
        "review_translation": {**review_translation, "user_prompt": user_prompt_translation},
        "hard_constraints": prompt_payload.get("hard_constraints", []),
        "soft_preferences": prompt_payload.get("soft_preferences", []),
        "ranking_criteria": prompt_payload.get("ranking_criteria", []),
        "candidate_scope": {
            "city_id": target.city_id,
            "district_id": target.start.get("district_id", ""),
            "service_type": target.service_type,
            "area_hints": [target.start.get("area_name", "")],
        },
        "gold": {
            "candidates": ranked_candidates,
            "acceptable_top1": [gold_candidate.get("service_id", "")],
            "ranking_tiers": [
                {"tier": candidate["rank"], "service_ids": [candidate.get("service_id", "")]}
                for candidate in ranked_candidates
            ],
            "ranking_schema_version": "geoword.place_discovery.top3.v1",
        },
        "search_landscape": target.search_landscape,
        "distractors": target.distractors,
        "evaluation": prompt_payload.get("evaluation", {}),
        "evidence_ledger": evidence,
        "prompt_evidence_map": prompt_payload.get("prompt_evidence_map", {}),
        "quality_profile": quality_profile,
        "prompt_generation": prompt_payload.get("generation_method", {}),
        "review_status": review_status,
        "batch_metadata": {
            "generated_at": generated_at,
            "generator": "place_discovery.batch_generation",
            "search_ambiguity_score": quality_profile.get("search_ambiguity_score"),
            "constraint_complexity": quality_profile.get("constraint_complexity"),
            "character_constraints": target.character_constraints,
        },
    }
    example["judge_sequence_override"] = _resolve_judge_payload(example, "place_discovery")
    return example


def _write_curated_by_city(config: DiscoveryBatchGenerationConfig, examples: list[dict[str, Any]]) -> list[str]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for example in examples:
        city_id = example.get("candidate_scope", {}).get("city_id") or "unknown_city"
        grouped[city_id].append(example)
    paths = []
    for city_id, city_examples in grouped.items():
        payload = build_curated_payload(
            city_id=city_id,
            variant=config.variant,
            schema_version=config.schema_version,
            examples=city_examples,
        )
        path = config.output_dir / "specs" / city_id / "place_discovery" / "examples" / "task_specs.json"
        write_json(path, payload)
        paths.append(str(path))
    return paths


def _build_top3_candidates(target: SampledDiscoveryTarget, evidence: dict[str, Any], world=None) -> list[dict[str, Any]]:
    """Build a factual three-item ranking without letting the prompt writer set GT."""
    details_by_service = {
        detail.get("service_id", ""): detail
        for detail in evidence.get("candidate_services", [])
        if detail.get("service_id")
    }
    services = [target.gold_service, *target.candidate_services[1:]]
    places_by_id = {p["place_id"]: p for p in (world or {}).get("places", [])}
    places_by_id.update({p["place_id"]: p for p in
                        (evidence.get("construction_contract") or {}).get("candidate_facts", [])})
    candidates = []
    for service in services:
        service_id = service.get("service_id", "")
        detail = details_by_service.get(service_id, service)
        place_id = detail.get("place_id", service.get("place_id", ""))
        place = places_by_id.get(place_id) or (target.gold_place if service_id == target.gold_service.get("service_id") else {})
        candidates.append({
            "place_id": place_id,
            "service_id": service_id,
            "name": place.get("name") or detail.get("name", service.get("name", "")),
            "coordinate": place.get("location") or detail.get("location") or {},
            "place_type": place.get("primary_type") or detail.get("place_type", ""),
            "reasons": service_reason_lines(detail) or ([f"Public type: {place['primary_type']}"] if place.get("primary_type") else []),
            "factual_rank_features": {
                "rating": detail.get("rating"),
                "queue_time_min": detail.get("queue_time_min"),
                "crowding_level": detail.get("crowding_level"),
                "price_band": detail.get("price_band"),
                "open_status": detail.get("open_status"),
            },
        })
    if len(candidates) != 3:
        raise ValueError("place_discovery_top3_requires_three_materialized_candidates")

    # Rank 1 is the deterministically constraint-compatible service.  The two
    # alternatives remain factual but are ordered by observable service facts,
    # allowing evaluation of ranking quality rather than a forced single pick.
    primary, alternatives = candidates[0], candidates[1:]
    crowd_rank = {"low": 0, "medium_low": 1, "medium": 2, "high": 3}
    price_rank = {"low": 0, "medium": 1, "high": 2}
    alternatives.sort(key=lambda item: (
        -(item["factual_rank_features"].get("rating") or 0),
        item["factual_rank_features"].get("queue_time_min") if item["factual_rank_features"].get("queue_time_min") is not None else 10**9,
        crowd_rank.get(item["factual_rank_features"].get("crowding_level"), 9),
        price_rank.get(item["factual_rank_features"].get("price_band"), 9),
        item.get("service_id", ""),
    ))
    ordered = [primary, *alternatives]
    for rank, candidate in enumerate(ordered, start=1):
        candidate["rank"] = rank
    return ordered


def _slug(value: str) -> str:
    cleaned = "".join(ch for ch in str(value or "") if ch.isalnum() or ch in {"_", "-"})
    return cleaned[:32] or "target"
