"""Itinerary Generator — Type 5: 长时行程规划.

Pipeline:
  1. Sample character + hotel + multi-category POIs
  2. Build daily event templates
  3. Pre-check all commute times via RouteCache
  4. LLM generates scenario + structured GT
  5. Validate LLM output with rule-based checks
  6. Write task.json + task_sandbox.json + generation_log/
"""

from __future__ import annotations

import json
import logging
import random
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.config_loader import PipelineConfig
from shared.io import read_json, write_json
from shared.maps_client import GoogleMapsClient
from shared.llm_client import OpenAICompatibleChatClient

from task_module.task_schema import BenchmarkTask, WorldSliceSummary
from task_module._shared.base_generator import BaseTaskGenerator, make_folder_name
from task_module._shared.output_writer import write_task_folder
from task_module.task_types.route_planning.route_cache import RouteCache
from world_module.registry import refresh_city_route_cache_asset, write_task_world_provenance

from task_module.task_types.itinerary.sampler import ItinerarySampler
from task_module.task_types.itinerary.constraint_builder import build_scenario
from task_module.task_types.itinerary.validator import run_audit
from task_module.task_types.itinerary.solver import solve_itinerary
from task_module.task_types.itinerary.construction_agents import ItineraryConstructionAgents
from task_module.task_types.itinerary.call_audit import AttemptAudit, audit_totals, audited_generation
from task_module.task_types.itinerary.dag_builder import DagBuildError, build_itinerary_dag
from task_module.task_types.itinerary.requirement_builder import (
    RequirementBuildError,
    build_public_requirements,
)


def infeasible_quota(task_count: int, infeasible_percent: int) -> int:
    """Return a deterministic, half-up target count for a requested batch."""
    return (task_count * infeasible_percent + 50) // 100


def _resolve_itinerary_routing_place_ids(
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


class ItineraryGenerator(BaseTaskGenerator):
    """Generates itinerary (long-horizon planning) benchmark tasks."""

    task_type = "itinerary"
    label_zh = "长时行程规划"
    default_count = 10
    min_chars = 1
    max_chars = 1
    preferred_service_types = ["restaurant_reservation", "attraction_visit", "hotel_booking"]
    env_event_categories: list[str] = []

    def __init__(
        self, world: dict, characters: dict,
        env_events: list, judge_nodes: list,
        output_dir: str, seed: int = 42,
        use_llm_prompt: bool = True,
        config_yaml: str = "config.yaml",
        stage_model_key: str = "scenario_patch",
        infeasible_percent: int = 0,
        route_cache_only: bool = False,
    ):
        super().__init__(
            world=world,
            characters=characters,
            env_events=env_events,
            judge_nodes=judge_nodes,
            output_dir=output_dir,
            seed=seed,
            use_llm_prompt=use_llm_prompt,
            config_yaml=config_yaml,
        )
        self.stage_model_key = stage_model_key
        self.route_cache_only = bool(route_cache_only)
        if not isinstance(infeasible_percent, int) or not 0 <= infeasible_percent <= 100:
            raise ValueError("infeasible_percent must be an integer from 0 to 100")
        self.infeasible_percent = infeasible_percent

    def _persist_routes_before_formalization(self, route_cache: RouteCache) -> None:
        """Make routes observable before a formal task refers to them.

        A managed batch may be interrupted after an individual task folder has
        been written.  Persisting only at the end of the batch leaves that
        otherwise formal task bound to routes that exist solely in memory.
        Refreshing this derived asset deliberately keeps the active city
        version unchanged while giving this task an exact route-cache hash.
        """
        route_cache.save()
        binding = self._task_world_binding
        if not binding:
            return
        refresh_city_route_cache_asset(binding["world_id"], binding["city_id"])
        provenance = write_task_world_provenance(
            binding["world_id"],
            binding["city_id"],
            binding["batch_dir"],
            task_type=binding["task_type"],
            mode=binding["mode"],
            task_folders=[],
        )
        contexts = provenance.get("generation_contexts") or []
        if not contexts or not isinstance(contexts[-1], dict):
            raise RuntimeError("Failed to refresh route-cache provenance context for itinerary task.")
        binding["generation_context"] = contexts[-1]

    @audited_generation
    def generate(self, count: int | None = None) -> list[BenchmarkTask]:
        count = count or self.default_count
        if self.infeasible_percent:
            raise ValueError(
                "incremental itinerary DAG currently formalizes feasible tasks only; "
                "set infeasible_percent=0"
            )
        rng = self.next_generation_rng()

        task_type_dir = self.output_dir / "tasks"
        task_type_dir.mkdir(parents=True, exist_ok=True)

        log_path = self.output_dir / "generate.log"
        _setup_log(log_path)
        logger = logging.getLogger("itinerary")

        route_cache = RouteCache(self.city_id)
        route_pool_path = route_cache.cache_dir / "route_poi_pool.json"
        route_pool_payload = read_json(route_pool_path) if route_pool_path.is_file() else {}
        routing_place_ids = _resolve_itinerary_routing_place_ids(
            self.world, route_pool_payload, list(route_cache._cache.values()),
        )
        if not routing_place_ids:
            raise ValueError(
                "itinerary requires non-empty Routing POI coverage for the active city version"
            )
        maps_client = (
            None
            if self.route_cache_only
            else GoogleMapsClient(PipelineConfig(self.config_yaml)) if self.config_yaml else None
        )

        model_name = "unknown"
        construction_client: OpenAICompatibleChatClient | None = None
        try:
            ep = PipelineConfig(self.config_yaml).model_endpoint(
                PipelineConfig(self.config_yaml).stage_model(self.stage_model_key)
            )
            model_name = ep.get("model", "?")
            construction_client = OpenAICompatibleChatClient(
                base_url=ep.get("base_url", ""),
                api_key=ep.get("api_key", ""),
                model=model_name,
                endpoint_name=PipelineConfig(self.config_yaml).stage_model(self.stage_model_key),
                pricing_reference=ep.get("pricing_reference"),
            )
        except Exception:
            pass

        logger.info("━" * 60)
        logger.info(f"BATCH START {self.output_dir.name}")
        logger.info(f"  CITY {self.city_id}   TYPE {self.task_type}   COUNT {count}   SEED {self.seed}")
        logger.info(f"  LLM {self.stage_model_key}/{model_name}")
        logger.info(f"  ROUTES {'cache-only (0 API)' if self.route_cache_only else 'cache + API'}")
        requested_infeasible_count = infeasible_quota(count, self.infeasible_percent)
        logger.info(
            f"  FEASIBILITY feasible={count - requested_infeasible_count} "
            f"infeasible={requested_infeasible_count} ({self.infeasible_percent}%)"
        )
        logger.info("━" * 60)

        taken = set()
        for f in task_type_dir.iterdir():
            if f.is_dir() and f.name.startswith("task_"):
                try:
                    taken.add(int(f.name.split("_")[1]))
                except (ValueError, IndexError):
                    pass
        next_idx = max(taken, default=0) + 1
        if next_idx > 1:
            logger.info(f"[{self.task_type}] {next_idx - 1} existing tasks, starting at {next_idx}")

        itinerary_sampler = ItinerarySampler(
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

        max_sampling_attempts = max(count * 5, count + 10)
        attempt = 0
        slot_attempt = 0
        while len(tasks) < count and attempt < max_sampling_attempts:
            i = attempt
            attempt += 1
            slot_attempt += 1
            task_idx = next_idx
            srng = self.activate_task_attempt(task_idx, slot_attempt)
            itinerary_sampler = ItinerarySampler(
                self.world, self.characters, route_cache, rng=srng,
                routing_place_ids=routing_place_ids,
            )
            self.set_cost_unit(f"slot_{task_idx:04d}")
            task_id_short = f"task_{task_idx:04d}"
            attempt_audit = AttemptAudit(
                self.output_dir / 'construction_audit' / self._itinerary_audit_run
                / task_id_short / f'attempt_{slot_attempt:04d}',
                {'run_id': self._itinerary_audit_run, 'task_id': task_id_short,
                 'seed_lineage': dict(self._active_seed_lineage),
                 'world_id': (self._task_world_binding or {}).get('world_id', ''),
                 'city_id': self.city_id},
            )
            self._itinerary_attempt_audit = attempt_audit
            self._itinerary_audits.append(attempt_audit)

            character = itinerary_sampler.sample_character()
            if not character:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP no characters available")
                attempt_audit.finish('skipped', 'no characters available')
                continue

            num_days = itinerary_sampler.determine_duration()
            hotel = itinerary_sampler.sample_hotel()
            if not hotel:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP no hotel found")
                attempt_audit.finish('skipped', 'no hotel found')
                continue

            transit_hubs = itinerary_sampler._pick_transit_hubs(hotel["district_id"], count=2)
            if len(transit_hubs) < 1:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP no transit hub found")
                attempt_audit.finish('skipped', 'no transit hub found')
                continue

            scenario = itinerary_sampler.sample_scenario_profile(srng)
            scenario["city_name"] = str(
                ((self.world.get("cities") or [{}])[0]).get("name") or self.city_id
            )
            scenario["daily_scenarios"] = itinerary_sampler.build_scenario_sequence(
                num_days, srng,
            )
            scenario["start_weekday_index"] = itinerary_sampler.sample_start_weekday()
            # Use a deterministic quota rather than a Bernoulli draw so a
            # five-task 20% batch reliably contains one negative task.
            requested_feasibility = (
                "infeasible" if len(tasks) < requested_infeasible_count else "feasible"
            )
            scenario["feasibility_target"] = requested_feasibility
            if not self.use_llm_prompt or construction_client is None:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP incremental DAG requires construction Agents")
                attempt_audit.finish('skipped', 'incremental DAG requires construction Agents')
                continue
            construction_agents = ItineraryConstructionAgents(construction_client, audit=attempt_audit)
            city_activity_candidates = itinerary_sampler.activity_candidates(routing_only=False)
            route_stats_before = route_cache.stats_snapshot()
            dag_result = None
            dag_error: DagBuildError | None = None
            try:
                dag_result = build_itinerary_dag(
                    sampler=itinerary_sampler,
                    agents=construction_agents,
                    character=character,
                    hotel=hotel,
                    transit_hubs=transit_hubs,
                    num_days=num_days,
                    scenario=scenario,
                    maps_client=maps_client,
                )
            except DagBuildError as exc:
                dag_error = exc
            route_stats_after = route_cache.stats_snapshot()
            route_stats_delta = _numeric_delta(route_stats_before, route_stats_after)
            batch_api_calls += int(route_stats_delta.get("api_requests", 0))
            if dag_result is None:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP incremental DAG: {dag_error}")
                attempt_audit.finish('rejected', str(dag_error), failure_phase='dag',
                                     error_class=type(dag_error).__name__)
                continue
            menus = dag_result.menus
            all_pois = dag_result.all_pois
            route_data = dag_result.route_data
            route_queries = dag_result.route_queries
            candidate_attempts = dag_result.candidate_attempts
            scenario["construction_dag"] = dag_result.construction_dag
            try:
                public_requirements, requirement_contracts = build_public_requirements(
                    dag_result.construction_dag, city_activity_candidates,
                    route_cache=route_cache, place_lookup=itinerary_sampler.place_dict,
                    time_repair_client=construction_client, audit=attempt_audit,
                    route_resolver=(lambda origin, destination, mode: route_cache.get_or_compute(
                        origin, destination, mode, maps_client)) if maps_client else None,
                )
            except RequirementBuildError as exc:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP public requirement: {exc}")
                attempt_audit.finish('rejected', str(exc), failure_phase='public_requirements')
                continue
            finally:
                batch_api_calls += int(_numeric_delta(route_stats_after, route_cache.stats_snapshot()).get('api_requests', 0))
            from task_module.task_types.itinerary.time_repair_binding import sync_repaired_menus
            sync_repaired_menus(dag_result.construction_dag, menus, public_requirements, requirement_contracts)
            route_stats_delta = _numeric_delta(route_stats_before, route_cache.stats_snapshot())
            scenario["public_requirements"] = public_requirements
            requirement_dag = {
                "schema_version": "geoword.itinerary.requirement_dag.v1",
                "days": [
                    {
                        "day": day,
                        "activity_groups": [
                            item for item in public_requirements if int(item.get("day") or 0) == day
                        ],
                    }
                    for day in range(1, num_days + 1)
                ],
            }
            scenario["requirement_dag"] = requirement_dag
            total_pois = len(all_pois)

            if requested_feasibility == "infeasible":
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP incremental DAG currently formalizes feasible tasks only")
                attempt_audit.finish('skipped', 'incremental DAG currently formalizes feasible tasks only')
                continue

            if self.use_llm_prompt:
                endpoint = PipelineConfig(self.config_yaml).model_endpoint(
                    PipelineConfig(self.config_yaml).stage_model(self.stage_model_key)
                )
                writer_attempt_limit = 1 + max(0, int(endpoint.get("max_transient_retries", 2)))
                llm_result = None
                token_usage = {"prompt": 0, "completion": 0, "total": 0, "duration_s": 0.0}
                for writer_attempt in range(1, writer_attempt_limit + 1):
                    attempt_result, attempt_usage = build_scenario(
                        character, hotel, menus, route_data,
                        num_days, scenario=scenario, public_requirements=public_requirements,
                        config_yaml=self.config_yaml,
                        stage_model_key=self.stage_model_key,
                        audit=attempt_audit, role_attempt=writer_attempt,
                    )
                    for key in ("prompt", "completion", "total"):
                        token_usage[key] += int(attempt_usage.get(key, 0) or 0)
                    token_usage["duration_s"] += float(attempt_usage.get("duration_s", 0) or 0)
                    if attempt_result:
                        llm_result = attempt_result
                        break
                    action = "retrying the same DAG" if writer_attempt < writer_attempt_limit else "no retries remain"
                    logger.warning(
                        f"[{task_id_short}] writer attempt {writer_attempt}/{writer_attempt_limit} "
                        f"did not satisfy the output contract; {action}"
                    )
            else:
                llm_result = {"user_prompt": _template_user_prompt(character, hotel, menus, num_days)}
                token_usage = {"prompt": 0, "completion": 0, "total": 0, "duration_s": 0}

            if not llm_result:
                raise RuntimeError(
                    f"{task_id_short} writer failed its output contract after "
                    f"{writer_attempt_limit} attempts for the same construction DAG"
                )

            user_prompt = llm_result.get("user_prompt", "")
            # The model only writes the user-facing scenario. The calendar
            # oracle is built from real POIs, fixed events, and slot windows.
            gt = solve_itinerary(menus, character, route_data, scenario)
            if requested_feasibility == "infeasible" and (
                gt.get("expected_feasible") or not gt.get("infeasibility_certificate")
            ):
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP requested infeasible case lacks a solver proof")
                attempt_audit.finish('rejected', 'requested infeasible case lacks a solver proof')
                continue
            if requested_feasibility == "feasible" and not gt.get("expected_feasible"):
                batch_skip += 1
                logger.info(
                    f"[{task_id_short}] SKIP feasible case rejected by temporal graph solver | "
                    f"certificate={json.dumps(gt.get('infeasibility_certificate'), ensure_ascii=False)}"
                )
                attempt_audit.finish('rejected', 'feasible case rejected by temporal graph solver',
                                     certificate=gt.get('infeasibility_certificate'))
                continue
            if not user_prompt or len(user_prompt) < 20:
                batch_skip += 1
                logger.info(
                    f"[{task_id_short}] SKIP empty/bad prompt | "
                    f"{token_usage.get('total', 0)} tok"
                )
                attempt_audit.finish('rejected', 'empty/bad prompt')
                continue

            audit = run_audit({"gt": gt}, menus, all_pois, hotel, route_data)
            if audit.get("warnings"):
                logger.warning(f"[{task_id_short}] audit: {len(audit['warnings'])} warnings")
                for w in audit["warnings"][:5]:
                    logger.warning(f"  [WARN] {w}")

            bt = audit_totals([attempt_audit])
            batch_tokens["prompt"] += bt.get("prompt", 0)
            batch_tokens["completion"] += bt.get("completion", 0)
            batch_tokens["total"] += bt.get("total", 0)
            batch_llm_dur += bt.get("duration_s", 0)

            folder_name = make_folder_name(task_idx, user_prompt)
            task_folder = task_type_dir / folder_name
            task_folder.mkdir(parents=True, exist_ok=False)
            gen_dir = task_folder / "generation_log"
            gen_dir.mkdir(parents=True, exist_ok=True)

            writer_messages = list(llm_result.get("_writer_messages") or [])
            llm_input_text = json.dumps(writer_messages[:2], ensure_ascii=False, indent=2)
            (gen_dir / "llm_in.txt").write_text(llm_input_text, encoding="utf-8")
            (gen_dir / "llm_out.json").write_text(
                json.dumps(llm_result, ensure_ascii=False, indent=2), encoding="utf-8",
            )

            all_place_ids = [p.get("place_id", "") for p in all_pois if p.get("place_id")]
            all_place_ids = list(set(all_place_ids))

            slot_counts = {}
            for m in menus:
                d = m["day"]
                n = (len(m.get("fixed_events", []))
                     + len(m.get("required_slots", []))
                     + len(m.get("optional_slots", [])))
                slot_counts[d] = n

            gt_days = gt.get("correct_itinerary", {}).get("days", [])
            gt_event_count = sum(len(d.get("events", [])) for d in gt_days)

            poi_by_category: dict[str, list[str]] = {}
            for p in all_pois:
                cat = p.get("category", "other")
                poi_by_category.setdefault(cat, []).append(p.get("name", "?"))

            (gen_dir / "details.json").write_text(json.dumps({
                "character": {k: v for k, v in character.items()
                              if k not in ("preferences",)},
                "hotel": {k: hotel.get(k) for k in
                          ("service_id", "place_id", "name", "district_name", "rating", "price_level")},
                "num_days": num_days,
                "scenario": scenario,
                "total_pois": total_pois,
                "poi_by_category": {k: len(v) for k, v in poi_by_category.items()},
                "daily_slot_counts": slot_counts,
                "gt_summary": {
                    "expected_feasible": gt.get("expected_feasible"),
                    "num_days_gt": len(gt_days),
                    "gt_event_count": gt_event_count,
                    "constraints": gt.get("constraints", {}),
                    "infeasibility_certificate": gt.get("infeasibility_certificate"),
                    "acceptable_count": len(gt.get("acceptable_itineraries", [])),
                },
                "generation": {
                    "construction_audit": str(attempt_audit.directory.relative_to(self.output_dir)),
                    "llm_model": model_name,
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
                gt.get("constraints", {}), ensure_ascii=False, indent=2,
            ), encoding="utf-8")

            (gen_dir / "compatibility.json").write_text(json.dumps({
                "hotel_info": {
                    "name": hotel.get("name", ""),
                    "district": hotel.get("district_name", ""),
                    "check_in": "14:00", "check_out": "11:00",
                },
                "poi_names_by_category": poi_by_category,
                "total_commute_pairs": len(route_data),
            }, ensure_ascii=False, indent=2), encoding="utf-8")

            (gen_dir / "route_queries.json").write_text(
                json.dumps(route_queries, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            (gen_dir / "candidate_attempts.json").write_text(
                json.dumps(candidate_attempts, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            (gen_dir / "candidate_comparison.json").write_text(json.dumps({
                contract["requirement_id"]: contract.get("candidate_comparison")
                for contract in requirement_contracts
            }, ensure_ascii=False, indent=2), encoding="utf-8")

            task_sandbox = {
                "task_type": "itinerary",
                "num_days": num_days,
                "character": character,
                "hotel": hotel,
                "all_pois": [
                    {k: p.get(k) for k in ("place_id", "name", "location", "category",
                      "selected_service_type", "primary_service_type", "service_types",
                      "required_service_types",
                      "district_id", "district_name", "rating", "price_level",
                      "price_range", "service_features", "weekly_opening_intervals",
                      "opening_intervals", "duration_typical_min")}
                    for p in all_pois
                ],
                "menus": menus,
                "scenario": scenario,
                "public_requirements": public_requirements,
                "requirement_dag": requirement_dag,
                "requirement_contracts": requirement_contracts,
                "construction_dag": dag_result.construction_dag,
                "route_data": route_data,
                "constraints": gt.get("constraints", {}),
                "expected_feasible": gt.get("expected_feasible", True),
                "correct_itinerary": gt.get("correct_itinerary", {}),
                "acceptable_itineraries": gt.get("acceptable_itineraries", []),
                "temporal_graph": gt.get("temporal_graph", {}),
                "infeasibility_certificate": gt.get("infeasibility_certificate"),
                "_audit": audit,
            }

            chars_for_slice = [{"character_id": character["character_id"], "name": character["name"]}]
            world_slice = self.sampler.build_world_slice(chars_for_slice, all_pois[:15])
            districts_for_slice = {
                (str(p.get("district_id") or ""), str(p.get("district_name") or ""))
                for p in all_pois[:15]
                if p.get("district_id") and p.get("district_name")
            }

            menu_event_count = sum(
                len(m.get("fixed_events", [])) + len(m.get("required_slots", []))
                + len(m.get("optional_slots", []))
                for m in menus
            )

            task = BenchmarkTask(
                task_id=f"task_{self.city_id}_{self.task_type}_{task_idx:04d}",
                task_type=self.task_type, difficulty="medium",
                city_id=self.city_id, variant="",
                generated_at=self.generated_at, user_prompt=user_prompt,
                summary={
                    "character_count": 1,
                    "num_days": num_days,
                    "place_count": total_pois,
                    "event_count": gt_event_count,
                    "menu_slot_count": menu_event_count,
                    "expected_feasible": gt.get("expected_feasible", True),
                    "review_feasibility": (
                        "infeasible_verified"
                        if not gt.get("expected_feasible", True) else "feasible"
                    ),
                },
                world_slice=world_slice,
                world_slice_summary=WorldSliceSummary(
                    characters=chars_for_slice,
                    places=[{"place_id": p.get("place_id", ""), "name": p.get("name", "")}
                            for p in all_pois[:15]],
                    districts=[
                        {"district_id": district_id, "name": district_name}
                        for district_id, district_name in sorted(districts_for_slice)
                    ],
                    corridors=[],
                ),
                involved_env_events=[], env_event_details=[], judge_stages=[],
                metadata={
                    "seed": self._active_seed_lineage["attempt_seed"],
                    "task_type": self.task_type,
                    "feasibility_policy": {
                        "mode": "mixed" if self.infeasible_percent else "feasible_only",
                        "infeasible_percent": self.infeasible_percent,
                        "target": gt.get("expected_feasible", True) is False,
                    },
                },
            )

            write_json(task_folder / "task.json", {
                "task_id": task.task_id, "task_type": self.task_type,
                "difficulty": "medium", "city_id": self.city_id,
                "generated_at": self.generated_at,
                "user_prompt": user_prompt, "summary": task.summary,
                # Human review metadata only. The test runner supplies the
                # evaluated model with user_prompt and public App observations,
                # never this task artifact.
                "review_metadata": {
                    "feasibility": task.summary["review_feasibility"],
                    "reason": (gt.get("infeasibility_certificate") or {}).get("reason", ""),
                    "certificate_present": bool(gt.get("infeasibility_certificate")),
                },
            })
            write_json(task_folder / "task_sandbox.json", task_sandbox)
            write_json(task_folder / "world_slice.json", task.world_slice_summary.to_dict())
            write_json(task_folder / "env_events.json", {"env_events": []})
            write_json(task_folder / "judge_sequence.json", {"stages": []})
            self._persist_routes_before_formalization(route_cache)
            formalization = self.finalize_task_folder(task_folder)
            if not formalization.get('valid'):
                batch_skip += 1
                attempt_audit.finish('rejected', 'formalization rejected', report=formalization)
                logger.warning(f"[{task_id_short}] SKIP formalization rejected: {formalization.get('errors', [])}")
                continue
            batch_success += 1
            attempt_audit.finish('completed', task_folder=task_folder.name)

            tasks.append(task)
            next_idx += 1
            slot_attempt = 0

            logger.info(
                f"[{task_id_short}] OK {num_days}d {gt_event_count}e | "
                f"feasible={gt.get('expected_feasible', True)} | "
                f"{bt.get('prompt', 0)}->{bt.get('completion', 0)} tok | "
                f"{bt.get('duration_s', 0)}s | "
                f"{route_stats_delta.get('api_requests', 0)} API"
            )

        total_dur = round(__import__("time").time() - llm_start_time, 1)
        all_llm_totals = audit_totals(self._itinerary_audits)
        batch_tokens = {key: all_llm_totals[key] for key in ('prompt', 'completion', 'total')}
        batch_llm_dur = all_llm_totals['duration_s']
        logger.info("━" * 60)
        logger.info(f"BATCH SUMMARY  success={batch_success}  skip={batch_skip}")
        logger.info(
            f"  Tokens: {batch_tokens['prompt']}+{batch_tokens['completion']}"
            f"={batch_tokens['total']} | LLM: {batch_llm_dur}s | Total: {total_dur}s"
        )
        logger.info(f"  API: {batch_api_calls} route calls")
        logger.info("━" * 60)

        route_cache.save()

        feasibility_rows = []
        for task_dir in sorted(task_type_dir.glob("task_*")):
            sandbox_path = task_dir / "task_sandbox.json"
            if not sandbox_path.is_file():
                continue
            sandbox = read_json(sandbox_path)
            feasibility_rows.append(bool(sandbox.get("expected_feasible", True)))
        feasibility_summary_path = self.output_dir / "feasibility_summary.json"
        try:
            prior_summary = read_json(feasibility_summary_path) if feasibility_summary_path.is_file() else {}
        except Exception:
            prior_summary = {}
        generation_requests = list(prior_summary.get("generation_requests") or [])
        generation_requests.append({
            "generated_at": self.generated_at,
            "requested_count": count,
            "infeasible_percent": self.infeasible_percent,
            "requested_infeasible_count": requested_infeasible_count,
            "generated_count": len(tasks),
            "generated_infeasible_verified_count": sum(
                not bool((task.summary or {}).get("expected_feasible", True)) for task in tasks
            ),
        })
        write_json(feasibility_summary_path, {
            "schema_version": "geoword.feasibility.v1",
            "task_type": self.task_type,
            "policy": {
                "mode": "mixed" if self.infeasible_percent else "feasible_only",
                "infeasible_percent": self.infeasible_percent,
                "requested_count": count,
                "requested_infeasible_count": requested_infeasible_count,
            },
            "batch_totals": {
                "task_count": len(feasibility_rows),
                "feasible_count": sum(feasibility_rows),
                "infeasible_verified_count": sum(not value for value in feasibility_rows),
            },
            "generation_requests": generation_requests,
        })

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
            raise ValueError("itinerary task contract validation failed")

        return tasks


def _numeric_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, float | int]:
    """Keep per-task route cost telemetry separate from cumulative cache totals."""
    delta: dict[str, float | int] = {}
    for key, value in after.items():
        previous = before.get(key)
        if isinstance(value, (int, float)) and isinstance(previous, (int, float)):
            difference = value - previous
            delta[key] = round(difference, 4) if isinstance(difference, float) else difference
    return delta


def _setup_log(path: Path) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(path), encoding="utf-8"),
        ],
    )


def _extract_prefix(prompt: str, n: int = 5) -> str:
    chinese = re.findall(r'[\u4e00-\u9fff]', prompt)
    return re.sub(r'[<>:"/\\\\|?*]', '', ''.join(chinese[:n])) or "task"


def _template_user_prompt(character: dict, hotel: dict, menus: list[dict], num_days: int) -> str:
    fixed = []
    for menu in menus:
        for event in menu.get("fixed_events", [])[:2]:
            place = event.get("place_name", "已安排地点")
            start = event.get("earliest", event.get("start", "约定时间"))
            fixed.append(f"第{menu.get('day', 1)}天{start}在{place}的安排")
    fixed_text = "；".join(fixed[:4]) or "部分事项已有固定时间"
    return (
        f"我计划入住{hotel.get('name', '酒店')}并停留{num_days}天，请帮我安排一份兼顾出行、办事和休闲的行程。"
        f"{fixed_text}。每天可按早餐、上午活动、午餐、下午活动、傍晚活动和晚餐组织，"
        "没有必要时可以留空；请保证固定安排准时可达，并说明各段之间的交通安排。"
    )
