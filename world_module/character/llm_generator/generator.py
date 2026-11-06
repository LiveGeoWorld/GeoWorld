"""LLM-powered character generator — 4-step pipeline.

Step 1: LLM allocates characters to districts  (region allocation)
Step 2: LLM generates character profiles in parallel per district
Step 3: Code fills structural data (coordinates, mobility, work district)
Step 4: LLM builds relationship network across all characters

Logs are written to llm_generator/log/ for debugging.
"""

from __future__ import annotations

import json
import random
import time as time_mod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from shared.config_loader import PipelineConfig
from shared.io import read_json, write_json
from shared.llm_client import OpenAICompatibleChatClient, YunwuResponsesClient

from world_module.character.character_schema import CharacterSet, RelationshipGraph, CharacterMobility
from world_module.character.config import CharacterGeneratorConfig
from world_module.character.llm_generator.logger import GenerationLogger
from world_module.character.llm_generator.prompt_builder import (
    build_region_prompt, build_profile_prompt, build_relationship_prompt,
    build_profile_prompt_incremental, SYSTEM_RELATIONSHIP,
)
from world_module.character.llm_generator.response_parser import (
    parse_region_result, parse_profile_result, parse_relationship_result,
)
from world_module.character.llm_generator.structure_filler import fill_characters


def _emit_progress(step: str, state: str, *, label: str, completed: int | None = None,
                   total: int | None = None) -> None:
    event = {"stage": "character_generation", "step": step, "state": state, "label": label}
    if completed is not None:
        event["completed"] = completed
    if total is not None:
        event["total"] = total
    print("[geoword-progress] " + json.dumps(event, ensure_ascii=False), flush=True)


def _normalized_character_name(profile: dict[str, Any]) -> str:
    return " ".join(str(profile.get("name", "")).split()).casefold()


def _parse_relationships_with_retry(
    request_text: Callable[[], str],
    *,
    char_ids: set[str],
    id_remap: dict[str, str],
    logger: GenerationLogger,
    max_attempts: int = 3,
) -> tuple[list[Any], RelationshipGraph]:
    """Retry malformed relationship JSON without regenerating character profiles."""
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        raw_text = request_text()
        try:
            relationships, graph = parse_relationship_result(raw_text, char_ids, id_remap)
            logger.log_step_response(raw_text, f"{len(relationships)} relationships (attempt {attempt}/{max_attempts})")
            return relationships, graph
        except (ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.log_step_response(raw_text, f"relationship JSON parse failed (attempt {attempt}/{max_attempts}): {exc}")
            print(f"[Step4] Invalid relationship JSON; retry {attempt}/{max_attempts}: {exc}", flush=True)
    raise RuntimeError(f"Relationship JSON remained invalid after {max_attempts} attempts: {last_error}")


def _repair_profile_identities(
    profiles: list[dict[str, Any]],
    profile_district: list[str],
    *,
    cfg: CharacterGeneratorConfig,
    city_name: str,
    country: str,
    provider: str,
    client: YunwuResponsesClient,
    chat_client: OpenAICompatibleChatClient | None,
    logger: GenerationLogger,
) -> dict[str, Any]:
    """Replace only duplicate/missing profile identities before Step 3/4."""
    occupied = {
        " ".join(str(name).split()).casefold()
        for name in cfg.excluded_character_names if str(name).strip()
    }
    conflicts: list[int] = []
    for index, profile in enumerate(profiles):
        key = _normalized_character_name(profile)
        if not key or key in occupied:
            conflicts.append(index)
        else:
            occupied.add(key)

    repaired = 0
    unresolved: list[str] = []
    for index in conflicts:
        district_name = profile_district[index]
        for attempt in range(1, cfg.profile_identity_repair_attempts + 1):
            existing_names = ", ".join(sorted(occupied))
            prompt = build_profile_prompt_incremental(
                district_name=district_name,
                existing_names=existing_names,
                sub_count=1,
                city_name=city_name,
                country=country,
            )
            logger.log_step_prompt(
                f"STEP 2 - IDENTITY REPAIR ({district_name} - attempt {attempt})",
                prompt, f"{len(prompt)} chars",
            )
            try:
                if provider in ("openai_compatible", "openai_chat"):
                    response = chat_client.call_chat(
                        [{"role": "user", "content": prompt}], extra={"max_tokens": 3000},
                    )
                    text = _extract_azure_text(response)
                else:
                    response = client.call(prompt, extra={"max_output_tokens": 3000})
                    text = _extract_yunwu_text(response)
                candidates = parse_profile_result(text)
                candidate = candidates[0] if candidates else {}
                key = _normalized_character_name(candidate)
                logger.log_step_response(text, f"IDENTITY REPAIR {attempt}: {'accepted' if key and key not in occupied else 'rejected'}")
                if key and key not in occupied:
                    profiles[index] = candidate
                    occupied.add(key)
                    repaired += 1
                    break
            except Exception as exc:
                logger.log_step_response("", f"IDENTITY REPAIR {attempt} FAILED - {exc}")
        else:
            unresolved.append(str(profiles[index].get("name", "<missing>")))

    return {
        "excluded_name_count": len(cfg.excluded_character_names),
        "duplicates_detected": len(conflicts),
        "repaired": repaired,
        "unresolved": unresolved,
        "max_attempts_per_profile": cfg.profile_identity_repair_attempts,
    }


class LLMGenerator:
    """Generate characters via 4-step LLM pipeline."""

    def __init__(self, config: CharacterGeneratorConfig | None = None):
        self.config = config or CharacterGeneratorConfig()

    def generate(self) -> CharacterSet:
        cfg = self.config
        t_start = time_mod.time()

        world_path = cfg.world_path
        if not world_path:
            raise ValueError("--world is required.")

        # Load world
        print(f"[LLMGenerator] Loading world: {world_path}")
        world = read_json(world_path)
        city = (world.get("cities") or [{}])[0]
        city_id = city.get("city_id", "unknown")
        city_name = city.get("name", city_id)
        country = city.get("country") or _infer_country(city_id, city_name)
        districts = world.get("districts", [])
        if not districts:
            raise ValueError("world.json has no districts.")

        # Setup LLM client
        pipeline_config = PipelineConfig(cfg.config_yaml)
        stage_model_name = pipeline_config.stage_model(cfg.stage_model_key)
        endpoint = pipeline_config.model_endpoint(stage_model_name)
        provider = endpoint.get("provider", "openai_compatible")
        model = endpoint.get("model", "gpt-5.5")
        print(f"[LLMGenerator] Provider={provider}, model={model}, count={cfg.count}")

        client = YunwuResponsesClient(
            model=model, config_path=cfg.config_yaml,
            timeout_seconds=cfg.llm_timeout_seconds, endpoint_name=stage_model_name,
        )
        chat_client = OpenAICompatibleChatClient(
            base_url=endpoint.get("base_url", ""), api_key=endpoint.get("api_key", ""), model=model,
            timeout_seconds=cfg.llm_timeout_seconds, endpoint_name=stage_model_name,
            pricing_reference=endpoint.get("pricing_reference"),
        ) if provider in ("openai_compatible", "openai_chat") else None

        # Setup logger
        logger = GenerationLogger()
        logger.log_header(world_path, cfg.count, model, provider)

        try:
            # ================================================================
            # Step 1: Region allocation
            # ================================================================
            _emit_progress("region_allocation", "running", label="分配人物至城市区域", completed=0, total=1)
            district_names = [d["name"] for d in districts]
            print(f"[Step1] Allocating {cfg.count} characters to {len(district_names)} districts...")

            region_prompt = build_region_prompt(cfg.count, district_names)
            logger.log_step_prompt(
                "STEP 1 — Region Allocation",
                region_prompt,
                f"{len(region_prompt)} chars, {cfg.count} chars → {len(district_names)} districts",
            )

            if provider in ("openai_compatible", "openai_chat"):
                resp = chat_client.call_chat([{"role": "user", "content": region_prompt}], extra={"max_tokens": 500})
                region_text = _extract_azure_text(resp)
            else:
                resp = client.call(region_prompt, extra={"max_output_tokens": 500})
                region_text = _extract_yunwu_text(resp)

            region_map: dict[str, int] = parse_region_result(region_text)
            if not region_map:
                print("[Step1] LLM failed, using fallback allocation")
                selected = random.Random(cfg.seed).sample(
                    district_names, k=min(4, len(district_names)),
                )
                per = cfg.count // len(selected)
                rem = cfg.count % len(selected)
                for i, name in enumerate(selected):
                    region_map[name] = per + (1 if i < rem else 0)

            logger.log_step_response(
                region_text,
                f"{region_map} (total={sum(region_map.values())}) {'✓' if sum(region_map.values()) == cfg.count else '✗ COUNT MISMATCH'}",
            )
            print(f"[Step1] Allocation: {region_map}, total={sum(region_map.values())}")
            _emit_progress("region_allocation", "completed", label="人物区域分配完成", completed=1, total=1)

            # ================================================================
            # Step 2: Character profiles (parallel, 2 concurrent)
            # ================================================================
            profile_district: list[str] = []
            for dname, count in region_map.items():
                profile_district.extend([dname] * count)

            district_batches: list[tuple[str, int]] = list(region_map.items())
            print(f"[Step2] Generating profiles for {len(district_batches)} districts...")
            _emit_progress("profiles", "running", label="生成人物档案", completed=0, total=cfg.count)

            all_profiles: list[dict[str, Any]] = [{}] * len(profile_district)

            completed_profiles = 0
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = {}
                for dname, sub_count in district_batches:
                    prompt = build_profile_prompt(dname, sub_count, city_name, country)
                    logger.log_step_prompt(
                        f"STEP 2 — Character Profiles ({dname} × {sub_count})",
                        prompt,
                        f"{len(prompt)} chars",
                    )
                    if provider in ("openai_compatible", "openai_chat"):
                        fut = executor.submit(lambda prompt=prompt: chat_client.call_chat(
                            [{"role": "user", "content": prompt}], extra={"max_tokens": 8000}
                        ))
                    elif provider == "azure_openai":
                        msgs = [{"role": "user", "content": prompt}]
                        if provider == "azure_openai":
                            fut = executor.submit(lambda: client.call_chat(msgs, extra={"max_tokens": 8000}))
                    else:
                        fut = executor.submit(lambda: client.call(prompt, extra={"max_output_tokens": 8000}))
                    futures[fut] = (dname, sub_count)

                for fut in as_completed(futures):
                    dname, expected = futures[fut]
                    try:
                        if provider in ("azure_openai", "openai_chat", "openai_compatible"):
                            text = _extract_azure_text(fut.result())
                        else:
                            text = _extract_yunwu_text(fut.result())
                        profiles = parse_profile_result(text)
                        logger.log_step_response(
                            text,
                            f"{len(profiles)}/{expected} profiles {'✓' if len(profiles) == expected else '✗'}",
                        )
                        print(f"[Step2] {dname}: got {len(profiles)}/{expected} profiles")

                        placed = 0
                        for idx, pd_name in enumerate(profile_district):
                            if pd_name == dname and placed < len(profiles):
                                all_profiles[idx] = profiles[placed]
                                placed += 1
                        completed_profiles += placed
                        _emit_progress("profiles", "running", label=f"已完成 {dname} 的人物档案",
                                       completed=completed_profiles, total=cfg.count)
                    except Exception as exc:
                        logger.log_step_response("", f"FAILED — {exc}")
                        print(f"[Step2] {dname}: FAILED — {exc}")

            # ── Step 2 retry: fill missing profiles incrementally ──
            for dname, expected in district_batches:
                existing = [
                    all_profiles[idx] for idx, pd in enumerate(profile_district)
                    if pd == dname and all_profiles[idx].get("name")
                ]
                missing = expected - len(existing)
                if missing <= 0:
                    continue
                existing_names = ", ".join(
                    f"{p['name']}({p.get('age', '?')}岁)" for p in existing
                )
                retry_prompt = build_profile_prompt_incremental(
                    district_name=dname,
                    existing_names=existing_names,
                    sub_count=missing,
                    city_name=city_name,
                    country=country,
                )
                logger.log_step_prompt(
                    f"STEP 2 — RETRY ({dname} × {missing})",
                    retry_prompt, f"{len(retry_prompt)} chars",
                )
                try:
                    if provider in ("openai_compatible", "openai_chat"):
                        retry_resp = chat_client.call_chat(
                            [{"role": "user", "content": retry_prompt}], extra={"max_tokens": 6000}
                        )
                        retry_text = _extract_azure_text(retry_resp)
                    elif provider == "azure_openai":
                        if provider == "azure_openai":
                            retry_resp = client.call_chat(
                                [{"role": "user", "content": retry_prompt}],
                                extra={"max_tokens": 6000},
                            )
                        retry_text = _extract_azure_text(retry_resp)
                    else:
                        retry_resp = client.call(retry_prompt, extra={"max_output_tokens": 6000})
                        retry_text = _extract_yunwu_text(retry_resp)
                    retry_profiles = parse_profile_result(retry_text)
                    logger.log_step_response(
                        retry_text,
                        f"RETRY → {len(retry_profiles)}/{missing} {'✓' if len(retry_profiles) >= missing else '✗'}",
                    )
                    placed = 0
                    for idx, pd in enumerate(profile_district):
                        if pd == dname and not all_profiles[idx].get("name") and placed < len(retry_profiles):
                            all_profiles[idx] = retry_profiles[placed]
                            placed += 1
                    print(f"[Step2] RETRY {dname}: added {placed}/{missing}")
                    completed_profiles += placed
                    _emit_progress("profiles", "running", label=f"补全 {dname} 的人物档案",
                                   completed=completed_profiles, total=cfg.count)
                except Exception as exc:
                    logger.log_step_response("", f"RETRY FAILED — {exc}")
                    print(f"[Step2] RETRY {dname}: FAILED — {exc}")

            # Step 3: Code fills structural data
            identity_validation = _repair_profile_identities(
                all_profiles, profile_district, cfg=cfg, city_name=city_name, country=country,
                provider=provider, client=client, chat_client=chat_client, logger=logger,
            )
            if identity_validation["duplicates_detected"]:
                print(
                    "[Step2] Identity repair "
                    f"{identity_validation['repaired']}/{identity_validation['duplicates_detected']} repaired"
                )
            _emit_progress("structure", "running", label="补全人物空间与行动结构", completed=0, total=cfg.count)
            print(f"[Step3] Filling structural data...")

            district_assignments: dict[str, list[int]] = {}
            for idx, dname in enumerate(profile_district):
                district_assignments.setdefault(dname, []).append(idx)

            characters = fill_characters(
                all_profiles, district_assignments, districts, city_id, cfg.seed,
            )
            logger.log_step_info(
                f"STEP 3 — Structure Fill\n  {len(characters)} characters: "
                f"coordinates from anchor_places, work districts by occupation, "
                f"mobility by age/region, free windows by occupation."
            )
            print(f"[Step3] Filled {len(characters)} characters")
            _emit_progress("structure", "completed", label="人物空间与行动结构完成",
                           completed=len(characters), total=cfg.count)

            # A relationship timeout must not require regenerating completed
            # profiles or re-running the map pipeline on the next continuation.
            variant = _derive_variant(city_id)
            draft_path = Path(cfg.output_root) / city_id / variant / "characters_draft.json"
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            draft = CharacterSet(
                city_id=city_id, city_name=city_name,
                generated_at=datetime.now(timezone.utc).isoformat(),
                generation_config={
                    "count": cfg.count, "seed": cfg.seed, "model": model,
                    "stage_model_key": cfg.stage_model_key, "world_path": world_path,
                    "pipeline": "4-step", "checkpoint": "profiles_and_structure",
                    "identity_validation": identity_validation,
                },
                characters=characters,
            )
            write_json(draft_path, draft.to_dict())
            print(f"[checkpoint] character_draft={draft_path}")

            # ================================================================
            # Step 4: Relationship network
            # ================================================================
            _emit_progress("relationships", "running", label="生成人物关系网络", completed=0, total=1)
            print(f"[Step4] Building relationship network...")

            summaries = [
                f"{c.name}, {c.age}, {'女' if c.gender == 'female' else '男'}, "
                f"{c.occupation}, {c.mobility.home_district_name}"
                for c in characters
            ]
            rel_prompt = build_relationship_prompt(summaries)
            logger.log_step_prompt(
                "STEP 4 — Relationship Network",
                rel_prompt,
                f"{len(rel_prompt)} chars, {len(characters)} character summaries",
            )

            id_remap = {f"char_{i + 1}": c.character_id for i, c in enumerate(characters)}
            char_ids = {c.character_id for c in characters}

            def request_relationship_text() -> str:
                if provider in ("openai_compatible", "openai_chat"):
                    chat_client.timeout_seconds = cfg.relationship_timeout_seconds
                    response = chat_client.call_chat([
                        {"role": "system", "content": SYSTEM_RELATIONSHIP},
                        {"role": "user", "content": rel_prompt},
                    ], extra={"max_tokens": 12000})
                    return _extract_azure_text(response)
                if provider == "azure_openai":
                    response = client.call_chat([
                        {"role": "system", "content": SYSTEM_RELATIONSHIP},
                        {"role": "user", "content": rel_prompt},
                    ], extra={"max_tokens": 12000})
                    return _extract_azure_text(response)
                response = client.call(
                    SYSTEM_RELATIONSHIP + "\n\n" + rel_prompt,
                    extra={"max_output_tokens": 12000},
                    timeout_seconds=cfg.relationship_timeout_seconds,
                )
                return _extract_yunwu_text(response)

            relationships, rel_graph = _parse_relationships_with_retry(
                request_relationship_text,
                char_ids=char_ids,
                id_remap=id_remap,
                logger=logger,
            )

            from world_module.character.character_schema import RelationshipGraphNode
            rel_graph.nodes = [
                RelationshipGraphNode(id=c.character_id, name=c.name, type="character")
                for c in characters
            ]

            print(f"[Step4] {len(relationships)} relationships")
            _emit_progress("relationships", "completed", label=f"人物关系网络完成（{len(relationships)} 条关系）",
                           completed=1, total=1)

            # ================================================================
            # Assemble + Write
            # ================================================================
            generated_at = datetime.now(timezone.utc).isoformat()
            result = CharacterSet(
                city_id=city_id, city_name=city_name,
                generated_at=generated_at,
                generation_config={
                    "count": cfg.count, "seed": cfg.seed,
                    "model": model, "stage_model_key": cfg.stage_model_key,
                    "world_path": world_path, "pipeline": "4-step",
                    "identity_validation": identity_validation,
                    "operation_ledger": {
                        "schema_version": "geoword.operation_ledger.v1", "operation": "character_generation",
                        "elapsed_seconds": round(time_mod.time() - t_start, 3),
                        "llm": (chat_client or client).telemetry_snapshot(),
                    },
                },
                characters=characters,
                relationships=relationships,
                relationship_graph=rel_graph,
            )

            out_path = Path(cfg.output_root) / city_id / variant / "characters.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(out_path, result.to_dict())
            _emit_progress("write", "completed", label="人物产物写入完成", completed=len(characters), total=cfg.count)

            duration = time_mod.time() - t_start
            logger.log_summary(len(characters), len(relationships), duration, str(out_path))

            print(
                f"[LLMGenerator] Generated {len(characters)} characters, "
                f"{len(relationships)} relationships → {out_path}"
            )
            return result

        finally:
            logger.close()

    def generate_relationships_only(self, input_path: str) -> CharacterSet:
        """Re-run Step 4 only: regenerate relationships from existing characters.json.

        Reads characters from input_path, builds summaries, calls LLM,
        and writes back to the same file with updated relationships.
        """
        cfg = self.config
        t_start = time_mod.time()

        data = read_json(input_path)
        city_id = data.get("city_id", "unknown")
        city_name = data.get("city_name", "")

        # Reconstruct Character list
        from world_module.character.character_schema import Character
        characters = []
        for cdict in data.get("characters", []):
            mob = cdict.get("mobility", {})
            characters.append(Character(
                character_id=cdict.get("character_id", ""),
                name=cdict.get("name", ""),
                name_romanized=cdict.get("name_romanized", ""),
                age=cdict.get("age", 30),
                gender=cdict.get("gender", ""),
                occupation=cdict.get("occupation", ""),
                personality_traits=cdict.get("personality_traits", []),
                mobility=CharacterMobility(
                    home_district_name=mob.get("home_district_name", ""),
                    home_district_id=mob.get("home_district_id", ""),
                    lat=float(mob.get("lat", 0.0) or 0.0),
                    lng=float(mob.get("lng", 0.0) or 0.0),
                    current_place=dict(mob.get("current_place", {}) or {}),
                ),
                source=cdict.get("source", ""),
            ))

        # Setup LLM
        pipeline_config = PipelineConfig(cfg.config_yaml)
        stage_model_name = pipeline_config.stage_model(cfg.stage_model_key)
        endpoint = pipeline_config.model_endpoint(stage_model_name)
        provider = endpoint.get("provider", "openai_compatible")
        model = endpoint.get("model", "gpt-5.5")

        client = YunwuResponsesClient(
            model=model, config_path=cfg.config_yaml,
            timeout_seconds=cfg.llm_timeout_seconds, endpoint_name=stage_model_name,
        )
        chat_client = OpenAICompatibleChatClient(
            base_url=endpoint.get("base_url", ""), api_key=endpoint.get("api_key", ""), model=model,
            timeout_seconds=cfg.relationship_timeout_seconds, endpoint_name=stage_model_name,
            pricing_reference=endpoint.get("pricing_reference"),
        ) if provider in ("openai_compatible", "openai_chat") else None

        logger = GenerationLogger()
        logger.log_header(input_path, len(characters), model, provider)

        print(f"[RelOnly] Re-generating relationships for {len(characters)} characters...")
        _emit_progress("relationships", "running", label="生成人物关系网络", completed=0, total=1)

        # Build summaries
        summaries = [
            f"{c.name}, {c.age}, {'女' if c.gender == 'female' else '男'}, "
            f"{c.occupation}, {c.mobility.home_district_name}"
            for c in characters
        ]
        rel_prompt = build_relationship_prompt(summaries)
        logger.log_step_prompt(
            "STEP 4 — Relationship Network (REL-ONLY)",
            rel_prompt,
            f"{len(rel_prompt)} chars, {len(characters)} summaries",
        )

        try:
            id_remap = {f"char_{i + 1}": c.character_id for i, c in enumerate(characters)}
            char_ids = {c.character_id for c in characters}

            def request_relationship_text() -> str:
                if provider in ("openai_compatible", "openai_chat"):
                    response = chat_client.call_chat([
                        {"role": "system", "content": SYSTEM_RELATIONSHIP},
                        {"role": "user", "content": rel_prompt},
                    ], extra={"max_tokens": 12000})
                    return _extract_azure_text(response)
                if provider == "azure_openai":
                    response = client.call_chat([
                        {"role": "system", "content": SYSTEM_RELATIONSHIP},
                        {"role": "user", "content": rel_prompt},
                    ], extra={"max_tokens": 12000})
                    return _extract_azure_text(response)
                response = client.call(
                    SYSTEM_RELATIONSHIP + "\n\n" + rel_prompt,
                    extra={"max_output_tokens": 12000},
                    timeout_seconds=cfg.relationship_timeout_seconds,
                )
                return _extract_yunwu_text(response)

            relationships, rel_graph = _parse_relationships_with_retry(
                request_relationship_text,
                char_ids=char_ids,
                id_remap=id_remap,
                logger=logger,
            )

            from world_module.character.character_schema import RelationshipGraphNode
            rel_graph.nodes = [
                RelationshipGraphNode(id=c.character_id, name=c.name, type="character")
                for c in characters
            ]
            print(f"[RelOnly] {len(relationships)} relationships")
            _emit_progress("relationships", "completed", label=f"人物关系网络完成（{len(relationships)} 条关系）",
                           completed=1, total=1)

            # Write back
            data["relationships"] = [r.__dict__ for r in relationships]
            from dataclasses import asdict
            data["relationships"] = _relationships_to_dicts(relationships)
            data["relationship_graph"] = asdict(rel_graph)
            data.setdefault("generation_config", {})["rel_only_rerun"] = True
            data["generation_config"]["operation_ledger"] = {
                "schema_version": "geoword.operation_ledger.v1",
                "operation": "relationship_generation",
                "elapsed_seconds": round(time_mod.time() - t_start, 3),
                "llm": (chat_client or client).telemetry_snapshot(),
            }
            write_json(input_path, data)
            _emit_progress("write", "completed", label="人物产物写入完成", completed=len(characters), total=len(characters))

            duration = time_mod.time() - t_start
            logger.log_summary(len(characters), len(relationships), duration, input_path)
            print(f"[RelOnly] Updated {input_path}")

        finally:
            logger.close()

        return CharacterSet(
            city_id=city_id, city_name=city_name,
            characters=characters,
            relationships=relationships,
            relationship_graph=rel_graph,
        )


def _relationships_to_dicts(relationships: list) -> list[dict]:
    from dataclasses import asdict
    return [asdict(r) for r in relationships]


def _derive_variant(city_id: str) -> str:
    parts = city_id.split("_")
    if parts[-1] in ("base", "large", "complex", "demo"):
        return parts[-1]
    return "complex"


def _extract_azure_text(response: dict[str, Any]) -> str:
    choices = response.get("choices", [])
    if choices and choices[0].get("message", {}).get("content"):
        return choices[0]["message"]["content"]
    return json.dumps(response, ensure_ascii=False)


def _extract_yunwu_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    chunks = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            t = content.get("text")
            if isinstance(t, str):
                chunks.append(t)
    if chunks:
        return "\n".join(chunks)

    def _collect(v):
        if isinstance(v, str) and v.strip():
            chunks.append(v)
        elif isinstance(v, dict):
            for child in v.values():
                _collect(child)
        elif isinstance(v, list):
            for child in v:
                _collect(child)
    _collect(response)
    return "\n".join(chunks) if chunks else json.dumps(response, ensure_ascii=False)


def _infer_country(city_id: str, city_name: str) -> str:
    """Derive country from city_id or city_name prefix."""
    mapping = {
        "tokyo": "Japan", "osaka": "Japan", "kyoto": "Japan",
        "london": "UK", "manchester": "UK", "edinburgh": "UK",
        "paris": "France", "lyon": "France",
        "beijing": "China", "shanghai": "China", "shenzhen": "China",
        "seoul": "Korea", "busan": "Korea",
        "new_york": "USA", "san_francisco": "USA", "chicago": "USA",
        "singapore": "Singapore", "bangkok": "Thailand",
        "dubai": "UAE", "istanbul": "Turkey",
    }
    for key, country in mapping.items():
        if key in city_id.lower() or key in city_name.lower():
            return country
    return ""
