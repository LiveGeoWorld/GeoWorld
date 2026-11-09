"""Grounding construction using the existing benchmark orchestrator.

Run from the repository root:
    python -m task_module.generator --world-id YOUR_WORLD --city YOUR_CITY --types place_grounding --counts 1 --new-batch

Other task-generation flows are pending migration and integration.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.io import read_json, write_json

from task_module.config import TaskGeneratorConfig
from task_module._shared.batch_state import ensure_batch_seed, existing_task_count, incomplete_task_directories, record_generation_run
from task_module._shared.base_generator import make_folder_name
from task_module._shared.benchmark_layout import batch_dir as managed_batch_dir, task_dirs
from world_module.registry import (
    city_version_context,
    refresh_city_route_cache_asset,
    write_task_world_provenance,
)
from world_module.map._internal.factual_service_index import materialize_factual_services, read_factual_service_index
from task_module.task_schema import (
    BenchmarkTask, ManifestEntry, BenchmarkManifest, TaskTypeConfig, TASK_TYPES,
)

# task_module lives directly below the pipeline root.  World registries and
# managed artifacts are rooted here, not in the outer repository directory.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _emit(message: str) -> None:
    """Best-effort terminal diagnostics must not invalidate formal artifacts."""
    try:
        print(message, flush=True)
    except OSError:
        pass


class TaskGenerator:
    """Generate benchmark tasks from world + character data across 8 task types."""

    def __init__(self, config: TaskGeneratorConfig | None = None):
        self.config = config or TaskGeneratorConfig()

    def generate(self) -> dict[str, list[BenchmarkTask]]:
        """Run Phase 1 → Phase 2 for all enabled task types.

        Returns:
            Dict mapping task_type → list of BenchmarkTask objects
        """
        cfg = self.config

        unsupported = [kind for kind, count in self._resolve_type_counts().items()
                       if count > 0 and kind not in {'place_grounding', 'place_discovery', 'route_planning', 'multi_entity', 'safety_planning', 'region_aware', 'itinerary'}]
        if unsupported:
            raise NotImplementedError('Pending migration and integration: ' + ', '.join(unsupported))

        world_path = self._resolve_world_path()
        characters_path = self._resolve_characters_path()

        _emit(f"[TaskGenerator] world: {world_path}")
        _emit(f"[TaskGenerator] characters: {characters_path}")

        # A factual service index is a deterministic view of stored POIs, not
        # a city-version-changing asset.  Hydrate it at read time so legacy
        # maps that predate the index remain usable without version migration.
        raw_world = read_json(world_path)
        service_index_path = self._resolve_factual_service_index_path(world_path)
        indexed_services = read_factual_service_index(service_index_path, raw_world) if service_index_path else None
        if service_index_path and indexed_services is None:
            _emit(f"[TaskGenerator] factual service index unavailable or stale; rebuilding from POI types: {service_index_path}")
        world = materialize_factual_services(raw_world, indexed_services=indexed_services)
        characters = read_json(characters_path) if Path(characters_path).exists() else {}

        city = (world.get("cities") or [{}])[0]
        city_id = city.get("city_id", cfg.city_id)
        base_dir = Path(cfg.output_root)

        # Route-derived task assets must remain bound to the selected world
        # and city version, rather than falling through to a global city cache.
        route_cache_context = {}
        if cfg.world_id:
            version_context = city_version_context(cfg.world_id)
            active_version = int(version_context["active_city_versions"].get(city_id, 0) or 0)
            if not active_version:
                raise ValueError(f"Managed world {cfg.world_id} has no active version for {city_id}.")
            for key in ("GEOWORD_ROUTE_CACHE_ROOT", "GEOWORD_ROUTE_CACHE_NAMESPACE"):
                route_cache_context[key] = os.environ.get(key)
            os.environ["GEOWORD_ROUTE_CACHE_ROOT"] = str(
                ROOT / "outputs" / "worlds" / cfg.world_id / "artifacts" / "routes"
            )
            os.environ["GEOWORD_ROUTE_CACHE_NAMESPACE"] = f"v{active_version}"

        if not cfg.world_id:
            raise ValueError("Managed task generation requires --world-id so every batch is world-bound.")

        # ── Batch resolution ──
        batch_ts = _resolve_batch(cfg, base_dir, city_id, cfg.world_id)

        # ── Phase 1: EventFactory ──
        _emit("\n" + "=" * 50)
        _emit("[TaskGenerator] Phase 1: Static judge contracts")
        _emit("=" * 50)

        from task_module.event_factory.judge_nodes import generate_judge_nodes
        judge_nodes = generate_judge_nodes(world, characters, city_id)
        env_events: list[Any] = []
        _emit(f"[TaskGenerator] Generated {len(judge_nodes)} rule-based judge nodes; dynamic events=0")

        # ── Phase 2: Per-type generation ──
        _emit("\n" + "=" * 50)
        _emit("[TaskGenerator] Phase 2: Task Types")
        _emit("=" * 50)

        # Determine which types and how many tasks
        type_counts = self._resolve_type_counts()

        all_tasks: dict[str, list[BenchmarkTask]] = {}
        manifest_entries: dict[str, list[ManifestEntry]] = {}
        total_tasks = 0

        for task_type, count in type_counts.items():
            if count <= 0:
                continue
            type_config = TASK_TYPES.get(task_type, TaskTypeConfig(task_type=task_type))

            # Structure: benchmarks/{task_type}/{world_id}/{city_id}/{batch_ts}/
            type_dir = managed_batch_dir(base_dir, task_type, cfg.world_id, city_id, batch_ts)
            # A formal batch is an initialized bundle before provenance is
            # recorded.  The first task folder remains a candidate beneath it.
            (type_dir / "tasks").mkdir(parents=True, exist_ok=True)
            incomplete = incomplete_task_directories(type_dir)
            if incomplete:
                names = ", ".join(path.name for path in incomplete[:5])
                raise RuntimeError(
                    f"Cannot append to a batch containing incomplete task directories: {names}. "
                    "Create a new batch instead."
                )
            existing_task_folders = {path.name for path in task_dirs(type_dir)}
            existing_before = existing_task_count(type_dir)
            generation_mode = "append" if existing_before else "new"
            # Freeze the world/city/version context before the first candidate
            # is written.  Each later formalized task reuses this context, so
            # a crash cannot leave an otherwise valid task unbound.
            batch_provenance = write_task_world_provenance(
                cfg.world_id,
                city_id,
                type_dir,
                task_type=task_type,
                mode=generation_mode,
                task_folders=[],
            )
            generation_context = (batch_provenance.get("generation_contexts") or [])[-1]
            if not isinstance(generation_context, dict):
                raise RuntimeError("Failed to initialize the task batch world provenance context.")
            _write_batch_metadata(type_dir, cfg.world_id, city_id, task_type, batch_ts)
            preferred_batch_seed = (
                int(os.environ.get("GEOWORD_ROUTE_PREFLIGHT_SEED") or 0)
                if task_type == "route_planning"
                else int(os.environ.get("GEOWORD_MULTI_ENTITY_PREFLIGHT_SEED") or 0)
                if task_type == "multi_entity"
                else int(os.environ.get("GEOWORD_ITINERARY_PREFLIGHT_SEED") or 0)
                if task_type == "itinerary"
                else int(os.environ.get("GEOWORD_SAFETY_PREFLIGHT_SEED") or 0)
                if task_type == "safety_planning"
                else int(os.environ.get("GEOWORD_REGION_PREFLIGHT_SEED") or 0)
                if task_type == "region_aware"
                else int(os.environ.get("GEOWORD_DYNAMIC_PREFLIGHT_SEED") or 0)
                if task_type == "dynamic_replanning" else 0
            )
            persisted_batch_seed = ensure_batch_seed(type_dir, preferred_batch_seed or None)
            generator = self._build_generator(
                task_type, world, characters,
                env_events, judge_nodes, str(type_dir), persisted_batch_seed,
            )
            generator._task_world_binding = {
                "world_id": cfg.world_id,
                "city_id": city_id,
                "batch_dir": type_dir,
                "task_type": task_type,
                "mode": generation_mode,
                "generation_context": generation_context,
            }
            generator.initialize_cost_ledger(generator._task_world_binding)

            _emit(f"\n--- [{task_type}] {type_config.label_zh}: {count} tasks ---")
            # A task becomes effective only after its own folder is
            # canonicalized and stamped by BaseTaskGenerator.finalize_task_folder.
            # ``count`` is the requested total for this selected batch, not
            # an append amount.  A reuse therefore retains valid siblings and
            # generates only the missing number.
            generator._requested_count = count
            tasks: list[BenchmarkTask] = []
            repair_round = 0
            formal_task_dirs = _formal_task_dirs(type_dir)
            while len(formal_task_dirs) < count and repair_round <= 2:
                needed = _remaining_task_count(count, len(formal_task_dirs))
                if repair_round:
                    _emit(
                        f"[TaskProgress] phase=repair round={repair_round}/2 missing={needed} requested={count}",
                    )
                with generator.cost_capture():
                    produced = generator.generate(needed)
                tasks.extend(produced)
                updated_dirs = _formal_task_dirs(type_dir)
                if len(updated_dirs) == len(formal_task_dirs) and not produced:
                    break
                formal_task_dirs = updated_dirs
                repair_round += 1

            if len(formal_task_dirs) < count:
                rejected = _rejected_task_reports(type_dir)
                details = "; ".join(
                    f"{item.get('folder', '?')}: {', '.join(item.get('errors') or [])}"
                    for item in rejected[-5:]
                ) or "generator did not produce enough contract-valid task folders"
                raise RuntimeError(
                    f"Formal task validation incomplete for {task_type}: "
                    f"valid {len(formal_task_dirs)}/{count} after {repair_round} rounds. {details}"
                )

            formal_names = {path.name for path in formal_task_dirs}
            tasks = [task for task in tasks if _task_folder_name(task) in formal_names]
            _localize_task_assets(formal_task_dirs, type_dir)
            all_tasks[task_type] = tasks
            total_tasks += len(tasks)
            record_generation_run(type_dir, {
                "task_type": task_type,
                "city_id": city_id,
                "world_id": cfg.world_id,
                "batch": batch_ts,
                "mode": generation_mode,
                "batch_seed": persisted_batch_seed,
                "seed": generator.seed,
                "requested_count": count,
                "existing_task_count_before": existing_before,
                "generated_task_count": len(tasks),
                "generated_task_ids": [task.task_id for task in tasks],
                "generated_task_folders": [_task_folder_name(task) for task in tasks],
                "formal_task_count_after": len(formal_task_dirs),
            })
            if cfg.world_id:
                write_task_world_provenance(
                    cfg.world_id,
                    city_id,
                    type_dir,
                    task_type=task_type,
                    mode=generation_mode,
                    # Individual task provenance is written during
                    # finalize_task_folder.  Do not overwrite a task-specific
                    # derived-asset snapshot (for example, itinerary routes
                    # saved immediately before that task became formal).
                    task_folders=[],
                    generation_context=generation_context,
                )
            self._record_validation_progress(task_type, len(formal_task_dirs), total_tasks)

            # Build manifest entries
            entries = []
            for t in tasks:
                chars = [c.get("name", "?") for c in
                         t.world_slice_summary.characters[:3]]
                summary = t.summary or {}
                scope = summary.get("candidate_scope") or {}
                quality = summary.get("quality_gate") or {}
                gold_summary = summary.get("gold_summary") or {}
                allowed_types = scope.get("allowed_place_types") or []
                # Compatibility fallback for legacy task objects missing folder metadata.
                folder_name = make_folder_name(int(t.task_id.rsplit("_", 1)[-1]), t.user_prompt)
                entries.append(ManifestEntry(
                    task_id=t.task_id,
                    folder=t.metadata.get("folder", folder_name),
                    task_type=task_type,
                    query_preview=t.user_prompt[:30] + "...",
                    difficulty=t.difficulty,
                    characters=chars,
                    env_event_count=len(t.involved_env_events),
                    judge_stages=len(t.judge_stages),
                    review_status=summary.get("review_status", ""),
                    prompt_language=summary.get("prompt_language", ""),
                    grounding_target_type=summary.get("grounding_target_type", ""),
                    acceptable_granularity=summary.get("acceptable_granularity", ""),
                    category=allowed_types[0] if allowed_types else "",
                    service_type=summary.get("service_type", ""),
                    top_candidate=gold_summary.get("top_candidate", ""),
                    quality_passed=quality.get("passed") if isinstance(quality, dict) else None,
                ))
            manifest_entries[task_type] = entries

        # Write a batch manifest from the canonical folders, rather than
        # assuming a particular historical manifest layout.  This makes an
        # append safe even when earlier tasks were produced by a family
        # generator that did not write a manifest of its own.
        for task_type in manifest_entries:
            generated_at = datetime.now(timezone.utc).isoformat()
            type_dir = managed_batch_dir(base_dir, task_type, cfg.world_id, city_id, batch_ts)
            combined_entries = _manifest_entries_from_task_folders(type_dir, task_type)
            manifest = BenchmarkManifest(
                city_id=city_id,
                variant="",
                generated_at=generated_at,
                task_count=len(combined_entries),
                tasks=combined_entries,
                tasks_by_type={task_type: combined_entries},
            )
            write_json(type_dir / "benchmark_manifest.json", asdict(manifest))

            # Create/update latest symlink (best-effort on Windows without admin)
            latest_link = type_dir.parent / "latest"
            try:
                if latest_link.is_symlink() or not latest_link.exists():
                    latest_link.unlink(missing_ok=True)
                    latest_link.symlink_to(batch_ts, target_is_directory=True)
            except OSError:
                pass

        _emit(f"\n[TaskGenerator] Done. {total_tasks} tasks across {len(all_tasks)} types.")
        _emit(f"[TaskGenerator] Output: {base_dir}/")
        if cfg.world_id:
            route_refresh = refresh_city_route_cache_asset(cfg.world_id, city_id)
            if route_refresh.get("updated"):
                _emit(f"[TaskGenerator] Registered route cache for {city_id} {route_refresh['city_version']}.")
        for key, value in route_cache_context.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return all_tasks

    def _record_validation_progress(self, task_type: str, generated_count: int, total_count: int) -> None:
        """Persist formal-run progress without changing normal task batches."""
        state_path = str(self.config.validation_state_path or "")
        if not state_path:
            return
        path = Path(state_path)
        try:
            state = read_json(path)
        except Exception:
            return
        stage = state.setdefault("task_generation", {})
        completed = stage.setdefault("completed_by_type", {})
        completed[task_type] = generated_count
        stage["completed_total"] = total_count
        stage["current_type"] = task_type
        stage["status"] = "running"
        write_json(path, state)

    def _build_generator(self, task_type, world, characters, env_events, judge_nodes, output_dir, persisted_batch_seed):
        from task_module.task_types.registry import get_task_generator
        kwargs = {
            "world": world,
            "characters": characters,
            "env_events": env_events,
            "judge_nodes": judge_nodes,
            "output_dir": output_dir,
            "seed": persisted_batch_seed,
            "use_llm_prompt": self.config.use_llm_user_prompt,
            "config_yaml": self.config.config_yaml,
        }
        if task_type in {"place_grounding", "place_discovery"}:
            # Evidence-task prompt writers must use the model selected for
            # this build rather than their historical vision_parse default.
            kwargs["stage_model_key"] = self.config.stage_model_key
            kwargs["curated_examples_dir"] = self.config.curated_examples_dir
            kwargs["require_quality_pass"] = self.config.require_quality_pass
            kwargs["env_event_count"] = self.config.env_event_count
            kwargs["max_attempts_per_slot"] = self.config.max_attempts_per_slot
            kwargs["export_task_spec"] = self.config.export_task_spec
            kwargs["export_specs_bundle"] = self.config.export_specs_bundle
            kwargs["variant"] = self.config.variant
            if self.config.from_specs_path:
                from task_module.curated.schema import resolve_specs_path_for_type

                kwargs["from_specs_path"] = str(resolve_specs_path_for_type(self.config.from_specs_path, task_type))
                kwargs["env_seed"] = self.config.env_seed
        if task_type == "dynamic_replanning":
            kwargs["pretask_admission"] = self.config.dynamic_pretask_admission
            kwargs["derivation_plan"] = self.config.dynamic_derivation_plan
            kwargs["require_time_context"] = self.config.dynamic_require_time_context
            # Dynamic overlays must consume static pretasks from this exact
            # build root, never an ambient historical benchmark directory.
            kwargs["pretask_root"] = self.config.output_root
            kwargs["world_id"] = self.config.world_id
            if self.config.world_id:
                version_context = city_version_context(self.config.world_id)
                kwargs["city_version"] = int(
                    version_context["active_city_versions"].get(self.config.city_id, 0) or 0
                )
        if task_type in {"multi_entity", "itinerary", "safety_planning", "region_aware", "dynamic_replanning"}:
            kwargs["stage_model_key"] = self.config.stage_model_key
        if task_type == "safety_planning":
            from task_module.task_types.safety_planning.generation_inputs import resolve_assets, time_sources
            assets = resolve_assets(ROOT, self.config.world_id, self.config.city_id)
            kwargs["safety_assets"] = assets
            kwargs["safety_time_sources"] = time_sources(
                benchmark_root=Path(self.config.output_root).resolve(), world_id=self.config.world_id,
                city_id=self.config.city_id, assets=assets,
            )
            kwargs["max_attempts_per_slot"] = self.config.max_attempts_per_slot
        if task_type == "region_aware":
            constraints_path, evidence_path = self._region_cultural_asset_paths()
            kwargs["cultural_constraints_path"] = str(constraints_path)
            kwargs["cultural_evidence_path"] = str(evidence_path)
            rules_path, bindings_path = self._region_stage3_asset_paths()
            kwargs["regional_rules_path"] = str(rules_path)
            kwargs["regional_bindings_path"] = str(bindings_path)
            kwargs["service_detail_cache_path"] = str(self._region_service_detail_cache_path())
            kwargs["route_cache_path"] = str(self._region_route_cache_path())
        if task_type == "itinerary":
            kwargs["infeasible_percent"] = int(
                self.config.infeasible_percent_by_type.get(task_type, 0)
            )
        return get_task_generator(task_type, **kwargs)

    def _region_cultural_asset_paths(self) -> tuple[Path, Path]:
        """Resolve Region inputs from the selected managed city version only."""
        if not self.config.world_id:
            raise ValueError("Region-Aware formal generation requires a managed world binding.")
        record_path = ROOT / "outputs" / "worlds" / self.config.world_id / "cities" / f"{self.config.city_id}.json"
        if not record_path.is_file():
            raise FileNotFoundError(f"Region city registry is missing: {record_path}")
        record = read_json(record_path)
        active_version = int(str(record.get("active_version", 0)).lstrip("vV") or 0)
        active = next(
            (item for item in record.get("city_versions", []) if int(item.get("version", 0) or 0) == active_version),
            None,
        )
        artifacts = (active or record).get("artifacts", {})
        constraints = Path(str((artifacts.get("cultural_constraints") or {}).get("path") or ""))
        evidence = Path(str((artifacts.get("cultural_evidence") or {}).get("path") or ""))
        if not constraints.is_file() or not evidence.is_file():
            raise FileNotFoundError(
                f"Region-Aware requires world-local cultural assets for {self.config.world_id}/{self.config.city_id}/v{active_version}."
            )
        return constraints, evidence

    def _region_stage3_asset_paths(self) -> tuple[Path, Path]:
        """Resolve only the completed formal Stage 1/2 asset family."""
        if not self.config.world_id:
            raise ValueError("Region Stage 3 requires a managed world binding.")
        record_path = ROOT / "outputs" / "worlds" / self.config.world_id / "cities" / f"{self.config.city_id}.json"
        if not record_path.is_file():
            raise FileNotFoundError(f"Region city registry is missing: {record_path}")
        record = read_json(record_path)
        active_version = int(str(record.get("active_version", 0)).lstrip("vV") or 0)
        if not active_version:
            raise ValueError(
                f"Region Stage 3 requires an active city version for "
                f"{self.config.world_id}/{self.config.city_id}."
            )

        root = (
            ROOT / "outputs" / "worlds" / self.config.world_id / "artifacts"
            / "cultural" / self.config.city_id / "region_source_pipeline_formal"
        )
        rules = root / "regional_rules.json"
        bindings = root / "regional_rule_stage2_assessments.json"
        manifest_path = root / "run_manifest.json"
        if not rules.is_file() or not bindings.is_file():
            raise FileNotFoundError(
                f"Region Stage 3 formal source assets are missing for "
                f"{self.config.world_id}/{self.config.city_id}."
            )
        if not manifest_path.is_file():
            raise FileNotFoundError(f"Region Stage 3 run manifest is missing: {manifest_path}")
        manifest = read_json(manifest_path)
        result = manifest.get("result") if isinstance(manifest.get("result"), dict) else {}
        binding = manifest.get("stage2_world_binding")
        if result.get("stage") != "stage2_complete" or not isinstance(binding, dict):
            raise ValueError("Region Stage 3 requires a completed formal Stage 2 source pipeline.")
        expected = {
            "world_id": self.config.world_id,
            "city_id": self.config.city_id,
            "city_version": f"v{active_version}",
        }
        actual = {key: str(binding.get(key) or "") for key in expected}
        if actual != expected:
            raise ValueError(
                "Region Stage 3 formal source binding mismatch: "
                f"expected {expected!r}, got {actual!r}."
            )
        return rules, bindings

    def _region_service_detail_cache_path(self) -> Path:
        """Resolve the complete, map-version-bound service cache for Region Stage 3."""
        if not self.config.world_id:
            raise ValueError("Region Stage 3 requires a managed world binding.")
        record_path = ROOT / "outputs" / "worlds" / self.config.world_id / "cities" / f"{self.config.city_id}.json"
        if not record_path.is_file():
            raise FileNotFoundError(f"Region city registry is missing: {record_path}")
        record = read_json(record_path)
        active_version = int(str(record.get("active_version", 0)).lstrip("vV") or 0)
        active = next(
            (item for item in record.get("city_versions", []) if int(item.get("version", 0) or 0) == active_version),
            None,
        )
        artifacts = (active or record).get("artifacts", {})
        cache = Path(str((artifacts.get("service_detail_cache") or {}).get("path") or ""))
        if not cache.is_file():
            raise FileNotFoundError(
                f"Region Stage 3 requires a complete service detail cache for "
                f"{self.config.world_id}/{self.config.city_id}/v{active_version}."
            )
        return cache

    def _region_route_cache_path(self) -> Path:
        """Resolve the existing route cache bound to the active Region city version."""
        if not self.config.world_id:
            raise ValueError("Region Stage 3 requires a managed world binding.")
        record_path = ROOT / "outputs" / "worlds" / self.config.world_id / "cities" / f"{self.config.city_id}.json"
        if not record_path.is_file():
            raise FileNotFoundError(f"Region city registry is missing: {record_path}")
        record = read_json(record_path)
        active_version = int(str(record.get("active_version", 0)).lstrip("vV") or 0)
        active = next(
            (item for item in record.get("city_versions", []) if int(item.get("version", 0) or 0) == active_version),
            None,
        )
        artifacts = (active or record).get("artifacts", {})
        route_cache = Path(str((artifacts.get("route_cache") or {}).get("path") or ""))
        if not str(route_cache) or str(route_cache) == ".":
            route_cache = (
                ROOT / "outputs" / "worlds" / self.config.world_id / "artifacts" /
                "routes" / self.config.city_id / f"v{active_version}" / "route_cache.json"
            )
        route_cache.parent.mkdir(parents=True, exist_ok=True)
        return route_cache

    def _resolve_type_counts(self) -> dict[str, int]:
        cfg = self.config
        if cfg.task_type_counts:
            counts = dict(cfg.task_type_counts)
        elif cfg.full_benchmark:
            counts = {t: tc.default_count for t, tc in TASK_TYPES.items()}
        else:
            # Legacy: distribute task_count across all types
            types = list(TASK_TYPES.keys())
            per_type = max(1, cfg.task_count // len(types))
            counts = {t: per_type for t in types}

        if cfg.enabled_types:
            counts = {t: c for t, c in counts.items() if t in cfg.enabled_types}

        return counts

    def _resolve_world_path(self) -> str:
        if self.config.world_path:
            return self.config.world_path
        if self.config.world_id:
            record_path = ROOT / "outputs" / "worlds" / self.config.world_id / "cities" / f"{self.config.city_id}.json"
            if not record_path.is_file():
                raise FileNotFoundError(f"City {self.config.city_id} is not registered in world {self.config.world_id}.")
            artifact = (read_json(record_path).get("artifacts") or {}).get("map_world") or {}
            if artifact.get("status") != "available":
                raise FileNotFoundError(f"World {self.config.world_id} has no available map artifact for {self.config.city_id}.")
            return str(ROOT / artifact["path"])
        city = self.config.city_id
        # Try exact match + suffixes (_smoke, _complex, _large, _base)
        for suffix in ["", "_smoke", "_complex", "_large", "_base"]:
            for variant in ["complex", "base"]:
                p = f"outputs/mapseeds/{city}{suffix}/{variant}/world.json"
                if Path(p).exists():
                    return str(p)
        raise FileNotFoundError(
            f"No world.json found for {city}. Run map_module first."
        )

    def _resolve_characters_path(self) -> str:
        if self.config.characters_path:
            return self.config.characters_path
        if self.config.world_id:
            record_path = ROOT / "outputs" / "worlds" / self.config.world_id / "cities" / f"{self.config.city_id}.json"
            if not record_path.is_file():
                return ""
            artifact = (read_json(record_path).get("artifacts") or {}).get("characters") or {}
            if artifact.get("status") == "available":
                return str(ROOT / artifact["path"])
            return ""
        city = self.config.city_id
        for suffix in ["", "_smoke", "_complex", "_large", "_base"]:
            for variant in ["complex", "base", "demo"]:
                p = f"outputs/characters/{city}{suffix}/{variant}/characters.json"
                if Path(p).exists():
                    return str(p)
            # demo variant path
            p = f"outputs/characters/{city}{suffix}/demo/characters_validation.json"
            if Path(p).exists():
                return str(p)
        return ""

    def _resolve_factual_service_index_path(self, world_path: str) -> str:
        """Resolve a managed version's derived service index without changing it."""
        if self.config.world_id:
            record_path = ROOT / "outputs" / "worlds" / self.config.world_id / "cities" / f"{self.config.city_id}.json"
            if record_path.is_file():
                artifact = (read_json(record_path).get("artifacts") or {}).get("factual_service_index") or {}
                if artifact.get("status") == "available":
                    path = ROOT / str(artifact.get("path") or "")
                    if path.is_file():
                        return str(path)
        fallback = Path(world_path).parent / "factual_service_index.json"
        return str(fallback) if fallback.is_file() else ""

def _resolve_batch(cfg, base_dir: Path, city_id: str, world_id: str) -> str:
    """Resolve batch timestamp directory. Creates tasks/ logs/ structure."""
    from datetime import datetime, timezone

    if cfg.new_batch or cfg.batch:
        ts = cfg.batch or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    else:
        # An implicit append never crosses task types or worlds.  The caller
        # supplies a task type later, so only explicit UI append is supported.
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return ts


def _localize_task_assets(task_folders: list[Path], batch_dir: Path) -> None:
    """Materialize grounding image evidence inside each task's immutable bundle.

    Other task types already write their logs, route sandboxes and evidence
    inside the task or batch directory.  Only the historical grounding pipeline
    writes Street View files outside the task, so it needs this post-write step.
    """
    from task_module._shared.task_assets import localize_task_assets
    for task_folder in task_folders:
        localize_task_assets(task_folder)


def _formal_task_dirs(batch_dir: Path) -> list[Path]:
    """Return all formal task folders already present in a managed batch."""
    formal: list[Path] = []
    for path in task_dirs(batch_dir):
        report_path = path / "task_validation.json"
        if not report_path.is_file():
            continue
        try:
            report = read_json(report_path)
        except Exception:
            continue
        if report.get("valid") and report.get("state") == "formal":
            formal.append(path)
    return formal


def _remaining_task_count(requested_total: int, existing_formal_count: int) -> int:
    """Return the number needed to reach a batch's requested final total."""
    return max(0, int(requested_total) - int(existing_formal_count))


def _rejected_task_reports(batch_dir: Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    rejected_root = batch_dir / "candidates" / "rejected"
    if not rejected_root.is_dir():
        return reports
    for path in sorted(rejected_root.iterdir()):
        report_path = path / "task_validation.json"
        if not report_path.is_file():
            continue
        try:
            report = read_json(report_path)
        except Exception:
            continue
        if isinstance(report, dict):
            reports.append({"folder": path.name, **report})
    return reports


def _task_folder_name(task: BenchmarkTask) -> str:
    """Match the writer's portable folder-name rule for every task family."""
    metadata = task.metadata or {}
    if metadata.get("folder"):
        return str(metadata["folder"])
    try:
        index = int(str(task.task_id).rsplit("_", 1)[-1])
    except (TypeError, ValueError):
        return ""
    return make_folder_name(index, task.user_prompt)


def _write_batch_metadata(batch_dir: Path, world_id: str, city_id: str, task_type: str, batch_id: str) -> None:
    """Keep a human-facing label separate from the immutable technical ID."""
    path = batch_dir / "batch_metadata.json"
    try:
        metadata = read_json(path) if path.is_file() else {}
    except Exception:
        metadata = {}
    metadata.setdefault("schema_version", "geoword.benchmark.batch_metadata.v1")
    metadata.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    metadata.update({
        "world_id": world_id,
        "city_scope": city_id,
        "task_type": task_type,
        "batch_id": batch_id,
        "lifecycle": "formal_pending_review",
        "display_name": metadata.get("display_name") or "正式任务构建",
    })
    write_json(path, metadata)


def _load_prior_manifest_entries(manifest_path: Path, task_type: str) -> list[ManifestEntry]:
    """Load both historical manifest layouts before appending new entries."""
    if not manifest_path.is_file():
        return []
    try:
        prior_manifest = read_json(manifest_path)
    except Exception:
        return []
    raw_entries = prior_manifest.get("tasks") or (
        (prior_manifest.get("tasks_by_type") or {}).get(task_type) or []
    )
    return [ManifestEntry(**entry) for entry in raw_entries if isinstance(entry, dict)]


def _manifest_entries_from_task_folders(batch_dir: Path, task_type: str) -> list[ManifestEntry]:
    """Build the release index from canonical task artifacts in a batch."""
    tasks_dir = batch_dir / "tasks"
    if not tasks_dir.is_dir():
        return []
    entries: list[ManifestEntry] = []
    for task_dir in sorted(path for path in tasks_dir.iterdir() if path.is_dir()):
        task_path = task_dir / "task.json"
        if not task_path.is_file():
            continue
        try:
            task = read_json(task_path)
        except Exception:
            continue
        summary = task.get("summary") or {}
        scope = summary.get("candidate_scope") or {}
        quality = summary.get("quality_gate") or {}
        gold_summary = summary.get("gold_summary") or {}
        allowed_types = scope.get("allowed_place_types") or []
        entries.append(ManifestEntry(
            task_id=task.get("task_id", ""),
            folder=task_dir.name,
            task_type=task.get("task_type") or task_type,
            query_preview=(task.get("user_prompt", "")[:30] + "..."),
            difficulty=task.get("difficulty", ""),
            review_status=summary.get("review_status", ""),
            prompt_language=summary.get("prompt_language", ""),
            grounding_target_type=summary.get("grounding_target_type", ""),
            acceptable_granularity=summary.get("acceptable_granularity", ""),
            category=allowed_types[0] if allowed_types else "",
            service_type=summary.get("service_type", ""),
            top_candidate=gold_summary.get("top_candidate", ""),
            quality_passed=quality.get("passed") if isinstance(quality, dict) else None,
        ))
    return entries


def _setup_task_log(log_path: Path) -> None:
    """Setup logging to a batch-specific generate.log file."""
    import logging, sys
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(str(log_path), encoding="utf-8"),
        ],
        force=True,
    )




# ── CLI ──

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate benchmark tasks — 8 types, 2 phases.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick test: 3 tasks of each type (24 total)
  python -m task_module.generator --city tokyo_complex --counts 3 --demo

  # Only 2 specific types
  python -m task_module.generator --city tokyo_complex --types place_grounding,route_planning --counts 5,5 --demo

  # Full benchmark (all 8 types, default counts)
  python -m task_module.generator --city tokyo_complex --full-benchmark --demo
""",
    )

    parser.add_argument("--city", default="tokyo")
    parser.add_argument("--variant", default="complex")
    parser.add_argument("--seed", type=int, default=123)
    parser.add_argument("--world", default="")
    parser.add_argument("--world-id", default="", help="Managed world version owning this task batch.")
    parser.add_argument("--characters", default="")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--stage-model", default="scenario_patch",
                        help="stage_models key used for scenario wording and dynamic event generation")
    parser.add_argument('--dynamic-derivation-plan', default='{}', type=__import__('json').loads)
    parser.add_argument('--dynamic-preflight-seed', type=int, default=None)
    parser.add_argument('--dynamic-pretask-admission', default='all_validated_static',
                        choices=['all_validated_static', 'audit_approved_only'])
    parser.add_argument("--output", default="./outputs/benchmarks")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--no-llm-user-prompt", action="store_false", dest="llm_user_prompt", default=True,
                        help="Disable LLM user prompt generation (use template fallback)")
    parser.add_argument("--curated-dir", default="",
                        help="(deprecated) alias for specs directory lookup")
    parser.add_argument("--from-specs", default="",
                        help="Replay approved task_specs.json without LLM/streetview")
    parser.add_argument("--export-specs-bundle", action="store_true",
                        help="Write task_specs.json bundle after evidence generation")
    parser.add_argument("--env-seed", type=int, default=None,
                        help="Fixed env sampling seed for --from-specs replay")
    parser.add_argument("--full-benchmark", action="store_true")
    parser.add_argument("--batch", default="",
                        help="Specify batch timestamp (default: latest)")
    parser.add_argument("--new-batch", action="store_true",
                        help="Create a new timestamp batch directory")
    parser.add_argument("--types", default="",
                        help="Comma-separated task types (e.g. place_grounding,route_planning)")
    parser.add_argument("--counts", default="",
                        help="Comma-separated counts matching --types, or single count for all")
    parser.add_argument("--infeasible-percent", type=int, default=0,
                        help="Integer 0-100. Currently supported by itinerary only.")
    parser.add_argument("--route-diffusion-start", type=int, default=None,
                        help="Routing POI count that starts reuse/diffusion sampling.")
    parser.add_argument("--route-pool-max", type=int, default=None,
                        help="Maximum Routing POI pool size for this generation run.")
    parser.add_argument("--route-preflight-seed", type=int, default=None,
                        help="Batch seed reserved by the accepted Routing cost preflight.")
    parser.add_argument("--multi-entity-preflight-seed", type=int, default=None,
                        help="Batch seed reserved by the accepted Multi-Entity cost preflight.")
    parser.add_argument("--itinerary-preflight-seed", type=int, default=None,
                        help="Batch seed reserved by the accepted Itinerary cost preflight.")
    parser.add_argument("--safety-preflight-seed", type=int, default=None,
                        help="Batch seed reserved by the accepted Safety cost preflight.")
    parser.add_argument("--region-preflight-seed", type=int, default=None,
                        help="Batch seed reserved by the accepted Region cost preflight.")

    args = parser.parse_args()

    if not 0 <= args.infeasible_percent <= 100:
        parser.error("--infeasible-percent must be an integer from 0 to 100")
    if args.route_diffusion_start is not None or args.route_pool_max is not None:
        if args.route_diffusion_start is None or args.route_pool_max is None:
            parser.error("Routing diffusion start and pool max must be supplied together")
        if args.route_diffusion_start < 1 or args.route_pool_max < 2 or args.route_diffusion_start >= args.route_pool_max:
            parser.error("--route-diffusion-start must be positive and smaller than --route-pool-max")
        os.environ["GEOWORD_ROUTE_DIFFUSION_START"] = str(args.route_diffusion_start)
        os.environ["GEOWORD_ROUTE_POOL_MAX"] = str(args.route_pool_max)
    if args.route_preflight_seed is not None:
        if args.route_preflight_seed < 1:
            parser.error("--route-preflight-seed must be positive")
        os.environ["GEOWORD_ROUTE_PREFLIGHT_SEED"] = str(args.route_preflight_seed)
    if args.multi_entity_preflight_seed is not None:
        if args.multi_entity_preflight_seed < 1:
            parser.error("--multi-entity-preflight-seed must be positive")
        os.environ["GEOWORD_MULTI_ENTITY_PREFLIGHT_SEED"] = str(args.multi_entity_preflight_seed)
    if args.itinerary_preflight_seed is not None:
        if args.itinerary_preflight_seed < 1:
            parser.error("--itinerary-preflight-seed must be positive")
        os.environ["GEOWORD_ITINERARY_PREFLIGHT_SEED"] = str(args.itinerary_preflight_seed)
    if args.safety_preflight_seed is not None:
        if args.safety_preflight_seed < 1:
            parser.error("--safety-preflight-seed must be positive")
        os.environ["GEOWORD_SAFETY_PREFLIGHT_SEED"] = str(args.safety_preflight_seed)
    if args.dynamic_preflight_seed is not None:
        if args.dynamic_preflight_seed < 1:
            parser.error('--dynamic-preflight-seed must be positive')
        os.environ['GEOWORD_DYNAMIC_PREFLIGHT_SEED'] = str(args.dynamic_preflight_seed)
    if args.region_preflight_seed is not None:
        if args.region_preflight_seed < 1:
            parser.error("--region-preflight-seed must be positive")
        os.environ["GEOWORD_REGION_PREFLIGHT_SEED"] = str(args.region_preflight_seed)

    # Parse types and counts
    type_counts: dict[str, int] = {}
    if args.types:
        type_list = [t.strip() for t in args.types.split(",") if t.strip()]
        if args.counts:
            count_list = [int(c.strip()) for c in args.counts.split(",") if c.strip()]
            for i, t in enumerate(type_list):
                type_counts[t] = count_list[i] if i < len(count_list) else count_list[-1] if count_list else 3
        else:
            for t in type_list:
                type_counts[t] = 3
    elif args.full_benchmark:
        pass  # Will use full_benchmark flag
    elif args.counts:
        count = int(args.counts.split(",")[0])
        for t in TASK_TYPES:
            type_counts[t] = count
    else:
        # Legacy: single count for all
        count = 5
        for t in TASK_TYPES:
            type_counts[t] = count

    config = TaskGeneratorConfig(
        city_id=args.city,
        variant=args.variant,
        world_id=args.world_id,
        seed=args.seed,
        world_path=args.world,
        characters_path=args.characters,
        config_yaml=args.config,
        stage_model_key=args.stage_model,
        dynamic_pretask_admission=args.dynamic_pretask_admission,
        dynamic_derivation_plan=args.dynamic_derivation_plan,
        output_root=args.output,
        batch=args.batch,
        new_batch=args.new_batch,
        use_llm_env_events=not args.demo,
        use_llm_user_prompt=args.llm_user_prompt and not args.from_specs,
        curated_examples_dir=args.curated_dir,
        from_specs_path=args.from_specs,
        export_specs_bundle=args.export_specs_bundle,
        env_seed=args.env_seed,
        full_benchmark=args.full_benchmark,
        task_type_counts=type_counts if type_counts else None,
        enabled_types=list(type_counts.keys()) if type_counts else None,
        infeasible_percent_by_type={"itinerary": args.infeasible_percent},
    )

    gen = TaskGenerator(config)
    try:
        gen.generate()
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
