"""Configuration for TaskGenerator."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TaskGeneratorConfig:
    """Configuration for benchmark task generation.

    Controls both Phase 1 (event node generation) and
    Phase 2 (task composition across 8 task types).

    Usage:
        # For testing: only a few tasks across types
        cfg = TaskGeneratorConfig(task_type_counts={"place_grounding": 3, "route_planning": 2})

        # For full benchmark: all 8 types with default counts
        cfg = TaskGeneratorConfig(full_benchmark=True)
    """

    # ── Data sources ──
    city_id: str = "tokyo"
    variant: str = "complex"
    # Required for new managed-world builds. Historical CLI calls may leave it
    # empty, but then no world-version sidecar is written.
    world_id: str = ""
    world_path: str = ""
    characters_path: str = ""

    # ── Output ──
    output_root: str = "./outputs/benchmarks"

    # ── Task type counts ──
    # If full_benchmark=True, uses default counts from TASK_TYPES registry.
    # Otherwise, can specify per-type counts as a dict.
    full_benchmark: bool = False
    task_type_counts: dict[str, int] = field(default_factory=dict)
    #   {"place_grounding": 150, "place_discovery": 150, ...}
    task_count: int = 5                     # DEPRECATED: for old single-type mode
    enabled_types: list[str] | None = None  # None = all types; list = only these types

    # ── Generation params ──
    difficulty: str = "mixed"
    seed: int = 123

    # ── Feasibility mix ──
    # A task family opts in only after it can produce a factual private proof
    # for every intentionally infeasible task. Values are integer percentages.
    # Missing entries preserve the historical feasible-only behavior.
    infeasible_percent_by_type: dict[str, int] = field(default_factory=dict)

    # ── Phase 1: Event generation ──
    use_llm_env_events: bool = True

    # ── Phase 2: Task composition ──
    use_llm_user_prompt: bool = True
    curated_examples_dir: str = ""          # Deprecated alias for specs_dir
    from_specs_path: str = ""              # Replay approved specs without LLM/streetview
    export_specs_bundle: bool = False      # Write task_specs.json bundle after generation
    env_seed: int | None = None            # Fixed env sampling seed for --from-specs replay
    require_quality_pass: bool = True       # grounding/discovery: skip failed quality_gate
    env_event_count: int = 3                # grounding/discovery: env events per task
    max_attempts_per_slot: int = 3          # grounding/discovery: retries per requested task
    export_task_spec: bool = True           # write task_spec.json per task folder

    # Dynamic overlay pretask admission. Human audit is optional during early
    # end-to-end verification but provenance is always recorded.
    dynamic_pretask_admission: str = "all_validated_static"
    dynamic_derivation_plan: dict = field(default_factory=dict)
    dynamic_require_time_context: bool = True

    # ── Batch control ──
    batch: str = ""                       # specific batch timestamp (empty = latest)
    new_batch: bool = False               # True = create new timestamp batch
    validation_state_path: str = ""      # Optional persisted formal-run progress file

    # ── LLM ──
    config_yaml: str = "config.yaml"
    stage_model_key: str = "scenario_patch"
    grounding_stage_model_key: str = "vision_parse"
