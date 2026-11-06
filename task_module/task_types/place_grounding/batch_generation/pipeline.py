"""End-to-end batch generation pipeline for place_grounding drafts."""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.io import read_json, write_json
from task_module.task_types.place_grounding.batch_generation.quality import (
    build_final_quality_profile,
    build_pre_prompt_quality_profile,
)
from task_module.task_types.place_grounding.batch_generation.config import (
    BatchGenerationConfig,
    as_jsonable_config,
    infer_characters_path,
    infer_world_path,
)
from task_module.task_types.place_grounding.batch_generation.evidence import build_evidence_package, final_task_image_path
from task_module.task_types.place_grounding.batch_generation.judge_templates import build_grounding_judge_override
from task_module.task_types.place_grounding.batch_generation.llm_writer import (
    deterministic_write_grounding_prompt,
    write_grounding_prompt_with_llm,
)
from task_module.task_types.place_grounding.batch_generation.quality_gate import evaluate_example_quality
from task_module.task_types.place_grounding.batch_generation.sampler import SampledTarget, sample_targets
from task_module.task_types.place_grounding.batch_generation.schema import build_curated_payload


def generate_batch(config: BatchGenerationConfig) -> dict[str, Any]:
    """Generate a cross-city batch and write draft artifacts."""
    worlds = _load_worlds(config)
    characters_by_city = _load_characters(config)
    examples, failures, quality_reports = generate_grounding_examples(
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
            "task_type": "place_grounding",
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


def generate_grounding_examples(
    *,
    config: BatchGenerationConfig,
    worlds: dict[str, dict[str, Any]],
    characters_by_city: dict[str, dict[str, Any]],
    count: int,
    require_quality_pass: bool = True,
    max_attempts_per_slot: int = 3,
    start_index: int = 1,
    excluded_place_ids: set[str] | None = None,
    cost_slot_callback: Any = None,
    attempt_rng_callback: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Generate grounding examples from world data with optional quality filtering.

    Returns:
        (examples, failures, quality_reports)
    """
    rng = random.Random(config.seed)
    generated_at = datetime.now(timezone.utc).isoformat()
    examples: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    quality_reports: list[dict[str, Any]] = []
    reserved_place_ids = set(excluded_place_ids or set())

    max_slots = count * 3
    slot = 0
    while len(examples) < count and slot < max_slots:
        slot += 1
        if cost_slot_callback is not None:
            cost_slot_callback(slot)
        success = False
        for attempt in range(1, max_attempts_per_slot + 1):
            if attempt_rng_callback is not None:
                rng = attempt_rng_callback(start_index + len(examples), attempt)
            targets = sample_targets(
                worlds=worlds,
                characters_by_city=characters_by_city,
                count=1,
                city_weights=config.city_weights,
                granularity_weights=config.granularity_weights,
                category_weights=config.category_weights,
                rng=rng,
                quality_min_ambiguity=config.quality_min_ambiguity,
                quality_max_ambiguity=config.quality_max_ambiguity,
                max_place_attempts=config.max_place_attempts,
                excluded_place_ids=reserved_place_ids,
            )
            if not targets:
                _emit_detail_progress(
                    task_index=start_index + len(examples), accepted=len(examples), requested=count,
                    step="sampling", state="retrying", note=f"attempt={attempt}",
                )
                failures.append({
                    "slot": slot,
                    "attempt": attempt,
                    "reason": "ambiguity_out_of_range",
                    "detail": (
                        f"no POI in [{config.quality_min_ambiguity}, {config.quality_max_ambiguity}] "
                        f"after sampling attempts"
                    ),
                })
                continue
            target = targets[0]
            index = start_index + len(examples)
            _emit_detail_progress(
                task_index=index, accepted=len(examples), requested=count,
                step="sampling", state="complete", note=f"attempt={attempt}",
            )
            try:
                example, quality = _generate_example_for_target(
                    config=config,
                    target=target,
                    world=worlds[target.city_id],
                    index=index,
                    generated_at=generated_at,
                    progress_callback=lambda step, state, note="": _emit_detail_progress(
                        task_index=index, accepted=len(examples), requested=count,
                        step=step, state=state, note=note,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - record and retry with another target
                _emit_detail_progress(
                    task_index=index, accepted=len(examples), requested=count,
                    step="candidate", state="retrying", note=f"attempt={attempt}",
                )
                failures.append({
                    "slot": slot,
                    "attempt": attempt,
                    "city_id": target.city_id,
                    "place_name": (target.place or {}).get("name", ""),
                    "reason": "generation_error",
                    "detail": str(exc),
                })
                continue

            quality_reports.append(
                {
                    "example_id": example["example_id"],
                    "city_id": target.city_id,
                    "passed": quality["passed"],
                    "hard_failed": quality["hard_failed"],
                    "soft_failed": quality["soft_failed"],
                }
            )
            if require_quality_pass and not quality["passed"]:
                _emit_detail_progress(
                    task_index=index, accepted=len(examples), requested=count,
                    step="quality_gate", state="retrying", note=f"attempt={attempt}",
                )
                failures.append({
                    "slot": slot,
                    "attempt": attempt,
                    "example_id": example["example_id"],
                    "city_id": target.city_id,
                    "place_name": (target.place or {}).get("name", ""),
                    "reason": "quality_gate_failed",
                    "hard_failed": quality["hard_failed"],
                })
                continue

            examples.append(example)
            example["_seed_lineage"] = {
                "task_index": start_index + len(examples) - 1, "attempt_index": attempt,
            }
            example["_cost_generation_unit_id"] = f"slot_{slot:04d}"
            gold_place_id = (example.get("gold") or {}).get("place_id", "")
            if gold_place_id:
                reserved_place_ids.add(str(gold_place_id))
            # The task folder is written after the evidence batch finishes.
            # Emit per-example construction progress now so the audit UI does
            # not appear frozen during Street View / LLM work.
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
            continue

    return examples, failures, quality_reports


def _generate_example_for_target(
    *,
    config: BatchGenerationConfig,
    target: SampledTarget,
    world: dict[str, Any],
    index: int,
    generated_at: str,
    progress_callback: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    evidence = build_evidence_package(
        target=target,
        world=world,
        image_pool_dir=config.image_pool_dir,
        index=index,
        use_google_streetview=config.use_google_streetview,
        streetview_output_dir=config.streetview_output_dir,
        streetview_headings=config.streetview_headings,
        streetview_size=config.streetview_size,
        streetview_fov=config.streetview_fov,
        streetview_pitch=config.streetview_pitch,
        streetview_radius_m=config.streetview_radius_m,
        progress_callback=progress_callback,
    )
    pre_quality_profile = build_pre_prompt_quality_profile(
        target=target,
        evidence=evidence,
    )
    _emit_progress(progress_callback, "llm_prompt", "running")
    prompt_payload = _write_prompt_payload(
        config=config,
        evidence=evidence,
        quality_profile=pre_quality_profile,
        target=target,
    )
    _emit_progress(
        progress_callback,
        "llm_prompt",
        "fallback" if (prompt_payload.get("generation_method") or {}).get("type") == "deterministic_fallback" else "complete",
    )
    final_quality_profile = build_final_quality_profile(
        target=target,
        evidence=evidence,
        prompt_payload=prompt_payload,
    )
    prompt_payload["quality_profile"] = final_quality_profile
    prompt_payload["difficulty_profile"] = final_quality_profile
    example = _build_example(
        target=target,
        evidence=evidence,
        prompt_payload=prompt_payload,
        index=index,
        generated_at=generated_at,
        review_status=config.review_status,
        display_difficulty=final_quality_profile["display_difficulty"],
    )
    example["judge_sequence_override"] = build_grounding_judge_override(example)
    _emit_progress(progress_callback, "quality_gate", "running")
    quality = evaluate_example_quality(example, world)
    _emit_progress(progress_callback, "quality_gate", "complete" if quality["passed"] else "failed")
    example["quality_gate"] = quality
    return example, quality


def _emit_progress(callback: Any, step: str, state: str, note: str = "") -> None:
    if callback is not None:
        callback(step, state, note)


def _emit_detail_progress(
    *, task_index: int, accepted: int, requested: int, step: str, state: str, note: str = "",
) -> None:
    suffix = f" note={note}" if note else ""
    print(
        f"[BuildProgress] phase=detail task={task_index} accepted={accepted} requested={requested} "
        f"step={step} state={state}{suffix}",
        flush=True,
    )


def _load_worlds(config: BatchGenerationConfig) -> dict[str, dict[str, Any]]:
    worlds: dict[str, dict[str, Any]] = {}
    for city_id in config.city_weights:
        world_path = infer_world_path(city_id)
        worlds[city_id] = read_json(world_path)
    return worlds


def _load_characters(config: BatchGenerationConfig) -> dict[str, dict[str, Any]]:
    characters: dict[str, dict[str, Any]] = {}
    for city_id in config.city_weights:
        path = infer_characters_path(city_id, config.variant)
        characters[city_id] = read_json(path) if path and path.exists() else {"characters": []}
    return characters


def _write_prompt_payload(
    *,
    config: BatchGenerationConfig,
    evidence: dict[str, Any],
    quality_profile: dict[str, Any],
    target: SampledTarget,
) -> dict[str, Any]:
    prompt_language = config.language_for_city(target.city_id)
    if config.use_llm_prompt:
        try:
            return write_grounding_prompt_with_llm(
                evidence=evidence,
                quality_profile=quality_profile,
                granularity=target.granularity,
                config_yaml=config.config_yaml,
                stage=config.prompt_model_stage,
                temperature=config.prompt_temperature,
                allow_gold_name=False,
                prompt_language=prompt_language,
                review_translation_language=config.review_translation_language,
            )
        except Exception as exc:  # noqa: BLE001 - preserve reason in draft metadata
            if config.fail_on_llm_error:
                raise
            payload = deterministic_write_grounding_prompt(
                evidence=evidence,
                quality_profile=quality_profile,
                granularity=target.granularity,
                allow_gold_name=False,
                prompt_language=prompt_language,
                review_translation_language=config.review_translation_language,
            )
            payload["generation_method"] = {
                "type": "deterministic_fallback",
                "fallback_reason": str(exc),
            }
            return payload
    return deterministic_write_grounding_prompt(
        evidence=evidence,
        quality_profile=quality_profile,
        granularity=target.granularity,
        allow_gold_name=False,
        prompt_language=prompt_language,
        review_translation_language=config.review_translation_language,
    )


def _build_example(
    *,
    target: SampledTarget,
    evidence: dict[str, Any],
    prompt_payload: dict[str, Any],
    index: int,
    generated_at: str,
    review_status: str,
    display_difficulty: str,
) -> dict[str, Any]:
    place = target.place
    gold = _gold_payload(target, evidence)
    example_id = f"pg_batch_{target.city_id}_{index:04d}_{_slug(gold.get('place_name', 'target'))}"
    image_path = _primary_image_path(evidence)
    prompt_reference_image_path = _prompt_reference_image_path(evidence)
    review_translation = prompt_payload.get("review_translation") or {}
    prompt_language = prompt_payload.get("prompt_language", "")
    review_language = review_translation.get("language", "")
    user_prompt_translation = review_translation.get("user_prompt", "")
    if not user_prompt_translation and prompt_language == review_language:
        user_prompt_translation = prompt_payload["user_prompt"]
    quality_profile = prompt_payload.get("quality_profile", {})
    return {
        "example_id": example_id,
        "task_type": "place_grounding",
        "difficulty": display_difficulty,
        "character_id": "",
        "character_name": "",
        "image_path": image_path,
        "prompt_reference_image_path": prompt_reference_image_path,
        "prompt_language": prompt_language,
        "review_language": review_language,
        "user_prompt": prompt_payload["user_prompt"],
        "user_prompt_translation": user_prompt_translation,
        "review_translation": {**review_translation, "user_prompt": user_prompt_translation},
        "grounding_target_type": "poi",
        "acceptable_granularity": target.granularity,
        "evidence_channels": ["image", "text", "map_search"],
        "visual_clues": prompt_payload["visual_clues"],
        "text_clues": prompt_payload["text_clues"],
        "negative_clues": prompt_payload["negative_clues"],
        "candidate_scope": prompt_payload["candidate_scope"],
        "gold": gold,
        "grounding_constraints": prompt_payload["grounding_constraints"],
        "evaluation": prompt_payload["evaluation"],
        "ambiguity_landscape": target.ambiguity_landscape,
        "evidence_ledger": evidence,
        "source_evidence": evidence,
        "prompt_evidence_map": prompt_payload["prompt_evidence_map"],
        "quality_profile": quality_profile,
        "difficulty_profile": quality_profile,
        "prompt_generation": prompt_payload.get("generation_method", {}),
        "review_status": review_status,
        "batch_metadata": {
            "generated_at": generated_at,
            "generator": "place_grounding.batch_generation",
            "gold_source": "world.place",
            "ambiguity_score": quality_profile.get("ambiguity_score"),
            "complexity_score": quality_profile.get("complexity_score"),
        },
    }


def _gold_payload(target: SampledTarget, evidence: dict[str, Any]) -> dict[str, Any]:
    place = target.place
    place_evidence = evidence.get("place_evidence", {})
    anchors = [place.get("name", ""), *[item.get("name", "") for item in place_evidence.get("nearby_anchors", [])]]
    anchors = [item for item in dict.fromkeys(anchors) if item]
    gold = {
        "place_id": place.get("place_id", ""),
        "candidate_id": place.get("place_id", ""),
        "place_name": place.get("name", ""),
        "district_id": place.get("district_id", ""),
        "district_name": place.get("district_id", ""),
        "coordinate": place.get("location", {}),
        "anchor_places": anchors or [place.get("name", "")],
    }
    # An exact POI task is judged by candidate identity.  Retain a radius only
    # for the legacy non-exact task variants that actually use spatial grading.
    if target.granularity != "exact_poi":
        gold["acceptable_radius_m"] = 500
    return gold


def _write_curated_by_city(config: BatchGenerationConfig, examples: list[dict[str, Any]]) -> list[str]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for example in examples:
        city_id = example.get("candidate_scope", {}).get("city_id") or example.get("gold", {}).get("city_id")
        city_id = city_id or "unknown_city"
        grouped[city_id].append(example)

    paths: list[str] = []
    for city_id, city_examples in grouped.items():
        payload = build_curated_payload(
            city_id=city_id,
            variant=config.variant,
            schema_version=config.schema_version,
            examples=city_examples,
        )
        path = config.output_dir / "specs" / city_id / "place_grounding" / "examples" / "task_specs.json"
        write_json(path, payload)
        paths.append(str(path))
    return paths


def _slug(value: str) -> str:
    cleaned = "".join(ch for ch in str(value or "") if ch.isalnum() or ch in {"_", "-"})
    return cleaned[:32] or "target"


def _primary_image_path(evidence: dict[str, Any]) -> str:
    return final_task_image_path(evidence)


def _prompt_reference_image_path(evidence: dict[str, Any]) -> str:
    assets = evidence.get("image_assets", [])
    for asset in assets:
        if asset.get("role") == "prompt_reference" and asset.get("path") and asset.get("quality") != "fetch_failed":
            return asset["path"]
    for asset in assets:
        if asset.get("role") not in {"evaluation", "primary"} and asset.get("path") and asset.get("quality") != "fetch_failed":
            return asset["path"]
    return ""
