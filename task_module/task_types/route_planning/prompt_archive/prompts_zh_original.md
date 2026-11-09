# Route Planning 原中文版本（2026-10-13 翻译前备份）

仅供对照；正式入口使用 ../prompts.py。以下完整保留翻译前文件的内容。

```python
"""User prompt templates for route planning tasks.

Primary: LLM-generated natural language prompts from structured constraints.
Fallback: 4 template families (A/B/C/D) if LLM fails or validation rejects.

Architecture:
    derive_constraints_for_stops() → structured dict
        ↓
    build_llm_prompt(constraints) → LLM API → natural user_prompt
        ↓
    validate_prompt(text, constraints) → pass? → write to task.json
        ↓ fail → template fallback
"""

from __future__ import annotations

import json
import random
from typing import Any


def generate_user_prompt(
    origin_name: str,
    dest_name: str,
    has_waypoint: bool,
    waypoint_name: str = "",
    character_names: list[str] | None = None,
    constraint_data: dict[str, Any] | None = None,
    rng: random.Random | None = None,
) -> str:
    """Generate a constraint-based route planning prompt.

    Tries LLM generation first; falls back to template if LLM fails
    or the output fails constraint validation.
    """
    rng = rng or random.Random()
    char_names_list = character_names or []
    char_name = char_names_list[0] if char_names_list else "用户"
    cd = constraint_data or {}
    constraints = cd.get("constraints", cd)
    is_infeasible = cd.get("expected_feasible") is False

    # Try LLM generation
    llm_prompt = _try_llm_generate(
        char_name, origin_name, dest_name, has_waypoint, waypoint_name,
        constraints, is_infeasible,
    )
    if llm_prompt:
        return llm_prompt

    # Fallback to template
    return _template_generate(
        char_name, origin_name, dest_name, has_waypoint, waypoint_name,
        constraints, is_infeasible, rng,
    )


# ── LLM Generation ──

_LLM_SYSTEM = """生成一个路线规划任务的自然中文描述。给定结构化约束，生成一个让智能体判断出行是否可行的任务指令。

要求：
1. 必须包含所有约束条件
2. 语言自然、流畅，像一个真实用户提的出行问题，不要机械罗列字段
3. 明确要求判断是否可行；若不可行请说明原因
4. 直接输出任务描述文本，不要加"以下是生成的任务"等引言"""


def _try_llm_generate(
    char_name: str, origin: str, dest: str,
    has_waypoint: bool, waypoint: str,
    constraints: dict, is_infeasible: bool,
) -> str | None:
    """Call LLM to generate a natural user_prompt. Returns None on failure."""
    time_min = constraints.get("time_budget_min", 0)
    stop_min = constraints.get("stop_duration_min", 0)
    forbidden = constraints.get("forbidden_modes", [])
    max_walk = constraints.get("max_walking_distance_m")

    constraint_lines = []
    if time_min:
        constraint_lines.append(f"  时间限制: {time_min}分钟内完成")
    if stop_min and has_waypoint:
        constraint_lines.append(f"  途经点停留: {stop_min}分钟")
    if forbidden:
        mode_cn = {"DRIVE": "驾车/出租车", "TRANSIT": "公共交通", "WALK": "步行"}
        names = [mode_cn.get(m, m) for m in forbidden]
        constraint_lines.append(f"  禁止交通方式: {'、'.join(names)}")
    if max_walk:
        constraint_lines.append(f"  步行距离限制: 不超过{max_walk}米")

    route_desc = f"从{origin}前往{dest}"
    if has_waypoint and waypoint:
        route_desc = f"从{origin}先去{waypoint}（停留{stop_min}分钟），再前往{dest}"

    infeas_hint = "该任务实际不可行（最短耗时超过限制）。请让任务询问\"是否可行\"，引导智能体发现不可行。" if is_infeasible else ""

    user = f"""角色: {char_name}
路线: {route_desc}
约束:
{chr(10).join(constraint_lines) if constraint_lines else '  (无额外约束)'}
{infeas_hint}"""

    try:
        text = _call_llm(_LLM_SYSTEM, user)
        if not text:
            return None
        text = text.strip().strip("'").strip('"')
        if not validate_prompt(text, constraints, char_name, origin, dest):
            return None
        return text
    except Exception:
        return None


def _call_llm(system: str, user: str) -> str | None:
    """Call LLM endpoint via OpenAI-compatible API."""
    from shared.llm_client import api_connection
    from shared.config_loader import PipelineConfig

    config = PipelineConfig()
    endpoint_name = config.stage_model("task_design")
    ep = config.model_endpoint(endpoint_name)

    base_url = ep.get("base_url", "").rstrip("/")
    api_key = ep.get("api_key", "")
    model = ep.get("model", "")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.8,
        "max_tokens": 512,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    conn, path = api_connection(base_url, "chat/completions", timeout=30)
    try:
        conn.request("POST", path, body, {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        if resp.status >= 400:
            return None
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "")
        return None
    except Exception:
        return None
    finally:
        conn.close()


def validate_prompt(
    text: str, constraints: dict, char_name: str,
    origin: str, dest: str,
) -> bool:
    """Check that all constraint values appear in the generated prompt."""
    time_min = constraints.get("time_budget_min", 0)
    forbidden = constraints.get("forbidden_modes", [])
    max_walk = constraints.get("max_walking_distance_m")
    stop_min = constraints.get("stop_duration_min", 0)

    if time_min and str(time_min) not in text:
        return False
    if forbidden:
        mode_kw = {"DRIVE": ["驾车", "开车", "出租车", "打车", "DRIVE"],
                   "TRANSIT": ["公共交通", "地铁", "公交", "TRANSIT"],
                   "WALK": ["步行", "走路", "WALK"]}
        for m in forbidden:
            if not any(kw in text for kw in mode_kw.get(m, [m])):
                return False
    if max_walk and str(max_walk) not in text:
        return False
    if char_name not in text:
        return False
    if origin not in text:
        return False
    if dest not in text:
        return False
    return True


# ── Template fallback ──

def _template_generate(
    char_name, origin, dest, has_wp, wp, constraints, infeasible, rng,
) -> str:
    time_min = constraints.get("time_budget_min", 30)
    stop_min = constraints.get("stop_duration_min", 0) if has_wp else 0
    forbidden = constraints.get("forbidden_modes", [])
    max_walk = constraints.get("max_walking_distance_m")

    family = rng.randint(0, 3)
    if family == 0:
        return _template_a(char_name, origin, dest, has_wp, wp, stop_min, time_min,
                           forbidden, max_walk, infeasible, rng)
    elif family == 1:
        return _template_b(char_name, origin, dest, has_wp, wp, stop_min, time_min,
                           forbidden, max_walk, infeasible, rng)
    elif family == 2:
        return _template_c(char_name, origin, dest, has_wp, wp, stop_min, time_min,
                           forbidden, max_walk, infeasible, rng)
    else:
        return _template_d(char_name, origin, dest, has_wp, wp, stop_min, time_min,
                           forbidden, max_walk, infeasible, rng)


def _forbidden_text(forbidden: list[str], rng: random.Random) -> str:
    if not forbidden:
        return ""
    mode_names = {"DRIVE": "驾车/出租车", "TRANSIT": "公共交通", "WALK": "步行"}
    names = [mode_names.get(m, m) for m in forbidden]
    templates = [
        f"不能使用{'、'.join(names)}",
        f"禁止使用{'、'.join(names)}出行",
        f"不允许搭乘{'、'.join(names)}",
        f"不能乘坐{'、'.join(names)}",
    ]
    return rng.choice(templates)


def _walk_text(max_walk_m: int | None, rng: random.Random) -> str:
    if not max_walk_m:
        return ""
    tmpl = [
        f"步行距离不超过{max_walk_m}米",
        f"希望步行不超过{max_walk_m}米",
        f"全程步行不要超过{max_walk_m}米",
        f"走路不能超过{max_walk_m}米",
    ]
    return rng.choice(tmpl)


def _purpose_text(rng: random.Random) -> str:
    return rng.choice([
        "取一份文件", "接一个人", "办一件事", "拿东西",
        "取快递", "开会", "碰面", "取资料",
    ])


def _template_a(char_name, origin, dest, has_wp, wp, stop_min, time_min,
                forbidden, max_walk, infeasible, rng):
    parts = [f"{char_name}需要从{origin}前往{dest}"]
    if has_wp and wp:
        parts.append(f"先去{wp}{_purpose_text(rng)}停留{stop_min}分钟后前往{dest}")
    parts.append(f"要求{time_min}分钟内到达")
    extra = []
    fb = _forbidden_text(forbidden, rng)
    if fb:
        extra.append(fb)
    wlk = _walk_text(max_walk, rng)
    if wlk:
        extra.append(wlk)
    if extra:
        parts.append(f"（{'，'.join(extra)}）")
    parts.append("请查询路线并判断是否可行")
    if infeasible:
        parts.append("如不可行请说明原因")
    return "。".join(parts) + "。"


def _template_b(char_name, origin, dest, has_wp, wp, stop_min, time_min,
                forbidden, max_walk, infeasible, rng):
    if has_wp and wp:
        parts = [f"{char_name}要从{origin}去{dest}，中途需要先到{wp}{_purpose_text(rng)}（约{stop_min}分钟）"]
    else:
        parts = [f"{char_name}要从{origin}前往{dest}"]
    parts.append(f"需要在{time_min}分钟内到达")
    extra = []
    fb = _forbidden_text(forbidden, rng)
    if fb:
        extra.append(fb)
    wlk = _walk_text(max_walk, rng)
    if wlk:
        extra.append(wlk)
    if extra:
        parts.append(f"，{'，'.join(extra)}")
    if infeasible:
        parts.append("。请给出可执行路线，如不可行请说明最短需要多久")
    else:
        parts.append("。请给出可执行路线")
    return "".join(parts) + "。"


def _template_c(char_name, origin, dest, has_wp, wp, stop_min, time_min,
                forbidden, max_walk, infeasible, rng):
    parts = [f"请帮{char_name}确认一个出行安排是否来得及："]
    if has_wp and wp:
        parts.append(f"从{origin}出发，先去{wp}{_purpose_text(rng)}停留{stop_min}分钟，再赶往{dest}")
    else:
        parts.append(f"从{origin}出发前往{dest}")
    parts.append(f"需要在{time_min}分钟内完成")
    extra = []
    fb = _forbidden_text(forbidden, rng)
    if fb:
        extra.append(fb)
    wlk = _walk_text(max_walk, rng)
    if wlk:
        extra.append(wlk)
    if extra:
        parts.append(f"，{'，'.join(extra)}")
    if infeasible:
        parts.append("。如果不可行，请说明最短需要多久以及违反的约束")
    else:
        parts.append("。如果可行请给出分段路线")
    return "".join(parts) + "。"


def _template_d(char_name, origin, dest, has_wp, wp, stop_min, time_min,
                forbidden, max_walk, infeasible, rng):
    if has_wp and wp:
        inner = f"我能不能从{origin}出发先到{wp}{_purpose_text(rng)}（大约{stop_min}分钟），再{time_min}分钟内赶到{dest}"
    else:
        inner = f"我能不能从{origin}出发，在{time_min}分钟内到{dest}"
    extra = []
    fb = _forbidden_text(forbidden, rng)
    if fb:
        extra.append(fb)
    wlk = _walk_text(max_walk, rng)
    if wlk:
        extra.append(wlk)
    if extra:
        inner += f"，{'，'.join(extra)}"
    return f"{char_name}问：\"{inner}？\"。请你帮他查询路线并制定方案。"

```

## generator.py 中的中文拼装（翻译前）

```python
char_names = [c.get("name", "用户") for c in chars]
waypoint_name="、".join(stop.get("name", "") for stop in waypoints)
user_prompt += "\n必经事项：" + "；".join(
    _semantic_stop_text(stop) for stop in waypoints
) + "。这些是需要完成的事务，不是可自由跳过的换乘点。"

def _semantic_stop_text(stop: dict[str, Any]) -> str:
    action_labels = {
        "document_pickup": "取文件",
        "parcel_collection": "取包裹",
        "brief_meeting": "短暂会面",
        "errand": "办理事务",
    }
    action = action_labels.get(stop.get("semantic_action", ""), "办理事务")
    return f"{stop.get('name', '')}（{action}，停留{stop.get('dwell_min', 0)}分钟）"
```
