"""Service operation judge nodes — derived from app registry + service affordances.

Now uses app/ for app+function definitions instead of hardcoded strings.
"""

from __future__ import annotations

from typing import Any

from app import get_app, get_function

from task_module.task_schema import JudgeNode

from . import _make_agent_action, _make_check


def generate_service_judge_nodes(
    world: dict[str, Any],
    city_id: str = "",
    id_gen: callable = None,
) -> list[JudgeNode]:
    services = world.get("services", [])
    if not services:
        return []

    if id_gen is None:
        _idx = [0]

        def id_gen():
            _idx[0] += 1
            return f"jcp_{city_id}_{_idx[0]:04d}" if city_id else f"jcp_{_idx[0]:04d}"

    nodes: list[JudgeNode] = []
    seen_types: set[str] = set()

    app = get_app("LocalServiceApp")

    for svc in services:
        service_type = svc.get("service_type", "")
        affordances = svc.get("affordances", [])
        if not affordances:
            continue

        # Only generate once per service type (not per service instance)
        if service_type and service_type in seen_types:
            continue
        seen_types.add(service_type)

        # Phase 1: Search node
        if any(a in affordances for a in ("searchable",)):
            try:
                func = get_function("LocalServiceApp", "search_services")
                checks = _build_checks(func)
                search_node = JudgeNode(
                    node_id=id_gen(),
                    category="service_operation",
                    agent_action=_make_agent_action(
                        app.name, func.name,
                        service_type=service_type,
                        district_id="<DISTRICT>",
                        filters={},
                    ),
                    checks=checks,
                    depends_on=[],
                    description_zh=f"{func.description_zh}（{_service_type_label(service_type)}）",
                    source="rule_derived",
                )
                nodes.append(search_node)
                search_id = search_node.node_id
            except KeyError:
                search_id = None
        else:
            search_id = None

        # Phase 2: Reserve/Book node
        if any(a in affordances for a in ("reservable", "bookable")):
            try:
                func_name = "make_reservation" if "reservable" in affordances else "book_service"
                func = get_function("LocalServiceApp", func_name)
                checks = _build_checks(func)
                reserve_node = JudgeNode(
                    node_id=id_gen(),
                    category="service_operation",
                    agent_action=_make_agent_action(
                        app.name, func.name,
                        service_id="<SERVICE>",
                    ),
                    checks=checks if checks else [
                        _make_check("action_executed"),
                        _make_check("field_equals", "response.status", expected="confirmed"),
                    ],
                    depends_on=[search_id] if search_id else [],
                    description_zh=f"{func.description_zh}（{_service_type_label(service_type)}）",
                    source="rule_derived",
                )
                nodes.append(reserve_node)
            except KeyError:
                pass

        # Phase 3: Cancel node
        if "cancelable" in affordances:
            try:
                func = get_function("LocalServiceApp", "cancel_reservation")
                checks = _build_checks(func)
                cancel_node = JudgeNode(
                    node_id=id_gen(),
                    category="service_operation",
                    agent_action=_make_agent_action(
                        app.name, func.name,
                        service_id="<SERVICE>",
                    ),
                    checks=checks if checks else [
                        _make_check("action_executed"),
                        _make_check("field_equals", "response.status", expected="cancelled"),
                    ],
                    depends_on=[],
                    description_zh=func.description_zh,
                    source="rule_derived",
                )
                nodes.append(cancel_node)
            except KeyError:
                pass

    return nodes


def _build_checks(func: Any) -> list:
    """Convert AppCheck list to JudgeNode check list."""
    checks = []
    for c in func.hard_checks:
        checks.append(_make_check(
            c.check_type, c.field,
            expected=c.expected,
            weight="hard",
            tolerance={c.tolerance: True} if c.tolerance else None,
        ))
    for c in func.soft_checks:
        checks.append(_make_check(
            c.check_type, c.field,
            expected=c.expected,
            weight="soft",
            tolerance={c.tolerance: True} if c.tolerance else None,
        ))
    return checks


def _service_type_label(stype: str) -> str:
    labels: dict[str, str] = {
        "restaurant_reservation": "餐厅",
        "table_booking": "餐厅",
        "hotel_booking": "酒店",
        "attraction_visit": "景点",
        "cafe_reservation": "咖啡厅",
    }
    return labels.get(stype, stype)
