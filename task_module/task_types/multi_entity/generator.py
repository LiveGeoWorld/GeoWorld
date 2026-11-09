"""Multi-Entity Coordination Generator — Type 4: 多主体地理协同.

Pipeline:
  1. Sample 2-4 connected characters and factual activity POIs
  2. Select a route-complete answer POI and materialize a feasible scenario
  3. Deterministically verify the private plan before any LLM call
  4. LLM constructs C + prose; append numeric feedback until uniquely verified
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.config_loader import PipelineConfig
from shared.io import read_json, write_json
from shared.maps_client import GoogleMapsClient
from shared.opening_hours import intervals_on_date

from task_module.task_schema import BenchmarkTask, JudgeStage, WorldSliceSummary
from task_module._shared.base_generator import BaseTaskGenerator, make_folder_name
from task_module._shared.sampler import TaskSampler
from task_module._shared.task_contract import assigned_time_context
from task_module._shared.output_writer import write_task_folder
from task_module.task_types.route_planning.route_cache import RouteCache

from task_module.task_types.multi_entity.sampler import MultiEntitySampler
from task_module.task_types.multi_entity.choice_verification import verify_candidate_choice
from task_module.task_types.multi_entity.constraint_revision import configured_client, revise_constraints
from task_module.task_types.multi_entity.natural_prompt import (
    build_natural_user_input,
    build_public_fact_projection,
    validate_natural_user_prompt,
)
from task_module.task_types.multi_entity.solver import solve_coordination


def _resolve_multi_entity_routing_places(
    world: dict[str, Any], route_pool_payload: dict, route_entries: list[dict],
) -> list[dict[str, Any]]:
    """Resolve Multi-Entity's Routing coverage from a manifest or legacy cache."""
    places = [place for place in world.get("places") or [] if isinstance(place, dict)]
    places_by_id = {
        str(place.get("place_id") or ""): place for place in places if place.get("place_id")
    }
    manifest_ids = {
        str(place_id) for place_id in route_pool_payload.get("place_ids") or []
        if str(place_id) in places_by_id
    }
    if manifest_ids:
        return [place for place in places if str(place.get("place_id") or "") in manifest_ids]

    coordinate_to_id = {
        (round(float(location["lat"]), 6), round(float(location["lng"]), 6)):
            str(place.get("place_id") or "")
        for place in places
        if isinstance((location := place.get("location")), dict)
        and location.get("lat") is not None and location.get("lng") is not None
        and place.get("place_id")
    }
    legacy_ids: set[str] = set()
    for entry in route_entries:
        if not isinstance(entry, dict):
            continue
        for prefix in ("origin", "dest"):
            place_id = str(entry.get(f"{prefix}_place_id") or "")
            if place_id in places_by_id:
                legacy_ids.add(place_id)
                continue
            try:
                mapped = coordinate_to_id.get((
                    round(float(entry.get(f"{prefix}_lat")), 6),
                    round(float(entry.get(f"{prefix}_lng")), 6),
                ))
            except (TypeError, ValueError):
                mapped = None
            if mapped:
                legacy_ids.add(mapped)
    return [place for place in places if str(place.get("place_id") or "") in legacy_ids]


class MultiEntityGenerator(BaseTaskGenerator):
    """Generates multi-entity coordination benchmark tasks."""

    task_type = "multi_entity"
    label_zh = "多主体地理协同"
    default_count = 100
    min_chars = 2
    max_chars = 4
    preferred_service_types = []
    env_event_categories = ["actor_condition", "transit", "service", "weather"]

    def __init__(
        self, world: dict, characters: dict,
        env_events: list, judge_nodes: list,
        output_dir: str, seed: int = 42,
        use_llm_prompt: bool = True,
        config_yaml: str = "config.yaml",
        stage_model_key: str = "scenario_patch",
    ):
        super().__init__(
            world=world, characters=characters, env_events=env_events,
            judge_nodes=judge_nodes, output_dir=output_dir, seed=seed,
            use_llm_prompt=use_llm_prompt, config_yaml=config_yaml,
        )
        self.stage_model_key = stage_model_key

    def generate(self, count: int | None = None) -> list[BenchmarkTask]:
        count = count or 10
        rng = self.next_generation_rng()

        task_type_dir = self.output_dir / "tasks"
        task_type_dir.mkdir(parents=True, exist_ok=True)

        log_path = self.output_dir / "generate.log"
        _setup_log(log_path)
        logger = logging.getLogger("multi_entity")

        route_cache = RouteCache(self.city_id)
        route_pool_path = route_cache.cache_dir / "route_poi_pool.json"
        route_pool_payload = read_json(route_pool_path) if route_pool_path.is_file() else {}
        routing_places = _resolve_multi_entity_routing_places(
            self.world, route_pool_payload, list(route_cache._cache.values()),
        )
        if not routing_places:
            raise ValueError(
                "multi_entity requires non-empty Routing POI coverage for the active city version"
            )
        maps_client = GoogleMapsClient(PipelineConfig(self.config_yaml)) if self.config_yaml else None

        # Batch header
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

        # Scan existing folders
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

        logger.info(f"[{self.task_type}] Generating {count} tasks ({self.label_zh})...")

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

        logger.info(f"[{self.task_type}] Generating {count} tasks ({self.label_zh})...")

        task_sampler = TaskSampler(self.world, self.characters, seed=self.seed)
        entity_sampler = MultiEntitySampler(self.world, self.characters, route_cache, rng=rng)
        frozen_places = {p['place_id']: p for p in self.world.get('places', []) if p.get('place_id')}
        city_timezone = (self.world.get('cities') or [{}])[0].get('timezone', 'UTC')

        tasks: list[BenchmarkTask] = []
        batch_tokens = {"prompt": 0, "completion": 0, "total": 0}
        batch_llm_dur = 0.0
        batch_api_calls = 0
        batch_success = 0
        batch_skip = 0
        llm_start_time = __import__("time").time()

        attempts = 0
        slot_attempt = 0
        max_attempts = max(count * 12, count + 4)
        while len(tasks) < count and attempts < max_attempts:
            attempts += 1
            slot_attempt += 1
            task_idx = next_idx
            slot_rng = self.activate_task_attempt(task_idx, slot_attempt)
            task_sampler = TaskSampler(self.world, self.characters, seed=self.task_seed(task_idx))
            entity_sampler = MultiEntitySampler(self.world, self.characters, route_cache, rng=slot_rng)
            self.set_cost_unit(f"slot_{task_idx:04d}")
            task_id_short = f"task_{task_idx:04d}"

            n_chars = slot_rng.randint(self.min_chars, self.max_chars)
            participants = entity_sampler.sample_participants(count=n_chars)
            if len(participants) < 2:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP only {len(participants)} participants")
                continue

            selection = entity_sampler.sample_activity_places(routing_places)
            if selection is None:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP no eligible activity POI selection")
                continue
            target = selection["target"]
            venues = [target, *selection["neighbors"]]
            task_id = f"task_{self.city_id}_{self.task_type}_{task_idx:04d}"
            time_context = assigned_time_context(task_id, self.city_id, city_timezone)
            instruction_time = datetime.fromisoformat(time_context['instruction_time'])
            activity_date = instruction_time.date()
            for venue in venues:
                venue['activity_opening_intervals'] = intervals_on_date(
                    frozen_places[venue['place_id']], activity_date)
            if target['activity_opening_intervals'] is None:
                batch_skip += 1
                self._record_rejected_candidate(task_type_dir, task_idx, 'gt_missing_opening_hours', {
                    'target_place_id': target['place_id'], 'activity_date': activity_date.isoformat(),
                })
                continue

            route_data, route_queries = entity_sampler.precheck_character_routes(
                participants, venues, maps_client)
            batch_api_calls += sum(1 for q in route_queries if q.get("source") == "api_or_cache")

            scenario = entity_sampler.materialize_feasible_activity(
                participants, target, route_data,
                earliest_departure=max(16 * 60, instruction_time.hour * 60 + instruction_time.minute))
            if scenario is None:
                batch_skip += 1
                self._record_rejected_candidate(task_type_dir, task_idx, "cannot_materialize_feasible_scenario", {
                    "target_place_id": target.get("place_id", ""),
                })
                logger.info(f"[{task_id_short}] SKIP cannot materialize target scenario")
                continue

            public_fact_projection = build_public_fact_projection(participants, venues, scenario)
            scenario["public_fact_projection"] = public_fact_projection
            # The deterministic authoring oracle is the feasible-only gate.
            # Do this before consuming an LLM call to write a natural prompt.
            gt = solve_coordination(participants, venues, route_data, scenario, target_place_id=target["place_id"])
            if not gt.get("expected_feasible") or (
                gt.get("expected_place_id") != target.get("place_id")
            ):
                batch_skip += 1
                self._record_rejected_candidate(task_type_dir, task_idx, "private_feasibility_gate_failed", {
                    "target_place_id": target.get("place_id", ""),
                    "expected_feasible": bool(gt.get("expected_feasible")),
                    "selected_place_id": gt.get("expected_place_id", ""),
                    "acceptable_plan_count": len(gt.get("acceptable_plans", [])),
                })
                logger.info(f"[{task_id_short}] SKIP private feasibility gate")
                continue

            choice_verification = verify_candidate_choice(
                public_fact_projection, venues, route_data, target["place_id"])
            if not choice_verification["valid"] and not self.use_llm_prompt:
                batch_skip += 1
                self._record_rejected_candidate(
                    task_type_dir, task_idx, "candidate_choice_not_verified", choice_verification)
                logger.info(f"[{task_id_short}] SKIP candidate-local uniqueness gate")
                continue
            llm_input = build_natural_user_input(participants, venues, scenario, route_data)
            if self.use_llm_prompt:
                import uuid
                client = configured_client(self.config_yaml, self.stage_model_key)
                call_log = task_type_dir.parent / 'generation_log' / 'constraint_revision_calls' / (uuid.uuid4().hex + '.json')
                llm_result, revised_proof, revision_log = revise_constraints(
                    client, public_fact_projection, venues, route_data, target['place_id'],
                    activity_date=activity_date.isoformat(),
                    save_audit=lambda record: write_json(call_log, record))
                telemetry = client.telemetry
                token_usage = {**telemetry['token_usage'], 'duration_s': telemetry['elapsed_seconds']}
                if llm_result:
                    public_fact_projection = llm_result['public_fact_projection']
                    choice_verification = revised_proof
                    for person, public in zip(participants, public_fact_projection['participants']):
                        person.update({key: public[key] for key in ('available_from', 'must_leave_by')})
                    scenario['meeting_duration_min'] = public_fact_projection['activity']['duration_min']
                    scenario['public_activity_C'] = public_fact_projection['activity']
                    scenario['public_fact_projection'] = public_fact_projection
                    gt = solve_coordination(participants, venues, route_data, scenario, target_place_id=target['place_id'])
                    if not gt.get('expected_feasible') or gt.get('expected_place_id') != target['place_id']:
                        raise ValueError('Revised C disagrees with the production GT solver')
                    llm_input = json.dumps(revision_log['context'], ensure_ascii=False, indent=2)
            else:
                llm_result = {"user_prompt": f"{activity_date.isoformat()}：{_template_user_prompt(participants)}"}
                token_usage = {"prompt": 0, "completion": 0, "total": 0, "duration_s": 0}
            if not llm_result:
                batch_skip += 1
                if self.use_llm_prompt:
                    self._record_rejected_candidate(task_type_dir, task_idx, 'constraint_revision_exhausted', {
                        'target_place_id': target['place_id'], 'call_log': str(call_log),
                        'attempts': len(revision_log['attempts']),
                        'last_feedback': revision_log['attempts'][-1].get('feedback'),
                    })
                logger.info(f"[{task_id_short}] SKIP LLM fail | {token_usage.get('total',0)} tok")
                continue

            user_prompt = llm_result.get("user_prompt", "")
            if not user_prompt:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP empty prompt")
                continue
            prompt_errors = validate_natural_user_prompt(user_prompt, participants)
            if prompt_errors:
                batch_skip += 1
                self._record_rejected_candidate(task_type_dir, task_idx, "public_prompt_faithfulness_gate_failed", {
                    "errors": prompt_errors,
                })
                logger.info(f"[{task_id_short}] SKIP public prompt faithfulness gate")
                continue

            batch_success += 1
            bt = token_usage
            batch_tokens["prompt"] += bt.get("prompt", 0)
            batch_tokens["completion"] += bt.get("completion", 0)
            batch_tokens["total"] += bt.get("total", 0)
            batch_llm_dur += bt.get("duration_s", 0)

            # Build task folder
            folder_name = make_folder_name(task_idx, user_prompt)
            task_folder = task_type_dir / folder_name
            task_folder.mkdir(parents=True, exist_ok=False)
            gen_dir = task_folder / "generation_log"
            gen_dir.mkdir(parents=True, exist_ok=True)
            if self.use_llm_prompt:
                write_json(gen_dir / 'constraint_revision_calls.json', revision_log)

            # Save generation logs
            (gen_dir / "llm_in.txt").write_text(llm_input, encoding="utf-8")
            (gen_dir / "llm_out.json").write_text(
                json.dumps(llm_result, ensure_ascii=False, indent=2), encoding="utf-8")
            (gen_dir / "public_fact_projection.json").write_text(
                json.dumps(public_fact_projection, ensure_ascii=False, indent=2), encoding="utf-8")
            (gen_dir / "candidate_choice_verification.json").write_text(
                json.dumps(choice_verification, ensure_ascii=False, indent=2), encoding="utf-8")

            # details.json
            (gen_dir / "details.json").write_text(json.dumps({
                "participants": [{k: v for k, v in p.items() if k not in ("preferences", "constraints")}
                                for p in participants],
                "places": venues,
                "poi_selection": {"branch": selection["branch"], "anchor": selection.get("anchor")},
                "scenario": scenario,
                "gt_summary": {
                    "expected_feasible": gt.get("expected_feasible"),
                    "expected_place": gt.get("expected_place_id", ""),
                    "constraints": gt.get("constraints", {}),
                    "acceptable_plans_count": len(gt.get("acceptable_plans", [])),
                },
                "generation": {
                    "llm_model": model_name,
                    "llm_duration_s": bt.get("duration_s", 0),
                    "tokens_prompt": bt.get("prompt", 0),
                    "tokens_completion": bt.get("completion", 0),
                    "tokens_total": bt.get("total", 0),
                    "route_api_calls": sum(1 for q in route_queries if q.get("source") == "api_or_cache"),
                    "route_cache_hits": sum(1 for q in route_queries if q.get("source") == "cache"),
                },
            }, ensure_ascii=False, indent=2), encoding="utf-8")

            # constraints.json
            (gen_dir / "constraints.json").write_text(json.dumps({
                "expected_feasible": gt.get("expected_feasible"),
                "expected_place_id": gt.get("expected_place_id", ""),
                "constraints": gt.get("constraints", {}),
                "acceptable_plan_count": len(gt.get("acceptable_plans", [])),
            }, ensure_ascii=False, indent=2), encoding="utf-8")

            # route_queries.json
            (gen_dir / "route_queries.json").write_text(
                json.dumps(route_queries, ensure_ascii=False, indent=2), encoding="utf-8")

            # Write task files
            all_place_ids = [p.get("home_place_id", "") for p in participants] + \
                           [venue.get("place_id", "") for venue in venues]
            all_place_ids = [pid for pid in all_place_ids if pid]
            all_places = [p for p in self.world.get("places", []) if p.get("place_id") in all_place_ids]
            chars_for_slice = [{"character_id": p["character_id"], "name": p["name"]} for p in participants]
            world_slice = task_sampler.build_world_slice(chars_for_slice, all_places[:10])

            task_sandbox = {
                "task_type": "multi_entity",
                "scenario": scenario,
                "participants": participants,
                "candidate_places": venues,
                "candidate_choice_verification": choice_verification,
                "route_data": route_data,
                "constraints": gt.get("constraints", {}),
                "expected_feasible": gt.get("expected_feasible", True),
                "expected_place_id": gt.get("expected_place_id", ""),
                "acceptable_plans": gt.get("acceptable_plans", []),
                "required_output_fields": ["selection", "assignments", "total_commute_min"],
            }

            task = BenchmarkTask(
                task_id=task_id,
                task_type=self.task_type, difficulty="medium",
                city_id=self.city_id, variant="",
                generated_at=self.generated_at, user_prompt=user_prompt,
                summary={"character_count": len(participants),
                         "place_count": len(venues),
                         "scenario_family": scenario.get("family", ""),
                         "expected_feasible": gt.get("expected_feasible", True)},
                world_slice=world_slice,
                world_slice_summary=WorldSliceSummary(
                    characters=chars_for_slice,
                    places=[{"place_id": venue["place_id"], "name": venue["name"]} for venue in venues],
                    districts=[], corridors=[],
                ),
                involved_env_events=[], env_event_details=[], judge_stages=[],
                metadata={"seed": self._active_seed_lineage["attempt_seed"], "task_type": self.task_type},
            )

            write_json(task_folder / "task.json", {
                "task_id": task.task_id, "task_type": self.task_type,
                "difficulty": "medium", "city_id": self.city_id,
                "generated_at": self.generated_at,
                "user_prompt": user_prompt, "summary": task.summary, "time_context": time_context,
            })
            write_json(task_folder / "task_sandbox.json", task_sandbox)
            write_json(task_folder / "world_slice.json", task.world_slice_summary.to_dict())
            write_json(task_folder / "env_events.json", {"env_events": []})
            write_json(task_folder / "judge_sequence.json", {"stages": []})
            self.finalize_task_folder(task_folder)

            tasks.append(task)
            next_idx += 1
            slot_attempt = 0

            logger.info(f"[{task_id_short}] OK {len(participants)}p {len(venues)}poi {selection['branch']} | "
                       f"feasible={gt.get('expected_feasible',True)} | "
                       f"{bt.get('prompt',0)}->{bt.get('completion',0)} tok | "
                       f"{bt.get('duration_s',0)}s | "
                       f"{sum(1 for q in route_queries if q.get('source')=='api_or_cache')} API")

        # Batch summary
        total_dur = round(__import__("time").time() - llm_start_time, 1)
        logger.info("━" * 60)
        logger.info(f"BATCH SUMMARY  success={batch_success}  skip={batch_skip}")
        logger.info(f"  Tokens: {batch_tokens['prompt']}+{batch_tokens['completion']}={batch_tokens['total']} | "
                   f"LLM: {batch_llm_dur}s | Total: {total_dur}s")
        logger.info(f"  API: {batch_api_calls} route calls")
        logger.info(f"  Candidate attempts: {attempts}/{max_attempts}")
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
            raise ValueError("multi_entity task contract validation failed")

        # Create/update latest symlink
        return tasks

    @staticmethod
    def _record_rejected_candidate(task_type_dir: Path, task_idx: int, reason: str, details: dict[str, Any]) -> None:
        """Preserve private construction diagnostics outside formal ``tasks/``."""
        rejected_dir = task_type_dir.parent / "candidates" / "rejected"
        rejected_dir.mkdir(parents=True, exist_ok=True)
        write_json(rejected_dir / f"slot_{task_idx:04d}.json", {
            "task_type": "multi_entity",
            "candidate_slot": task_idx,
            "reason": reason,
            "details": details,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        })


def _setup_log(path: Path) -> None:
    """Configure logging to both console and file."""
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


def _template_user_prompt(participants: list[dict]) -> str:
    windows = []
    for person in participants:
        name = person.get("name", "参与者")
        available = person.get("available_from", "傍晚")
        leave_by = person.get("must_leave_by", "晚些时候")
        windows.append(f"{name}从{available}起有空，最晚{leave_by}需要离开")
    return (
        "请帮我们协调一次线下会面。" + "；".join(windows) +
        "。请结合各自出发位置、可用交通方式、时间窗口和预算，选择合适地点并安排可执行的出行与会合方案。"
    )
