"""Route Planning Generator — builds tasks with Google Maps route data.

For each task:
  1. Sample core POIs (origin, dest ± waypoint) + distractor POIs (10-15)
  2. Generate route pairs and cache via Google Maps API
  3. Build route_sandbox.json
  4. Generate user_prompt + judge_sequence
  5. Write task folder

Route pairs are cached in outputs/routes/{city_id}/route_cache.json
to avoid duplicate API calls across tasks.

Usage:
    from task_module.task_types.route_planning.generator import RoutePlanningGenerator
    gen = RoutePlanningGenerator(world, characters, env_events, judge_nodes, output_dir)
    tasks = gen.generate(count=5)
"""

from __future__ import annotations

import os
import random
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.config_loader import PipelineConfig
from shared.io import read_json, write_json
from shared.maps_client import GoogleMapsClient

from task_module._shared.base_generator import BaseTaskGenerator, make_folder_name
from task_module._shared.sampler import TaskSampler
from task_module._shared.output_writer import write_task_folder
from task_module.task_schema import (
    BenchmarkTask, EnvEventNode, JudgeNode, JudgeStage, WorldSliceSummary,
)

from task_module.task_types.route_planning.route_cache import RouteCache
from task_module.task_types.route_planning.route_pool import RoutePoiPool
from task_module.task_types.route_planning.sampler import RouteSampler
from task_module.task_types.route_planning.pool_policy import (
    core_places as _core_places,
    sample_route_pool_context as _sample_route_pool_context,
)
from task_module.task_types.route_planning.prompts import generate_user_prompt
from task_module._shared.task_contract import assigned_time_context


ROUTE_SUBGRAPH_MAX_NEW_API_REQUESTS = 1800
ROUTE_SUBGRAPH_MAX_ELAPSED_S = 600.0


class RoutePlanningGenerator(BaseTaskGenerator):
    """Generates route planning benchmark tasks with real Google Maps data."""

    task_type = "route_planning"
    label_zh = "地理空间规划"
    default_count = 150
    min_chars = 1
    max_chars = 2
    preferred_service_types = ["restaurant_reservation", "hotel_booking"]
    env_event_categories = ["weather", "transit", "system"]

    def generate(self, count: int | None = None) -> list[BenchmarkTask]:
        count = count or self.default_count
        rng = self.next_generation_rng()

        task_type_dir = self.output_dir / "tasks"
        task_type_dir.mkdir(parents=True, exist_ok=True)

        import logging
        log_path = self.output_dir / "generate.log"
        _setup_log(log_path)
        logger = logging.getLogger("route_planning")
        logger.info("━" * 60)
        logger.info(f"BATCH START {self.output_dir.name}")
        logger.info(f"  CITY {self.city_id}   TYPE route_planning   COUNT {count}   SEED {self.seed}")
        logger.info("━" * 60)
        llm_ok = 0
        llm_fail = 0

        # Setup
        pipeline_config = PipelineConfig(self.config_yaml)
        route_cache = RouteCache(self.city_id)
        batch_cache_stats_before = route_cache.stats_snapshot()
        route_ids_before_batch = set(route_cache._route_id_index)
        formal_route_reuse_hits = 0
        formal_coordinate_reuse_hits = 0
        route_pool_max_pois = int(os.environ.get(
            "GEOWORD_ROUTE_POOL_MAX",
            pipeline_config.get("route_planning.route_pool_max_pois", 50),
        ))
        route_pool_diffusion_start = int(os.environ.get(
            "GEOWORD_ROUTE_DIFFUSION_START",
            pipeline_config.get("route_planning.route_pool_diffusion_start", 20),
        ))
        if route_pool_diffusion_start >= route_pool_max_pois:
            raise ValueError("route_pool_diffusion_start must be smaller than route_pool_max_pois")
        route_pool = RoutePoiPool(
            route_cache, self.world, snapshot_id="static_v1", max_pois=route_pool_max_pois,
        )
        maps_client = GoogleMapsClient(pipeline_config)

        sampler = RouteSampler(self.world, self.characters, rng=rng)
        task_sampler = TaskSampler(self.world, self.characters, seed=self.seed)

        generated_at = datetime.now(timezone.utc).isoformat()
        city = (self.world.get("cities") or [{}])[0]
        city_timezone = city.get("timezone", "UTC")
        t_batch_start = __import__("time").time()
        tasks: list[BenchmarkTask] = []

        print(f"[RoutePlanning] Generating {count} tasks for {self.city_id}...")

        # Scan existing task folders → start numbering from next available index
        taken_idx: set[int] = set()
        for f in task_type_dir.iterdir():
            if f.is_dir() and f.name.startswith("task_"):
                try:
                    taken_idx.add(int(f.name.split("_")[1]))
                except (ValueError, IndexError):
                    pass
        next_idx = max(taken_idx, default=0) + 1
        if next_idx > 1:
            print(f"[RoutePlanning] {next_idx - 1} existing tasks, starting at index {next_idx}")

        max_sampling_attempts = max(count * 5, count + 10)
        attempt = 0
        slot_attempt = 0
        while len(tasks) < count and attempt < max_sampling_attempts:
            i = attempt
            attempt += 1
            slot_attempt += 1
            t0 = __import__("time").time()
            task_idx = next_idx
            srng = self.activate_task_attempt(task_idx, slot_attempt)
            sampler = RouteSampler(self.world, self.characters, rng=srng)
            self.set_cost_unit(f"slot_{task_idx:04d}")
            task_id_short = f"task_{task_idx:04d}"
            task_id = f"task_{self.city_id}_{self.task_type}_{task_idx:04d}"
            time_context = assigned_time_context(task_id, self.city_id, city_timezone)
            # Static route facts belong to the frozen city world.  The task's
            # instruction time remains available in time_context; persistent
            # changes are represented only by the dynamic overlay layer.
            route_snapshot_id = "static_v1"

            # Recovery and legacy initialization finish the current matrix
            # before another task is allowed to enlarge the POI pool.
            existing_pool_pairs = (
                sampler.generate_pool_pairs(route_pool.places)
                if route_pool.graph_mode == "dense" else []
            )
            existing_pool_missing = [
                pair for pair in existing_pool_pairs
                if route_cache.get(
                    pair["from"], pair["to"], pair["mode"], snapshot_id=route_snapshot_id,
                ) is None
            ]
            if existing_pool_missing:
                chunk = existing_pool_missing[:ROUTE_SUBGRAPH_MAX_NEW_API_REQUESTS]
                logger.info(
                    f"[{task_id_short}] MATERIALIZE existing route matrix "
                    f"{len(chunk)}/{len(existing_pool_missing)} missing units"
                )
                for pair in chunk:
                    route_cache.get_or_compute(
                        pair["from"], pair["to"], pair["mode"], maps_client,
                        snapshot_id=route_snapshot_id,
                    )
                route_cache.save()
                continue

            # 1. Sample formal POIs and update the version-bound shared route pool.
            core, distractors, pool_stage = _sample_route_pool_context(
                sampler, route_pool, srng, diffusion_start=route_pool_diffusion_start,
            )
            if not core["origin"] or not core["dest"]:
                print(f"  [{i}] Skipped: could not sample valid origin-dest pair")
                continue

            origin = core["origin"]
            dest = core["dest"]
            waypoints = core.get("waypoints", [])
            waypoint = waypoints[0] if waypoints else None
            main_mode = core["mode"]
            sampled_leg_modes = list(core.get("leg_modes") or [main_mode] * (len(waypoints) + 1))

            pool_places = route_pool.places

            # 2. New cities keep a complete matrix. A legacy sparse graph only
            # connects this attempt's newly added POIs; old gaps remain untouched.
            if route_pool.graph_mode == "legacy_sparse":
                new_ids = {place.get("place_id") for place in route_pool.last_added}
                pairs = [
                    pair for pair in sampler.generate_pool_pairs(pool_places)
                    if pair["from"].get("place_id") in new_ids
                    or pair["to"].get("place_id") in new_ids
                ]
            else:
                pairs = sampler.generate_pool_pairs(pool_places)
            subgraph_budget = {
                "poi_count": len(pool_places),
                "route_pool_stage": pool_stage,
                "route_pool_max_pois": route_pool.max_pois,
                "directed_mode_pair_count": len(pairs),
                "snapshot_id": route_snapshot_id,
                "max_new_api_requests": ROUTE_SUBGRAPH_MAX_NEW_API_REQUESTS,
                "max_elapsed_s": ROUTE_SUBGRAPH_MAX_ELAPSED_S,
            }
            missing_pairs = [
                pair for pair in pairs
                if route_cache.get(
                    pair["from"], pair["to"], pair["mode"], snapshot_id=route_snapshot_id,
                ) is None
            ]
            planned_misses = len(missing_pairs)
            planned_hits = len(pairs) - planned_misses
            subgraph_budget["planned_cache_hits"] = planned_hits
            subgraph_budget["planned_new_api_requests"] = planned_misses
            if planned_misses > ROUTE_SUBGRAPH_MAX_NEW_API_REQUESTS:
                logger.info(
                    f"[{task_id_short}] MATERIALIZE partial route matrix "
                    f"{ROUTE_SUBGRAPH_MAX_NEW_API_REQUESTS}/{planned_misses} missing units"
                )
                for pair in missing_pairs[:ROUTE_SUBGRAPH_MAX_NEW_API_REQUESTS]:
                    route_cache.get_or_compute(
                        pair["from"], pair["to"], pair["mode"], maps_client,
                        snapshot_id=route_snapshot_id,
                    )
                route_cache.save()
                continue

            all_places = [origin, *waypoints, dest]
            all_places.extend(distractors)

            # 3. Cache routes
            routes_in_task: list[dict] = []
            cache_stats_before = route_cache.stats_snapshot()
            subgraph_started_at = time.monotonic()
            subgraph_timed_out = False

            task_place_ids = {
                place.get("place_id") for place in [origin, *waypoints, dest, *distractors]
            }
            for pair in pairs:
                if time.monotonic() - subgraph_started_at > ROUTE_SUBGRAPH_MAX_ELAPSED_S:
                    subgraph_timed_out = True
                    logger.info(
                        f"[{task_id_short}] SKIP subgraph wall-clock budget "
                        f">{ROUTE_SUBGRAPH_MAX_ELAPSED_S:.0f}s"
                    )
                    break
                cached = route_cache.get_or_compute(
                    pair["from"], pair["to"], pair["mode"], maps_client,
                    snapshot_id=route_snapshot_id,
                )
                if (
                    cached and cached.get("route", {}).get("available") is True
                    and pair["from"].get("place_id") in task_place_ids
                    and pair["to"].get("place_id") in task_place_ids
                ):
                    routes_in_task.append(cached)
            cache_stats_after = route_cache.stats_snapshot()
            subgraph_budget["cache_hits"] = int(cache_stats_after["cache_hits"]) - int(cache_stats_before["cache_hits"])
            subgraph_budget["cache_misses"] = int(cache_stats_after["cache_misses"]) - int(cache_stats_before["cache_misses"])
            subgraph_budget["api_requests"] = int(cache_stats_after["api_requests"]) - int(cache_stats_before["api_requests"])
            subgraph_budget["api_retries"] = int(cache_stats_after["api_retries"]) - int(cache_stats_before["api_retries"])
            subgraph_budget["elapsed_s"] = round(time.monotonic() - subgraph_started_at, 3)
            subgraph_budget["timed_out"] = subgraph_timed_out
            if subgraph_timed_out:
                route_cache.save()
                continue

            # Find correct route
            correct_route_id = ""
            if main_mode != "MIXED":
                correct_key = route_cache._make_key(origin, dest, main_mode, snapshot_id=route_snapshot_id)
                correct_route_id = route_cache._cache.get(correct_key, {}).get("route_id", "") if correct_key in route_cache._cache else ""

            # For waypoint tasks, collect leg route_ids (origin→waypoint + waypoint→dest)
            correct_leg_route_ids = []
            required_chain = [origin, *waypoints, dest]
            for leg_origin, leg_destination, leg_mode in zip(
                required_chain, required_chain[1:], sampled_leg_modes,
            ):
                leg_key = route_cache._make_key(
                    leg_origin, leg_destination, leg_mode, snapshot_id=route_snapshot_id,
                )
                leg_id = route_cache._cache.get(leg_key, {}).get("route_id", "")
                if leg_id:
                    correct_leg_route_ids.append(leg_id)

            # Derive constraints from cached routes (constraint-based evaluation)
            constraint_data = sampler.derive_constraints_for_stops(
                core, route_cache, rng=srng, snapshot_id=route_snapshot_id,
            )
            if constraint_data.get("error"):
                logger.info(f"[{task_id_short}] SKIP incomplete required API route data")
                continue
            expected_feasible = constraint_data.get("expected_feasible", True)
            time_limit_min = constraint_data.get("time_budget_min", max(1, core["time_limit_s"] // 60))
            stop_duration_min = constraint_data.get("stop_duration_min", 0)
            if expected_feasible and constraint_data.get("acceptable_plan_ids"):
                # The private chain must be an API-materialized feasible plan,
                # not a legacy preference for one sampled transport mode.
                correct_leg_route_ids = list(constraint_data["acceptable_plan_ids"][0])
            resolved_leg_modes = [
                str((route_cache._route_id_index.get(route_id) or {}).get("mode") or "")
                for route_id in correct_leg_route_ids
            ]
            resolved_leg_modes = [mode for mode in resolved_leg_modes if mode]
            summary_mode = (
                resolved_leg_modes[0]
                if resolved_leg_modes and len(set(resolved_leg_modes)) == 1
                else "MIXED" if resolved_leg_modes else main_mode
            )
            selected_coordinate_reuse_hits = 0
            selected_route_reuse_hits = 0
            for route_id in correct_leg_route_ids:
                selected_entry = route_cache._route_id_index.get(route_id) or {}
                if selected_entry.get("reuse_type") == "coordinate":
                    selected_coordinate_reuse_hits += 1
                elif route_id in route_ids_before_batch:
                    selected_route_reuse_hits += 1
            formal_coordinate_reuse_hits += selected_coordinate_reuse_hits
            formal_route_reuse_hits += selected_route_reuse_hits
            subgraph_budget["selected_coordinate_reuse_hits"] = selected_coordinate_reuse_hits
            subgraph_budget["selected_route_reuse_hits"] = selected_route_reuse_hits

            # 4. Build route_sandbox.json
            core_places_info = [
                {"place_id": origin.get("place_id"), "name": origin.get("name", ""), "role": "origin"},
                {"place_id": dest.get("place_id"), "name": dest.get("name", ""), "role": "destination"},
            ]
            for stop_index, stop in enumerate(waypoints, start=1):
                core_places_info.append({
                    "place_id": stop.get("place_id"), "name": stop.get("name", ""),
                    "role": "waypoint", "sequence": stop_index,
                    "semantic_action": stop.get("semantic_action", ""),
                    "dwell_min": stop.get("dwell_min", 0),
                })

            dist_places_info = [
                {"place_id": p.get("place_id"), "name": p.get("name", "")}
                for p in distractors
            ]

            route_sandbox = {
                "core_places": core_places_info,
                "distractor_places": dist_places_info,
                "correct_route_id": correct_route_id,
                "correct_leg_route_ids": correct_leg_route_ids,
                "correct_route_summary": {
                    "mode": summary_mode,
                    "leg_modes": resolved_leg_modes,
                    "time_limit_min": time_limit_min,
                    "urgency": core["urgency"],
                    "has_waypoint": core["has_waypoint"],
                    "semantic_stop_count": len(waypoints),
                    "route_snapshot_id": route_snapshot_id,
                },
                "route_subgraph_build": subgraph_budget,
                "all_routes_in_task": routes_in_task,
                # Constraint-based GT (evaluator primary input)
                "constraints": {
                    "departure_time_str": constraint_data.get("departure_time_str", ""),
                    "arrival_deadline_str": constraint_data.get("arrival_deadline_str", ""),
                    "time_budget_min": time_limit_min,
                    "stop_duration_min": stop_duration_min,
                    "stop_durations_min": constraint_data.get("stop_durations_min", []),
                    "semantic_stop_count": len(waypoints),
                    "forbidden_modes": constraint_data.get("forbidden_modes", []),
                    "max_walking_distance_m": constraint_data.get("max_walking_distance_m"),
                },
                "expected_feasible": expected_feasible,
                "acceptable_plan_ids": constraint_data.get("acceptable_plan_ids", []),
                "minimum_possible_duration_min": constraint_data.get("minimum_possible_duration_min", time_limit_min),
            }

            # 5. Generate user_prompt
            chars = task_sampler.sample_characters(
                {"min_chars": core["char_count"], "max_chars": core["char_count"]},
                count=core["char_count"],
            )
            char_names = [c.get("name", "the user") for c in chars]
            user_prompt = generate_user_prompt(
                origin_name=origin.get("name", ""),
                dest_name=dest.get("name", ""),
                has_waypoint=core["has_waypoint"],
                waypoint_name=", ".join(stop.get("name", "") for stop in waypoints),
                character_names=char_names,
                constraint_data=constraint_data,
                rng=srng,
            )
            if waypoints:
                user_prompt += "\nRequired stops: " + "; ".join(
                    _semantic_stop_text(stop) for stop in waypoints
                ) + ". These are required errands, not optional transfer points that can be skipped."

            # Save per-task LLM log (reconstruct input + user_prompt as output)
            task_log_dir = self.output_dir / "logs" / f"{task_id_short}_{int(t0 * 1000)}"
            _save_task_logs(task_log_dir, task_id_short, constraint_data,
                           origin.get("name",""), dest.get("name",""),
                           " -> ".join(stop.get("name", "") for stop in waypoints),
                           char_names[0] if char_names else "",
                           user_prompt)
            dur = round(__import__("time").time() - t0, 1)
            logger.info(f"[{task_id_short}] DONE | feasible={expected_feasible} | "
                       f"forbidden={constraint_data.get('forbidden_modes',[])} | "
                       f"time={constraint_data.get('time_budget_min',0)}min | "
                       f"walk={constraint_data.get('max_walking_distance_m')} | {dur}s")

            # 6. Build judge_sequence
            judge_stages = _build_route_judge_stages(
                self.all_judge_nodes, origin, dest, waypoint,
                sampled_leg_modes[0] if sampled_leg_modes else "TRANSIT", time_limit_min,
                chars, srng,
            )

            # 7. World slice
            world_slice = task_sampler.build_world_slice(chars, all_places[:10])
            ws_data = task_sampler.build_world_slice_summary(chars, all_places[:10])
            ws_summary = WorldSliceSummary(
                characters=ws_data.get("characters", []),
                places=ws_data.get("places", []),
                districts=ws_data.get("districts", []),
                corridors=[],
            )

            # Static Route Planning must remain free of active dynamic overlays.
            env_events = []

            # 8. Build task
            task = BenchmarkTask(
                task_id=task_id,
                task_type=self.task_type,
                difficulty="medium",
                city_id=self.city_id,
                variant="",
                generated_at=generated_at,
                time_context=time_context,
                user_prompt=user_prompt,
                summary={
                    "character_count": core["char_count"],
                    "place_count": len(all_places),
                    "route_count": len(routes_in_task),
                    "env_event_count": len(env_events),
                    "judge_stage_count": len(judge_stages),
                    "distance_km": round(core["distance_m"] / 1000, 1),
                    "time_limit_min": time_limit_min,
                    "mode": summary_mode,
                    "leg_modes": resolved_leg_modes,
                    "has_waypoint": core["has_waypoint"],
                    "semantic_stop_count": len(waypoints),
                    "expected_feasible": expected_feasible,
                    "forbidden_modes": constraint_data.get("forbidden_modes", []),
                    "max_walking_m": constraint_data.get("max_walking_distance_m"),
                },
                world_slice=world_slice,
                world_slice_summary=ws_summary,
                involved_env_events=[e.node_id for e in env_events],
                env_event_details=env_events,
                judge_stages=judge_stages,
                metadata={"seed": self._active_seed_lineage["attempt_seed"], "task_type": self.task_type},
            )

            # 9. Write task folder
            folder_name = make_folder_name(task_idx, user_prompt)
            write_task_folder(task_type_dir / folder_name, task, env_events, judge_stages, ws_summary)

            # Also write route_sandbox.json
            write_json(task_type_dir / folder_name / "route_sandbox.json", route_sandbox)
            self.finalize_task_folder(task_type_dir / folder_name)

            tasks.append(task)
            next_idx += 1
            slot_attempt = 0
            # Persist after every accepted task: dense route subgraphs are
            # expensive real API artifacts and must survive a later failure.
            route_cache.save()

            if count > 10 and len(tasks) % max(1, count // 5) == 0:
                print(f"  [{self.task_type}] {len(tasks)}/{count}", flush=True)

        total_dur = round(__import__("time").time() - t_batch_start, 1)
        batch_cache_stats_after = route_cache.stats_snapshot()
        total_api = int(batch_cache_stats_after["api_requests"] or 0) - int(
            batch_cache_stats_before["api_requests"] or 0
        )
        coordinate_reuse_materialized = int(
            batch_cache_stats_after["coordinate_reuse_materialized"] or 0
        ) - int(batch_cache_stats_before["coordinate_reuse_materialized"] or 0)
        logger.info("━" * 60)
        logger.info(
            f"BATCH SUMMARY tasks={len(tasks)}/{count} attempts={attempt} "
            f"duration={total_dur}s api_calls={total_api} "
            f"coordinate_reuse_materialized={coordinate_reuse_materialized} "
            f"selected_route_reuse_hits={formal_route_reuse_hits} "
            f"selected_coordinate_reuse_hits={formal_coordinate_reuse_hits}"
        )
        logger.info("━" * 60)

        route_cache.save()

        from task_module._shared.generation_telemetry import write_generation_telemetry
        write_generation_telemetry(
            self.output_dir, task_type=self.task_type, city_id=self.city_id,
            requested_count=count, generated_count=len(tasks), elapsed_s=total_dur,
            route_cache=route_cache, skipped_count=attempt - len(tasks),
            failure_reasons={
                "sampling_or_route_rejections": attempt - len(tasks),
                "requested_but_unfilled": max(0, count - len(tasks)),
            },
        )

        from task_module._shared.task_contract import canonicalize_batch
        city = (self.world.get("cities") or [{}])[0]
        reports = canonicalize_batch(
            self.output_dir,
            task_type=self.task_type,
            city_id=self.city_id,
            city_timezone=city.get("timezone", "UTC"),
        )
        invalid = [report for report in reports if not report["valid"]]
        if invalid:
            raise ValueError(f"Route task contract validation failed: {invalid[:3]}")

        print(f"[RoutePlanning] Generated {len(tasks)} tasks")
        print(
            f"[RoutePlanning] Route cache: {len(route_cache)} entries "
            f"({total_api} new API calls, {coordinate_reuse_materialized} coordinate aliases)"
        )
        return tasks


def _build_route_judge_stages(
    all_judge_nodes: list[JudgeNode],
    origin: dict, dest: dict, waypoint: dict | None,
    mode: str, time_limit_min: int,
    characters: list[dict], rng: random.Random,
) -> list[JudgeStage]:
    """Build judge stages specific to route planning."""
    nodes_by_cat: dict[str, list[JudgeNode]] = {}
    for n in all_judge_nodes:
        nodes_by_cat.setdefault(n.category, []).append(n)

    stages: list[JudgeStage] = []

    # Stage 1: Search/identify places
    search_cands = [n for n in nodes_by_cat.get("service_operation", [])
                    if "search" in n.agent_action.function]
    if not search_cands:
        search_cands = nodes_by_cat.get("service_operation", [])[:1]
    stages.append(JudgeStage(
        stage_id="search", description="搜索确认起点、终点和途经点信息",
        depends_on=[], nodes=search_cands[:1],
    ))

    # Stage 2: Plan route
    nav_cands = [n for n in nodes_by_cat.get("navigation", [])
                 if "plan_route" in n.agent_action.function]
    if not nav_cands:
        nav_cands = nodes_by_cat.get("navigation", [])[:1]
    for node in nav_cands:
        slots = node.agent_action.arg_slots
        if origin.get("place_id"):
            slots["origin_place_id"] = origin["place_id"]
        if dest.get("place_id"):
            slots["dest_place_id"] = dest["place_id"]
        slots.pop("<FROM_PLACE>", None)
        slots.pop("<TO_PLACE>", None)
        if "mode" in slots:
            slots["mode"] = mode
    stages.append(JudgeStage(
        stage_id="plan_route", description=f"规划{origin.get('name','?')}到{dest.get('name','?')}的路线",
        depends_on=["search"], nodes=nav_cands[:1],
    ))

    # Stage 3: Notify user
    comm_cands = nodes_by_cat.get("communication", [])
    if comm_cands:
        stages.append(JudgeStage(
            stage_id="notify", description="通知用户最终路线方案",
            depends_on=["plan_route"],
            nodes=rng.sample(comm_cands, min(2, len(comm_cands))),
        ))

    return stages


def _extract_prefix(prompt: str, n: int = 5) -> str:
    chinese = re.findall(r'[\u4e00-\u9fff]', prompt)
    prefix = ''.join(chinese[:n]) if chinese else prompt[:n]
    return re.sub(r'[<>:"/\\|?*]', '', prefix) or "task"


def _semantic_stop_text(stop: dict[str, Any]) -> str:
    action_labels = {
        "document_pickup": "pick up a document",
        "parcel_collection": "collect a parcel",
        "brief_meeting": "brief meeting",
        "errand": "run an errand",
    }
    action = action_labels.get(stop.get("semantic_action", ""), "run an errand")
    return f"{stop.get('name', '')} ({action}, stay for {stop.get('dwell_min', 0)} minutes)"


# ── Standalone CLI ──

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Build route planning benchmark tasks.")
    parser.add_argument("--city", required=True, help="City ID (e.g. tokyo_complex)")
    parser.add_argument("--count", type=int, default=5, help="Number of tasks (default: 5)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--world", default="", help="Path to world.json")
    parser.add_argument("--characters", default="", help="Path to characters.json")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", default="./outputs/benchmarks")
    parser.add_argument("--batch", default="", help="Specify batch timestamp (default: latest)")
    parser.add_argument("--new-batch", action="store_true", help="Create a new timestamp batch")

    args = parser.parse_args()

    # Resolve paths
    world_path = args.world or _find_world_path(args.city)
    chars_path = args.characters or _find_chars_path(args.city)

    if not Path(world_path).exists():
        print(f"Error: world.json not found at {world_path}")
        return 1

    world = read_json(world_path)
    characters = {}
    if Path(chars_path).exists():
        characters = read_json(chars_path)

    city = (world.get("cities") or [{}])[0]
    city_id = city.get("city_id", args.city)

    # Batch resolution
    from datetime import datetime as dt, timezone as tz
    if args.new_batch or args.batch:
        batch_ts = args.batch or dt.now(tz.utc).strftime("%Y%m%d_%H%M%S")
    else:
        city_root = Path(args.output) / "route_planning" / city_id
        latest_link = city_root / "latest"
        batch_ts = latest_link.resolve().name if latest_link.is_symlink() else dt.now(tz.utc).strftime("%Y%m%d_%H%M%S")

    out_dir = Path(args.output) / "route_planning" / city_id / batch_ts
    out_dir.mkdir(parents=True, exist_ok=True)

    # Update latest symlink
    latest_link = Path(args.output) / "route_planning" / city_id / "latest"
    latest_link.unlink(missing_ok=True)
    latest_link.symlink_to(batch_ts, target_is_directory=True)

    # Load event nodes (generate if not exist)
    event_nodes_path = out_dir / "event_nodes.json"
    if not event_nodes_path.exists():
        from task_module.event_factory import run_event_factory
        run_event_factory(
            world_path=world_path, characters_path=chars_path,
            output_dir=str(out_dir), config_yaml=args.config,
        )

    event_data = read_json(event_nodes_path)
    env_events = _dicts_to_env_events(event_data.get("env_events", []))
    judge_nodes = _dicts_to_judge_nodes(event_data.get("judge_nodes", []))

    gen = RoutePlanningGenerator(
        world=world, characters=characters,
        env_events=env_events, judge_nodes=judge_nodes,
        output_dir=str(out_dir), seed=args.seed,
        config_yaml=args.config,
    )
    gen.generate(count=args.count)

    route_cache_dir = Path("./outputs/routes") / city_id
    print(f"\n[RoutePlanning] Done.")
    print(f"[RoutePlanning] Tasks: {out_dir}/tasks/")
    print(f"[RoutePlanning] Cache: {route_cache_dir}/route_cache.json")
    return 0


def _derive_variant(city_id: str) -> str:
    parts = city_id.split("_")
    if parts[-1] in ("base", "large", "complex", "demo"):
        return parts[-1]
    return "complex"


def _dicts_to_env_events(raw: list[dict]) -> list[EnvEventNode]:
    from task_module.task_schema import StateEffect
    result = []
    for d in raw:
        effects = [
            StateEffect(
                target_type=e.get("target_type", ""),
                target_id_pattern=e.get("target_id_pattern", ""),
                field_path=e.get("field_path", ""),
                op=e.get("op", "set"), value=e.get("value"),
                duration_seconds=e.get("duration_seconds"),
            ) for e in d.get("effects", [])
        ]
        result.append(EnvEventNode(
            node_id=d.get("node_id", ""), category=d.get("category", ""),
            scope_type=d.get("scope_type", ""), scope_id=d.get("scope_id"),
            effects=effects, trigger_mode=d.get("trigger_mode", "random"),
            trigger_probability=d.get("trigger_probability", 1.0),
            trigger_delay_seconds=d.get("trigger_delay_seconds"),
            visibility=d.get("visibility", "visible"),
            description_zh=d.get("description_zh", ""),
            severity=d.get("severity", ""), source=d.get("source", ""),
        ))
    return result


def _dicts_to_judge_nodes(raw: list[dict]) -> list[JudgeNode]:
    from task_module.task_schema import AgentAction, CheckItem
    result = []
    for d in raw:
        action = d.get("agent_action", {})
        checks = [
            CheckItem(
                check_type=c.get("check_type", ""), field=c.get("field", ""),
                expected=c.get("expected"), tolerance=c.get("tolerance"),
                weight=c.get("weight", "hard"),
            ) for c in d.get("checks", [])
        ]
        result.append(JudgeNode(
            node_id=d.get("node_id", ""), category=d.get("category", ""),
            agent_action=AgentAction(
                app=action.get("app", ""), function=action.get("function", ""),
                arg_slots=action.get("arg_slots", {}),
            ),
            checks=checks, depends_on=d.get("depends_on", []),
            description_zh=d.get("description_zh", ""),
            source=d.get("source", ""),
        ))
    return result


def _setup_log(path: Path) -> None:
    """Configure logging to both console and file."""
    import logging, sys
    path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(path), encoding="utf-8"),
        ],
        force=True,
    )


def _save_task_logs(task_dir: Path, task_id: str, constraint_data: dict,
                    origin: str, dest: str, waypoint: str,
                    char_name: str, user_prompt: str) -> None:
    """Save per-task LLM reconstructed input + output."""
    gen_log_dir = task_dir
    gen_log_dir.mkdir(parents=True, exist_ok=True)
    cd = constraint_data.get("constraints", constraint_data)
    llm_in = f"origin: {origin}\ndest: {dest}\n"
    if waypoint:
        llm_in += f"waypoint: {waypoint}\n"
    llm_in += f"char: {char_name}\n"
    llm_in += f"time_budget_min: {cd.get('time_budget_min','?')}\n"
    llm_in += f"forbidden_modes: {cd.get('forbidden_modes',[])}\n"
    llm_in += f"max_walking_distance_m: {cd.get('max_walking_distance_m')}\n"
    llm_in += f"expected_feasible: {constraint_data.get('expected_feasible')}\n"
    (gen_log_dir / f"{task_id}_llm_in.txt").write_text(llm_in, encoding="utf-8")
    (gen_log_dir / f"{task_id}_llm_out.txt").write_text(user_prompt, encoding="utf-8")

    import json
    (gen_log_dir / f"{task_id}_details.json").write_text(
        json.dumps({
            "origin": origin, "dest": dest, "waypoint": waypoint if waypoint else None,
            "constraints": cd,
            "expected_feasible": constraint_data.get("expected_feasible"),
            "prompt_length": len(user_prompt),
        }, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_world_path(city: str) -> str:
    """Find world.json for a city, trying multiple variant paths."""
    for variant in ["", "_smoke", "_complex", "_large", "_base"]:
        city_slug = city if variant else city
        if variant:
            city_slug = city + variant if not city.endswith(variant) else city
        candidates = [
            f"outputs/mapseeds/{city_slug}/complex/world.json",
            f"outputs/mapseeds/{city_slug}/base/world.json",
            f"outputs/mapseeds/{city}/complex/world.json",
            f"outputs/mapseeds/{city}/base/world.json",
        ]
        for path in candidates:
            if Path(path).exists():
                return path
    return f"outputs/mapseeds/{city}/complex/world.json"


def _find_chars_path(city: str) -> str:
    """Find characters.json for a city."""
    for city_slug in [city, f"{city}_smoke", f"{city}_complex"]:
        candidates = [
            f"outputs/characters/{city_slug}/complex/characters.json",
            f"outputs/characters/{city_slug}/base/characters.json",
            f"outputs/characters/{city_slug}/demo/characters_validation.json",
            f"outputs/characters/{city}/demo/characters_validation.json",
        ]
        for path in candidates:
            if Path(path).exists():
                return path
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
