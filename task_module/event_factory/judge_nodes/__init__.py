"""Judge node generation — rule-based derivation of verification checkpoints.

Judgment nodes are derived deterministically from:
  - Service affordances (searchable, reservable, bookable, cancelable, modifiable)
  - Character relationships (spouse, coworker, friend, family, neighbor)
  - Spatial connections (district transit, place proximity, corridor routing)

These are NOT LLM-generated. They are the building blocks that
task_composer samples and assembles into task judge sequences.
"""

from __future__ import annotations

from typing import Any

from task_module.task_schema import (
    AgentAction,
    CheckItem,
    JudgeNode,
)


def generate_judge_nodes(
    world: dict[str, Any],
    characters: dict[str, Any],
    city_id: str = "",
) -> list[JudgeNode]:
    """Generate all judge nodes from world.json + characters.json.

    Returns a flat list of JudgeNode objects ready for serialization.
    """
    from task_module.event_factory.judge_nodes.service_checks import generate_service_judge_nodes
    from task_module.event_factory.judge_nodes.comm_checks import generate_comm_judge_nodes
    from task_module.event_factory.judge_nodes.nav_checks import generate_nav_judge_nodes

    nodes: list[JudgeNode] = []
    node_idx = 0

    def _next_id() -> str:
        nonlocal node_idx
        node_idx += 1
        return f"jcp_{city_id}_{node_idx:04d}" if city_id else f"jcp_{node_idx:04d}"

    # Service operation checks
    service_nodes = generate_service_judge_nodes(world, city_id, _next_id)
    nodes.extend(service_nodes)

    # Communication checks
    comm_nodes = generate_comm_judge_nodes(characters, city_id, _next_id)
    nodes.extend(comm_nodes)

    # Navigation checks
    nav_nodes = generate_nav_judge_nodes(world, city_id, _next_id)
    nodes.extend(nav_nodes)

    return nodes


def _make_agent_action(app: str, function: str, **arg_slots: Any) -> AgentAction:
    return AgentAction(app=app, function=function, arg_slots=dict(arg_slots))


def _make_check(
    check_type: str,
    field: str = "",
    expected: Any = None,
    weight: str = "hard",
    tolerance: dict[str, Any] | None = None,
) -> CheckItem:
    return CheckItem(
        check_type=check_type,
        field=field,
        expected=expected,
        weight=weight,
        tolerance=tolerance or {},
    )
