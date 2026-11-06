"""Task module — generate benchmark tasks from world + character data.

Two-phase pipeline:
  Phase 1: EventFactory → event_nodes.json (event primitives)
  Phase 2: Per-type generators → tasks/{type}/ folders

8 task types (see TASK_TYPES in task_schema.py):
  Atomic:    place_grounding, place_discovery, route_planning
  Compositional: multi_entity, itinerary, safety_planning, region_aware
  Overlay:   dynamic_replanning

Usage:
    from task_module import TaskGenerator, TaskGeneratorConfig

    config = TaskGeneratorConfig(task_type_counts={"place_grounding": 3, "route_planning": 2})
    gen = TaskGenerator(config)
    gen.generate()

CLI:
    python -m task_module.generator --city tokyo_complex --full-benchmark --demo
    python -m task_module.generator --city tokyo_complex --types place_grounding,route_planning --counts 5,5 --demo
"""


def __getattr__(name: str):
    if name == "TaskGenerator":
        from task_module.generator import TaskGenerator
        return TaskGenerator
    if name == "TaskGeneratorConfig":
        from task_module.config import TaskGeneratorConfig
        return TaskGeneratorConfig
    if name in {"BenchmarkTask", "EnvEventNode", "JudgeNode", "JudgeStage",
                 "EventNodeSet", "BenchmarkManifest", "ManifestEntry",
                 "TaskTypeConfig", "TASK_TYPES"}:
        from task_module.task_schema import (
            BenchmarkTask, EnvEventNode, JudgeNode, JudgeStage,
            EventNodeSet, BenchmarkManifest, ManifestEntry,
            TaskTypeConfig, TASK_TYPES,
        )
        return locals()[name]
    raise AttributeError(f"module 'task_module' has no attribute {name!r}")


__all__ = [
    "TaskGenerator", "TaskGeneratorConfig",
    "BenchmarkTask", "EnvEventNode", "JudgeNode", "JudgeStage",
    "EventNodeSet", "BenchmarkManifest", "ManifestEntry",
    "TaskTypeConfig", "TASK_TYPES",
]
