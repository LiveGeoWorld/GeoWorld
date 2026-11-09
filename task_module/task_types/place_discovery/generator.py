"""Place Discovery Generator — Type 2: 地点发现.

Evidence-backed generation: world + characters + EventFactory env pool
→ batch evidence pipeline → task folders (gold, env_events, task_spec).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.io import write_json

from task_module._shared.base_generator import BaseTaskGenerator
from task_module._shared.batch_state import existing_gold_ids, next_task_index
from task_module._shared.sampler import TaskSampler
from task_module.curated.exporter import export_specs_bundle, generate_tasks_from_specs, write_evidence_task_from_example
from task_module.task_schema import BenchmarkTask
from task_module.task_types.place_discovery.batch_generation.config import DiscoveryBatchGenerationConfig
from task_module.task_types.place_discovery.batch_generation.pipeline import generate_discovery_examples


class PlaceDiscoveryGenerator(BaseTaskGenerator):
    task_type = "place_discovery"
    label_zh = "地点发现"
    default_count = 150
    min_chars = 1
    max_chars = 3
    preferred_service_types = ["restaurant_reservation", "cafe_reservation", "attraction_visit"]
    env_event_categories = ["service", "actor_preference_change"]
    prompt_llm_endpoint = "cliproxy_gpt55_pro"

    def __init__(
        self,
        world: dict[str, Any],
        characters: dict[str, Any],
        env_events: list,
        judge_nodes: list,
        output_dir: str,
        seed: int = 42,
        use_llm_prompt: bool = True,
        config_yaml: str = "config.yaml",
        curated_examples_dir: str = "",
        require_quality_pass: bool = True,
        env_event_count: int = 3,
        max_attempts_per_slot: int = 3,
        export_task_spec: bool = True,
        export_specs_bundle: bool = False,
        from_specs_path: str = "",
        env_seed: int | None = None,
        variant: str = "complex",
        stage_model_key: str = "task_design",
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
            curated_examples_dir=curated_examples_dir,
        )
        self.require_quality_pass = require_quality_pass
        self.env_event_count = env_event_count
        self.max_attempts_per_slot = max_attempts_per_slot
        self.export_task_spec = export_task_spec
        self.export_specs_bundle = export_specs_bundle
        self.from_specs_path = from_specs_path
        self.env_seed = env_seed
        self.variant = variant
        self.stage_model_key = stage_model_key
        self._task_sampler = TaskSampler(world, characters, seed=seed)

    def generate(self, count: int | None = None) -> list[BenchmarkTask]:
        count = count or self.default_count
        if self.from_specs_path:
            return generate_tasks_from_specs(
                self,
                self.from_specs_path,
                count,
                env_seed=self.env_seed,
                export_task_spec=self.export_task_spec,
            )

        start_index = next_task_index(self.output_dir)
        used_service_ids = existing_gold_ids(self.output_dir, "service_id")
        if start_index > 1:
            print(
                f"[{self.task_type}] Append mode: {start_index - 1} existing tasks; "
                f"reserving {len(used_service_ids)} existing gold services.",
                flush=True,
            )

        batch_config = DiscoveryBatchGenerationConfig(
            count=count,
            seed=self.seed,
            output_root=str(self.output_dir),
            run_id="_construction",
            city_weights={self.city_id: 1.0},
            config_yaml=self.config_yaml,
            variant=self.variant,
            use_llm_prompt=self.use_llm_prompt,
            use_characters=True,
            prompt_model_stage=self.stage_model_key,
        )

        worlds = {self.city_id: self.world}
        characters_by_city = {self.city_id: self.characters}

        print(
            f"[{self.task_type}] Evidence pipeline: {count} tasks "
            f"(quality_pass={self.require_quality_pass}, env={self.env_event_count})",
            flush=True,
        )

        examples, failures, quality_reports = generate_discovery_examples(
            config=batch_config,
            worlds=worlds,
            characters_by_city=characters_by_city,
            count=count,
            require_quality_pass=self.require_quality_pass,
            max_attempts_per_slot=self.max_attempts_per_slot,
            start_index=start_index,
            excluded_service_ids=used_service_ids,
            cost_slot_callback=lambda slot: self.set_cost_unit(f"slot_{slot:04d}"),
            attempt_rng_callback=self.activate_task_attempt,
        )

        tasks: list[BenchmarkTask] = []
        generated_at = datetime.now(timezone.utc).isoformat()
        for ordinal, example in enumerate(examples, start=1):
            lineage = example.get("_seed_lineage") or {}
            self.activate_task_attempt(
                int(lineage.get("task_index") or (start_index + ordinal - 1)),
                int(lineage.get("attempt_index") or 1),
            )
            self.set_cost_unit(str(example.get("_cost_generation_unit_id") or f"slot_{ordinal:04d}"))
            index = start_index + ordinal - 1
            places = _places_for_example(self.world, example)
            characters = _characters_for_example(self.characters, example)
            # Static discovery evaluates a frozen world snapshot.  Dynamic
            # events are generated only by the Persistent Replanning overlay.
            env_events = []
            task = write_evidence_task_from_example(
                self,
                example,
                index,
                env_events=env_events,
                generated_at=generated_at,
                export_task_spec=self.export_task_spec,
            )
            tasks.append(task)
            print(f"  [{self.task_type}] {ordinal}/{len(examples)} {task.metadata.get('folder', '')} OK", flush=True)

        report = {
            "generated_at": generated_at,
            "task_type": self.task_type,
            "city_id": self.city_id,
            "requested_count": count,
            "start_index": start_index,
            "excluded_gold_service_count": len(used_service_ids),
            "success_count": len(tasks),
            "failure_count": len(failures),
            "quality": quality_reports,
            "passed_count": sum(1 for item in quality_reports if item.get("passed")),
            "failures": failures,
        }
        write_json(Path(self.output_dir) / "generation_report.json", report)

        if self.export_specs_bundle and examples:
            bundle_path = export_specs_bundle(
                task_type=self.task_type,
                city_id=self.city_id,
                variant=self.variant,
                schema_version=batch_config.schema_version,
                examples=examples,
                output_path=Path(self.output_dir) / "task_specs.json",
            )
            print(f"[{self.task_type}] Exported specs bundle: {bundle_path}", flush=True)

        print(
            f"[{self.task_type}] Generated {len(tasks)}/{count} tasks "
            f"({len(failures)} failures logged)",
            flush=True,
        )
        return tasks


def _places_for_example(world: dict[str, Any], example: dict[str, Any]) -> list[dict[str, Any]]:
    from task_module.curated.exporter import _places_for_example as _impl

    return _impl(world, example)


def _characters_for_example(characters: dict[str, Any], example: dict[str, Any]) -> list[dict[str, Any]]:
    from task_module.curated.exporter import _characters_for_example as _impl

    return _impl(characters, example)
