"""Communication judge nodes — derived from character relationships + app registry.

Uses app/ for app+function definitions instead of hardcoded strings.
"""

from __future__ import annotations

from typing import Any

from app import get_app, get_function

from task_module.task_schema import JudgeNode

from . import _make_agent_action, _make_check


RELATIONSHIP_LABELS: dict[str, str] = {
    "spouse": "配偶", "parent": "父母", "child": "子女",
    "sibling": "兄弟姐妹", "coworker": "同事",
    "friend": "朋友", "classmate": "同学", "neighbor": "邻居",
}


def generate_comm_judge_nodes(
    characters: dict[str, Any],
    city_id: str = "",
    id_gen: callable = None,
) -> list[JudgeNode]:
    chars = characters.get("characters", [])
    relationships = characters.get("relationships", [])

    if not chars or not relationships:
        return []

    if id_gen is None:
        _idx = [0]

        def id_gen():
            _idx[0] += 1
            return f"jcp_{city_id}_{_idx[0]:04d}" if city_id else f"jcp_{_idx[0]:04d}"

    nodes: list[JudgeNode] = []
    seen_rel_types: set[str] = set()

    # Get send_message function from app registry
    msg_app = get_app("MessagingAppV2")
    send_func = msg_app.get_function("send_message")
    ui_app = get_app("UserInterfaceApp")
    notify_func = ui_app.get_function("send_message_to_user")

    char_names: dict[str, str] = {}
    for c in chars:
        char_names[c.get("character_id", "")] = c.get("name", "")

    for rel in relationships:
        rel_type = rel.get("type", "")
        if not rel_type or rel_type in seen_rel_types:
            continue
        seen_rel_types.add(rel_type)

        rel_label = RELATIONSHIP_LABELS.get(rel_type, rel_type)
        related_char_id = rel.get("related_id", "")
        related_name = char_names.get(related_char_id, related_char_id)

        # Build checks from app function spec
        checks = [_make_check("action_executed")]
        if send_func:
            for c in send_func.hard_checks:
                checks.append(_make_check(c.check_type, c.field, expected=c.expected, weight="hard"))
            for c in send_func.soft_checks:
                checks.append(_make_check(c.check_type, c.field, expected=c.expected, weight="soft"))

        if not checks:
            checks = [
                _make_check("action_executed"),
                _make_check("field_equals", "args.recipient_id"),
            ]

        node = JudgeNode(
            node_id=id_gen(),
            category="communication",
            agent_action=_make_agent_action(
                msg_app.name, send_func.name if send_func else "send_message",
                recipient_id="<CHARACTER>",
                content="",
            ),
            checks=checks,
            depends_on=[],
            description_zh=f"Agent应通知{rel_label}{related_name}预订结果",
            source="rule_derived_from_relationship",
        )
        nodes.append(node)

    # Always add a "notify user" node
    notify_checks = [_make_check("action_executed")]
    if notify_func:
        for c in notify_func.hard_checks:
            notify_checks.append(_make_check(c.check_type, c.field, weight="hard"))
        for c in notify_func.soft_checks:
            notify_checks.append(_make_check(c.check_type, c.field, weight="soft"))
    else:
        notify_checks.append(_make_check("field_not_empty", "args.content", weight="soft"))

    notify_user = JudgeNode(
        node_id=id_gen(),
        category="communication",
        agent_action=_make_agent_action(
            ui_app.name, notify_func.name if notify_func else "send_message_to_user",
            content="",
        ),
        checks=notify_checks,
        depends_on=[],
        description_zh="Agent应将最终方案通知用户",
        source="rule_derived",
    )
    nodes.append(notify_user)

    return nodes
