"""Safety-Aware Planning Generator — Type 6: 安全感知规划.

Formal generation delegates to generation_pipeline: fixed GT, source-backed
activity, shared C, model-authored event, current-task proof, English Writer.
The historical implementation is retained explicitly for regression only.
"""

from __future__ import annotations

import json
import logging
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.config_loader import PipelineConfig
from shared.io import read_json, write_json
from shared.maps_client import GoogleMapsClient

from task_module.task_schema import BenchmarkTask, WorldSliceSummary
from task_module._shared.base_generator import BaseTaskGenerator, make_folder_name
from task_module._shared.sampler import TaskSampler
from task_module.task_types.route_planning.route_cache import RouteCache

from task_module.task_types.safety_planning.sampler import SafetySampler
from task_module.task_types.safety_planning.constraint_builder import build_scenario, build_static_risk_proposal
from task_module.task_types.safety_planning.validator import run_audit
from task_module.task_types.safety_planning.solver import solve_safety_plan
from task_module.task_types.safety_planning.static_app_state import compile_static_safety_snapshot


def _resolve_safety_routing_place_ids(
    world: dict[str, Any], route_pool_payload: dict, route_entries: list[dict],
) -> set[str]:
    """Resolve active-version Routing coverage, including legacy caches."""
    places = [place for place in world.get("places") or [] if isinstance(place, dict)]
    places_by_id = {
        str(place.get("place_id") or ""): place for place in places if place.get("place_id")
    }
    manifest_ids = {
        str(place_id) for place_id in route_pool_payload.get("place_ids") or []
        if str(place_id) in places_by_id
    }
    if manifest_ids:
        return manifest_ids

    coordinate_to_id = {
        (round(float(location["lat"]), 6), round(float(location["lng"]), 6)):
            str(place.get("place_id") or "")
        for place in places
        if isinstance((location := place.get("location")), dict)
        and location.get("lat") is not None and location.get("lng") is not None
        and place.get("place_id")
    }
    compatible_ids: set[str] = set()
    for entry in route_entries:
        if not isinstance(entry, dict):
            continue
        for prefix in ("origin", "dest"):
            place_id = str(entry.get(f"{prefix}_place_id") or "")
            if place_id in places_by_id:
                compatible_ids.add(place_id)
                continue
            try:
                mapped = coordinate_to_id.get((
                    round(float(entry.get(f"{prefix}_lat")), 6),
                    round(float(entry.get(f"{prefix}_lng")), 6),
                ))
            except (TypeError, ValueError):
                mapped = None
            if mapped:
                compatible_ids.add(mapped)
    return compatible_ids


class SafetyPlanningGenerator(BaseTaskGenerator):
    task_type = "safety_planning"
    label_zh = "安全感知规划"
    default_count = 10
    min_chars = 1
    max_chars = 1

    def __init__(self, world: dict, characters: dict,
                 env_events: list, judge_nodes: list,
                 output_dir: str, seed: int = 42,
                 use_llm_prompt: bool = True,
                 config_yaml: str = "config.yaml",
                 stage_model_key: str = "scenario_patch",
                 safety_assets: dict | None = None,
                 safety_time_sources: dict | None = None,
                 max_attempts_per_slot: int = 3,
                 route_cache_only: bool = False):
        super().__init__(
            world=world, characters=characters, env_events=env_events,
            judge_nodes=judge_nodes, output_dir=output_dir, seed=seed,
            use_llm_prompt=use_llm_prompt, config_yaml=config_yaml,
        )
        self.stage_model_key = stage_model_key
        self.sampler_shared = self.sampler
        self.safety_assets = safety_assets
        self.safety_time_sources = safety_time_sources
        self.max_attempts_per_slot = max_attempts_per_slot
        self.route_cache_only = route_cache_only

    def generate(self, count: int | None = None) -> list[BenchmarkTask]:
        from task_module.task_types.safety_planning.generation_pipeline import generate_choices
        return generate_choices(self, self.default_count if count is None else count)

    def generate_legacy(self, count: int | None = None) -> list[BenchmarkTask]:
        """Historical implementation retained for regression, never a fallback."""
        count = count or self.default_count
        rng = self.next_generation_rng()

        task_type_dir = self.output_dir / "tasks"
        task_type_dir.mkdir(parents=True, exist_ok=True)

        log_path = self.output_dir / "generate.log"
        _setup_log(log_path)
        logger = logging.getLogger("safety")

        route_cache = RouteCache(self.city_id)
        route_pool_path = route_cache.cache_dir / "route_poi_pool.json"
        route_pool_payload = read_json(route_pool_path) if route_pool_path.is_file() else {}
        routing_place_ids = _resolve_safety_routing_place_ids(
            self.world, route_pool_payload, list(route_cache._cache.values()),
        )
        if not routing_place_ids:
            raise ValueError(
                "safety_planning requires non-empty Routing POI coverage for the active city version"
            )
        maps_client = GoogleMapsClient(PipelineConfig(self.config_yaml)) if self.config_yaml else None

        model_name = "unknown"
        try:
            ep = PipelineConfig(self.config_yaml).model_endpoint(
                PipelineConfig(self.config_yaml).stage_model(self.stage_model_key)
            )
            model_name = ep.get("model", "?")
        except Exception:
            pass

        logger.info("━" * 60)
        logger.info(f"BATCH START {self.output_dir.name}")
        logger.info(f"  CITY {self.city_id}   TYPE {self.task_type}   COUNT {count}   SEED {self.seed}")
        logger.info(f"  LLM {self.stage_model_key}/{model_name}")
        logger.info("━" * 60)

        taken = set()
        for f in task_type_dir.iterdir():
            if f.is_dir() and f.name.startswith("task_"):
                try:
                    taken.add(int(f.name.split("_")[1]))
                except (ValueError, IndexError):
                    pass
        next_idx = max(taken, default=0) + 1
        if taken:
            logger.info(f"[{self.task_type}] {len(taken)} existing tasks, starting at {next_idx}")

        sampler = SafetySampler(
            self.world, self.characters, route_cache, rng=rng,
            routing_place_ids=routing_place_ids,
        )

        tasks: list[BenchmarkTask] = []
        batch_tokens = {"prompt": 0, "completion": 0, "total": 0}
        batch_llm_dur = 0.0
        batch_api_calls = 0
        batch_success = 0
        batch_skip = 0
        llm_start_time = __import__("time").time()

        slot_attempt = 0
        for i in range(count):
            slot_attempt += 1
            task_idx = next_idx
            slot_rng = self.activate_task_attempt(task_idx, slot_attempt)
            sampler = SafetySampler(
                self.world, self.characters, route_cache, rng=slot_rng,
                routing_place_ids=routing_place_ids,
            )
            self.set_cost_unit(f"slot_{task_idx:04d}")
            task_id_short = f"task_{task_idx:04d}"

            character = sampler.sample_character()
            if not character:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP no characters")
                continue

            risky = sampler.sample_risky_target()
            if not risky:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP no factual service target")
                continue

            risk_seed = sampler.sample_static_risk_seed(risky, character)
            if not risk_seed:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP no factually eligible static risk")
                continue
            if self.use_llm_prompt:
                risk_proposal, risk_usage = build_static_risk_proposal(
                    risky, risk_seed, config_yaml=self.config_yaml, stage_model_key=self.stage_model_key,
                )
                if not risk_proposal:
                    batch_skip += 1
                    logger.info(f"[{task_id_short}] SKIP static-risk LLM API failed | {risk_usage.get('total', 0)} tok")
                    continue
            else:
                risk_proposal, risk_usage = {}, {"prompt": 0, "completion": 0, "total": 0, "duration_s": 0}
            static_app_state, evidence, risk_case = compile_static_safety_snapshot(
                task_id=f"{self.city_id}_{task_id_short}", risky_target=risky,
                risk_seed=risk_seed, proposal=risk_proposal,
            )
            # The private GT derives from the same frozen App state which the
            # evaluation runtime loads before the first Agent tool call.
            risky["risk_evidence"] = evidence

            alternatives = sampler.sample_alternatives(risky, evidence, count=3)
            if len(alternatives) < 2:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP only {len(alternatives)} alternatives")
                continue

            distractors = sampler.sample_distractors(risky, alternatives, evidence, count=2)

            route_stats_before = route_cache.stats_snapshot()
            route_data, route_queries = sampler.precheck_commutes(
                character, risky, alternatives, distractors, maps_client,
            )
            route_stats_delta = _numeric_delta(route_stats_before, route_cache.stats_snapshot())
            batch_api_calls += int(route_stats_delta.get("api_requests", 0))

            attempt_path = self.output_dir / "candidate_attempts.json"
            attempts = read_json(attempt_path) if attempt_path.exists() else {"attempts": []}
            attempt_record = {
                "task_index": task_idx, "seed_lineage": dict(self._active_seed_lineage or {}),
                "status": "pending", "reason": "routes_prepared",
                "candidate_place_ids": [item.get("place_id") for item in [risky, *alternatives, *distractors]],
                "route_queries": route_queries, "route_cost_delta": route_stats_delta,
                "excluded_from_structural_upper_bound_if_rejected": True,
            }
            attempts["attempts"].append(attempt_record)

            def record_attempt(status: str, reason: str) -> None:
                attempt_record.update(status=status, reason=reason)
                write_json(attempt_path, attempts)

            record_attempt("pending", "routes_prepared")

            if self.use_llm_prompt:
                llm_result, token_usage = build_scenario(
                    character, risky, evidence, alternatives, distractors, route_data,
                    config_yaml=self.config_yaml, stage_model_key=self.stage_model_key,
                )
            else:
                llm_result = {"user_prompt": _template_user_prompt(character, risky)}
                token_usage = {"prompt": 0, "completion": 0, "total": 0, "duration_s": 0}

            if not llm_result:
                record_attempt("rejected", "writer_request_or_output_failed")
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP LLM API failed | {token_usage.get('total',0)} tok")
                continue

            user_prompt = llm_result.get("user_prompt", "")
            # The language model drafts only the natural-language scenario.
            # Factual risk, rejection, and safe alternatives are private GT.
            gt = solve_safety_plan(character, risky, evidence, alternatives, distractors, route_data, risk_case)
            if not user_prompt or len(user_prompt) < 20:
                record_attempt("rejected", "empty_writer_prompt")
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP empty prompt | {token_usage.get('total',0)} tok")
                continue
            if not gt.get("counterfactual", {}).get("decision_changed"):
                record_attempt("rejected", "risk_does_not_change_decision")
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP risk does not change the counterfactual decision")
                continue

            audit = run_audit({"gt": gt}, risky, alternatives, route_data)
            if audit.get("warnings"):
                logger.warning(f"[{task_id_short}] audit: {len(audit['warnings'])} warnings")
                for w in audit["warnings"][:5]:
                    logger.warning(f"  ⚠ {w}")

            bt = token_usage
            batch_tokens["prompt"] += bt.get("prompt", 0) + risk_usage.get("prompt", 0)
            batch_tokens["completion"] += bt.get("completion", 0) + risk_usage.get("completion", 0)
            batch_tokens["total"] += bt.get("total", 0) + risk_usage.get("total", 0)
            batch_llm_dur += bt.get("duration_s", 0) + risk_usage.get("duration_s", 0)

            folder_name = make_folder_name(task_idx, user_prompt)
            task_folder = task_type_dir / folder_name
            task_folder.mkdir(parents=True, exist_ok=False)
            gen_dir = task_folder / "generation_log"
            gen_dir.mkdir(parents=True, exist_ok=True)

            if token_usage.get("messages"):
                write_json(gen_dir / "writer_messages.json", {"messages": token_usage["messages"]})

            llm_in = _build_llm_input_doc(character, risky, evidence)
            (gen_dir / "llm_in.txt").write_text(llm_in, encoding="utf-8")
            (gen_dir / "llm_out.json").write_text(json.dumps(llm_result, ensure_ascii=False, indent=2), encoding="utf-8")
            (gen_dir / "risk_llm_out.json").write_text(json.dumps(risk_proposal, ensure_ascii=False, indent=2), encoding="utf-8")

            risk_dims = sorted({card.get("dimension", "") for card in evidence if card.get("dimension")})
            (gen_dir / "details.json").write_text(json.dumps({
                "character": {k: v for k, v in character.items() if k not in ("preferences",)},
                "risky_target": {k: risky.get(k) for k in ("place_id", "name", "district_name", "risk_tags")},
                "safety_risk_case": risk_case,
                "risk_evidence": evidence,
                "risk_dimensions": risk_dims,
                "alternatives_count": len(alternatives),
                "distractors_count": len(distractors),
                "gt_summary": {
                    "expected_safe": gt.get("expected_safe"),
                    "recommendation": (gt.get("expected_plan") or {}).get("recommendation", "?"),
                    "alt_count": len((gt.get("expected_plan") or {}).get("alternatives", [])),
                    "constraints": gt.get("constraints", {}),
                    "causal_chain": gt.get("causal_chain", {}),
                },
                "generation": {
                    "llm_model": model_name,
                    "risk_llm_duration_s": risk_usage.get("duration_s", 0),
                    "risk_tokens_total": risk_usage.get("total", 0),
                    "llm_duration_s": bt.get("duration_s", 0),
                    "tokens_prompt": bt.get("prompt", 0),
                    "tokens_completion": bt.get("completion", 0),
                    "tokens_total": bt.get("total", 0),
                    "route_graph_edge_count": len(route_data),
                    "route_cache_statistics": route_stats_delta,
                    "audit_warnings": len(audit.get("warnings", [])),
                },
            }, ensure_ascii=False, indent=2), encoding="utf-8")

            (gen_dir / "constraints.json").write_text(json.dumps(
                gt.get("constraints", {}), ensure_ascii=False, indent=2), encoding="utf-8")
            (gen_dir / "compatibility.json").write_text(json.dumps({
                "risk_dimensions": risk_dims,
                "risk_evidence": evidence,
                "alternatives": [{"name": a["name"], "indoor": a["indoor"], "dist_m": a.get("distance_m", 0)}
                                 for a in alternatives],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            (gen_dir / "route_queries.json").write_text(json.dumps(route_queries, ensure_ascii=False, indent=2), encoding="utf-8")

            task_sandbox = {
                "task_type": "safety_planning",
                "static_app_state_ref": "static_app_state.json",
                "character": character,
                "risky_target": risky,
                "risk_evidence": evidence,
                "alternatives": alternatives,
                "distractors": distractors,
                "route_data": route_data,
                "constraints": gt.get("constraints", {}),
                "expected_safe": gt.get("expected_safe"),
                "original_plan_safe": gt.get("original_plan_safe"),
                "safe_plan_feasible": gt.get("safe_plan_feasible"),
                "expected_plan": gt.get("expected_plan", {}),
                "acceptable_plans": gt.get("acceptable_plans", []),
                "risk_factors": gt.get("risk_factors", []),
                "causal_chain": gt.get("causal_chain", {}),
                "counterfactual": gt.get("counterfactual", {}),
                "safety_risk_case": gt.get("safety_risk_case", {}),
                "_audit": audit,
            }

            all_pois = [risky] + alternatives + distractors
            chars_for_slice = [{"character_id": character["character_id"], "name": character["name"]}]
            world_slice = self.sampler_shared.build_world_slice(chars_for_slice, all_pois[:10])

            task = BenchmarkTask(
                task_id=f"task_{self.city_id}_{self.task_type}_{task_idx:04d}",
                task_type=self.task_type, difficulty="medium",
                city_id=self.city_id, variant="",
                generated_at=self.generated_at, user_prompt=user_prompt,
                summary={
                    "character_count": 1,
                    "place_count": len(all_pois),
                    "expected_safe": gt.get("expected_safe"),
                    "original_plan_safe": gt.get("original_plan_safe"),
                    "safe_plan_feasible": gt.get("safe_plan_feasible"),
                    "risk_dimensions": risk_dims,
                    "risk_family": risk_case.get("family", ""),
                },
                world_slice=world_slice,
                world_slice_summary=WorldSliceSummary(
                    characters=chars_for_slice,
                    places=[{"place_id": p.get("place_id", ""), "name": p.get("name", "")}
                            for p in all_pois[:10]],
                    districts=[], corridors=[],
                ),
                involved_env_events=[], env_event_details=[], judge_stages=[],
                metadata={"seed": self._active_seed_lineage["attempt_seed"], "task_type": self.task_type},
            )

            write_json(task_folder / "task.json", {
                "task_id": task.task_id, "task_type": self.task_type,
                "difficulty": "medium", "city_id": self.city_id,
                "generated_at": self.generated_at,
                "user_prompt": user_prompt, "summary": task.summary,
            })
            write_json(task_folder / "task_sandbox.json", task_sandbox)
            write_json(task_folder / "static_app_state.json", static_app_state)
            write_json(task_folder / "world_slice.json", task.world_slice_summary.to_dict())
            write_json(task_folder / "env_events.json", {"env_events": []})
            write_json(task_folder / "judge_sequence.json", {"stages": []})
            try:
                report = self.finalize_task_folder(task_folder)
            except Exception:
                record_attempt("rejected", "formalization_failed")
                raise
            if not report.get("valid"):
                attempt_record["formalization_errors"] = report.get("errors", [])
                attempt_record["candidate_path"] = report.get("task_dir", str(task_folder))
                record_attempt("rejected", "formalization_failed")
                batch_skip += 1
                logger.warning(f"[{task_id_short}] SKIP formalization failed: {report.get('errors', [])}")
                continue
            record_attempt("accepted", "formalized")
            batch_success += 1
            tasks.append(task)
            next_idx += 1
            slot_attempt = 0

            logger.info(
                f"[{task_id_short}] OK risk={risk_case.get('family', '')} | "
                f"safe={gt.get('expected_safe')} | "
                f"{bt.get('prompt',0)}->{bt.get('completion',0)} tok | "
                f"{bt.get('duration_s',0)}s | "
                f"{route_stats_delta.get('api_requests', 0)} API"
            )

        total_dur = round(__import__("time").time() - llm_start_time, 1)
        logger.info("━" * 60)
        logger.info(f"BATCH SUMMARY  success={batch_success}  skip={batch_skip}")
        logger.info(f"  Tokens: {batch_tokens['prompt']}+{batch_tokens['completion']}={batch_tokens['total']} | "
                     f"LLM: {batch_llm_dur}s | Total: {total_dur}s")
        logger.info(f"  API: {batch_api_calls} route calls")
        logger.info("━" * 60)

        route_cache.save()

        from task_module._shared.generation_telemetry import write_generation_telemetry
        write_generation_telemetry(
            self.output_dir, task_type=self.task_type, city_id=self.city_id,
            requested_count=count, generated_count=len(tasks), elapsed_s=total_dur,
            route_cache=route_cache, llm_usage=batch_tokens, skipped_count=batch_skip,
        )

        from task_module._shared.task_contract import canonicalize_batch
        city = (self.world.get("cities") or [{}])[0]
        reports = canonicalize_batch(self.output_dir, self.task_type, self.city_id, city.get("timezone", "UTC"))
        if any(not report["valid"] for report in reports):
            raise ValueError("safety task contract validation failed")

        return tasks


def _setup_log(path: Path) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(str(path), encoding="utf-8")])


def _extract_prefix(prompt: str, n: int = 5) -> str:
    chinese = re.findall(r'[\u4e00-\u9fff]', prompt)
    return re.sub(r'[<>:"/\\\\|?*]', '', ''.join(chinese[:n])) or "task"


def _numeric_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, float | int]:
    return {
        key: round(value - before[key], 4) if isinstance(value, float) else value - before[key]
        for key, value in after.items()
        if isinstance(value, (int, float)) and isinstance(before.get(key), (int, float))
    }


def _build_llm_input_doc(character: dict, risky: dict, evidence: list[dict]) -> str:
    from task_module.task_types.safety_planning.natural_prompt import (
        SYSTEM_SAFETY_NATURAL, build_natural_user_input,
    )
    user = build_natural_user_input(character, risky, evidence)
    return f"=== SYSTEM ===\n{SYSTEM_SAFETY_NATURAL}\n\n=== USER ===\n{user}"


def _template_user_prompt(character: dict, risky: dict) -> str:
    return (
        f"我想前往{risky.get('name', '目标地点')}完成一次出行活动。"
        "请先查询当前天气、道路和地点服务状态，判断原计划是否安全可行；"
        "如有风险，请给出明确的调整方式或更安全的替代方案。"
    )
