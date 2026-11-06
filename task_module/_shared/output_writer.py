"""Output writer — write individual task folders and manifest."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from shared.io import write_json


def write_task_folder(
    task_dir: Path,
    task: Any,
    env_events: list[Any],
    judge_stages: list[Any],
    world_slice_summary: Any,
) -> None:
    """Write all files for a single task into its folder."""
    if task_dir.exists():
        raise FileExistsError(
            f"Task folder already exists: {task_dir}. "
            f"This should not happen with auto-increment numbering."
        )
    task_dir.mkdir(parents=True, exist_ok=False)
    try:
        # task.json
        write_json(task_dir / "task.json", {
            "task_id": _attr(task, "task_id", ""),
            "task_type": _attr(task, "task_type", ""),
            "scenario_type": _attr(task, "task_type", ""),  # backward compat
            "difficulty": _attr(task, "difficulty", ""),
            "city_id": _attr(task, "city_id", ""),
            "variant": _attr(task, "variant", ""),
            "generated_at": _attr(task, "generated_at", ""),
            "user_prompt": _attr(task, "user_prompt", ""),
            "summary": _attr(task, "summary", {}),
        })

        # task_sandbox.json — minimal stub for judge-sequence-based types
        write_json(task_dir / "task_sandbox.json", {
            "task_type": _attr(task, "task_type", ""),
            "task_id": _attr(task, "task_id", ""),
            "evaluation_mode": "judge_sequence",
        })

        # env_events.json (with concretized descriptions)
        env_data = []
        for evt in env_events:
            concrete_desc = _concretize_description(
                _attr(evt, "description_zh", ""),
                _attr(evt, "scope_type", ""),
                _attr(evt, "scope_id", "") or "",
                world_slice_summary,
            )
            effects = _attr(evt, "effects", [])
            env_data.append({
                "node_id": _attr(evt, "node_id", ""),
                "category": _attr(evt, "category", ""),
                "scope_type": _attr(evt, "scope_type", ""),
                "scope_id": _attr(evt, "scope_id", ""),
                "trigger_probability": _attr(evt, "trigger_probability", 1.0),
                "description_zh": concrete_desc,
                "visibility": _attr(evt, "visibility", "visible"),
                "severity": _attr(evt, "severity", "medium"),
                "effects_summary": [
                    f"{_attr(ef, 'target_type', '?')}.{_attr(ef, 'field_path', '?')} {_attr(ef, 'op', '?')}={_attr(ef, 'value', '?')}"
                    for ef in effects
                ],
            })
        write_json(task_dir / "env_events.json", {"env_events": env_data})

        # judge_sequence.json
        stages_data = []
        for stage in judge_stages:
            stage_nodes = []
            for node in (_attr(stage, "nodes", []) or []):
                action = _attr(node, "agent_action", None)
                checks = _attr(node, "checks", []) or []
                stage_nodes.append({
                    "node_id": _attr(node, "node_id", ""),
                    "category": _attr(node, "category", ""),
                    "agent_action": f"{_attr(action, 'app', '')}.{_attr(action, 'function', '')}" if action else "",
                    "arg_slots": _attr(action, "arg_slots", {}) if action else {},
                    "description_zh": _attr(node, "description_zh", ""),
                    "hard_checks": [
                        _attr(c, "field", "") or _attr(c, "check_type", "")
                        for c in checks if _attr(c, "weight", "hard") == "hard"
                    ],
                    "soft_checks": [
                        _attr(c, "field", "") or _attr(c, "check_type", "")
                        for c in checks if _attr(c, "weight", "soft") == "soft"
                    ],
                    "depends_on": _attr(node, "depends_on", []),
                })
            stages_data.append({
                "stage_id": _attr(stage, "stage_id", ""),
                "description": _attr(stage, "description", ""),
                "depends_on": _attr(stage, "depends_on", []),
                "nodes": stage_nodes,
            })
        write_json(task_dir / "judge_sequence.json", {"stages": stages_data})

        # world_slice.json
        if hasattr(world_slice_summary, "to_dict"):
            write_json(task_dir / "world_slice.json", world_slice_summary.to_dict())
        else:
            write_json(task_dir / "world_slice.json", world_slice_summary)
    except Exception:
        # A partially materialized task must never occupy an index in a later
        # append run or be mistaken for a formal task candidate.
        shutil.rmtree(task_dir, ignore_errors=True)
        raise


# ============================================================
# Description Concretization (moved from composer.py)
# ============================================================

def _concretize_description(desc: str, scope_type: str, scope_id: str, ws: Any) -> str:
    name = _resolve_scope_name(scope_type, scope_id, ws)
    if not name:
        name = _first_name_of_type(scope_type, ws)
    if not name:
        return desc

    generics: dict[str, list[tuple[str, str]]] = {
        "actor": [("某参与者", name), ("该参与者", name)],
        "district": [("该区域", name), ("某区域", name), ("区域", name)],
        "place": [("该餐厅", name), ("某餐厅", name), ("该店", name), ("某店", name), ("餐厅", name)],
        "service": [("某服务", name)],
        "corridor": [("某地铁线路", name + "线"), ("某线路", name + "线")],
    }

    for old, new in generics.get(scope_type, []):
        if old in desc:
            return desc.replace(old, new)
    return desc


def _resolve_scope_name(scope_type: str, scope_id: str, ws: Any) -> str:
    if not scope_id:
        return ""
    type_map = {
        "actor": ("characters", "character_id"),
        "district": ("districts", "district_id"),
        "place": ("places", "place_id"),
        "corridor": ("corridors", "corridor_id"),
    }
    entry = type_map.get(scope_type)
    if not entry:
        return ""
    collection_key, id_key = entry
    for item in _attr(ws, collection_key, []) or []:
        if isinstance(item, dict) and item.get(id_key) == scope_id:
            return item.get("name", "")
    return ""


def _first_name_of_type(scope_type: str, ws: Any) -> str:
    type_map = {
        "actor": "characters",
        "district": "districts",
        "place": "places",
        "corridor": "corridors",
    }
    key = type_map.get(scope_type, "")
    items = _attr(ws, key, []) or []
    return items[0].get("name", "") if items else ""


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if hasattr(obj, name):
        val = getattr(obj, name)
        return val() if callable(val) and not isinstance(val, (dict, list)) else val
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default
