"""Navigation judge nodes — derived from spatial connections + app registry."""

from __future__ import annotations

from typing import Any

from app import get_app, get_function

from task_module.task_schema import JudgeNode

from . import _make_agent_action, _make_check


def generate_nav_judge_nodes(
    world: dict[str, Any],
    city_id: str = "",
    id_gen: callable = None,
) -> list[JudgeNode]:
    districts = world.get("districts", [])
    corridors = world.get("corridors", [])

    if not districts:
        return []

    if id_gen is None:
        _idx = [0]

        def id_gen():
            _idx[0] += 1
            return f"jcp_{city_id}_{_idx[0]:04d}" if city_id else f"jcp_{_idx[0]:04d}"

    nodes: list[JudgeNode] = []
    map_app = get_app("MapApp")

    # Transit check
    if len(districts) >= 2:
        transit_func = map_app.get_function("check_transit")
        checks = [_make_check("action_executed")]
        if transit_func:
            for c in transit_func.hard_checks:
                checks.append(_make_check(c.check_type, c.field, expected=c.expected, weight="hard"))
        nodes.append(JudgeNode(
            node_id=id_gen(),
            category="navigation",
            agent_action=_make_agent_action(
                map_app.name, transit_func.name if transit_func else "check_transit",
                district_id="<DISTRICT>",
            ),
            checks=checks if checks else [
                _make_check("action_executed"),
                _make_check("field_not_empty", "response.status_list"),
            ],
            depends_on=[],
            description_zh="Agent应检查区域交通状态",
            source="rule_derived",
        ))

    # Route planning
    if corridors:
        route_func = map_app.get_function("plan_route")
        checks = [_make_check("action_executed")]
        if route_func:
            for c in route_func.hard_checks:
                checks.append(_make_check(c.check_type, c.field, expected=c.expected, weight="hard"))
        nodes.append(JudgeNode(
            node_id=id_gen(),
            category="navigation",
            agent_action=_make_agent_action(
                map_app.name, route_func.name if route_func else "plan_route",
                origin_place_id="<FROM_PLACE>",
                dest_place_id="<TO_PLACE>",
                mode="<MODE>",
            ),
            checks=checks if checks else [
                _make_check("action_executed"),
                _make_check("field_not_empty", "response.routes"),
            ],
            depends_on=[],
            description_zh="Agent应规划两地之间的最优路线",
            source="rule_derived",
        ))

    return nodes
