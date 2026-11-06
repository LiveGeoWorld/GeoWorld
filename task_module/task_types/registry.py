"""Task type registry — maps task_type keys to their generator classes."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from task_module._shared.base_generator import BaseTaskGenerator


def get_task_generator(task_type: str, **kwargs) -> "BaseTaskGenerator":
    """Factory: return the generator instance for a given task_type.

    Args:
        task_type: one of TASK_TYPES keys
        **kwargs: passed to the generator constructor
            (world, characters, env_events, judge_nodes, output_dir, seed, ...)
    """
    if task_type == "place_grounding":
        from task_module.task_types.place_grounding.generator import PlaceGroundingGenerator
        return PlaceGroundingGenerator(**kwargs)
    elif task_type == "place_discovery":
        from task_module.task_types.place_discovery.generator import PlaceDiscoveryGenerator
        return PlaceDiscoveryGenerator(**kwargs)
    elif task_type == "route_planning":
        from task_module.task_types.route_planning.generator import RoutePlanningGenerator
        return RoutePlanningGenerator(**kwargs)
    elif task_type == "multi_entity":
        from task_module.task_types.multi_entity.generator import MultiEntityGenerator
        return MultiEntityGenerator(**kwargs)
    elif task_type == "itinerary":
        from task_module.task_types.itinerary.generator import ItineraryGenerator
        return ItineraryGenerator(**kwargs)
    elif task_type == "safety_planning":
        from task_module.task_types.safety_planning.generator import SafetyPlanningGenerator
        return SafetyPlanningGenerator(**kwargs)
    elif task_type == "region_aware":
        from task_module.task_types.region_aware.generator import RegionAwareGenerator
        return RegionAwareGenerator(**kwargs)
    elif task_type == "dynamic_replanning":
        from task_module.task_types.dynamic_replanning.generator import DynamicReplanningGenerator
        return DynamicReplanningGenerator(**kwargs)
    else:
        raise ValueError(f"Unknown task_type: {task_type}")
