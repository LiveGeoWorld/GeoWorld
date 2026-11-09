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
    char_name = char_names_list[0] if char_names_list else "the user"
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

_LLM_SYSTEM = """Generate a natural English description of a route planning task. Given structured constraints, write a task instruction asking an agent to determine whether the trip is feasible.

Requirements:
1. Include all constraints.
2. Use natural, fluent language, like a travel question from a real user, rather than mechanically listing fields.
3. Explicitly ask whether the trip is feasible and request an explanation if it is not.
4. Output only the task description, without introductory phrases such as "Here is the generated task"."""


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
        constraint_lines.append(f"  Time limit: complete the trip within {time_min} minutes")
    if stop_min and has_waypoint:
        constraint_lines.append(f"  Stop duration at the waypoint(s): {stop_min} minutes")
    if forbidden:
        mode_en = {"DRIVE": "driving/taxis", "TRANSIT": "public transit", "WALK": "walking"}
        names = [mode_en.get(m, m) for m in forbidden]
        constraint_lines.append(f"  Prohibited travel modes: {', '.join(names)}")
    if max_walk:
        constraint_lines.append(f"  Walking distance limit: no more than {max_walk} meters")

    route_desc = f"Travel from {origin} to {dest}"
    if has_waypoint and waypoint:
        route_desc = f"Start from {origin}, first visit {waypoint} (staying for {stop_min} minutes), then continue to {dest}"

    infeas_hint = "This task is actually infeasible (the minimum travel time exceeds the limit). Phrase the task as a question about \"whether it is feasible\", prompting the agent to discover the infeasibility." if is_infeasible else ""

    user = f"""Character: {char_name}
Route: {route_desc}
Constraints:
{chr(10).join(constraint_lines) if constraint_lines else '  (no additional constraints)'}
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
        mode_kw = {"DRIVE": ["driving", "drive", "taxi", "cab", "DRIVE"],
                   "TRANSIT": ["public transit", "subway", "bus", "TRANSIT"],
                   "WALK": ["walking", "walk", "WALK"]}
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
    mode_names = {"DRIVE": "driving/taxis", "TRANSIT": "public transit", "WALK": "walking"}
    names = [mode_names.get(m, m) for m in forbidden]
    templates = [
        f"cannot use {', '.join(names)}",
        f"travel by {', '.join(names)} is prohibited",
        f"using {', '.join(names)} is not allowed",
        f"must not use {', '.join(names)}",
    ]
    return rng.choice(templates)


def _walk_text(max_walk_m: int | None, rng: random.Random) -> str:
    if not max_walk_m:
        return ""
    tmpl = [
        f"walking distance must not exceed {max_walk_m} meters",
        f"would like to walk no more than {max_walk_m} meters",
        f"total walking distance for the trip should not exceed {max_walk_m} meters",
        f"must not walk more than {max_walk_m} meters",
    ]
    return rng.choice(tmpl)


def _purpose_text(rng: random.Random) -> str:
    return rng.choice([
        "pick up a document", "pick someone up", "run an errand", "pick something up",
        "collect a parcel", "attend a meeting", "meet someone", "pick up some materials",
    ])


def _template_a(char_name, origin, dest, has_wp, wp, stop_min, time_min,
                forbidden, max_walk, infeasible, rng):
    parts = [f"{char_name} needs to travel from {origin} to {dest}"]
    if has_wp and wp:
        parts.append(f"First visit {wp} to {_purpose_text(rng)}, stay for {stop_min} minutes, and then continue to {dest}")
    parts.append(f"Arrival is required within {time_min} minutes")
    extra = []
    fb = _forbidden_text(forbidden, rng)
    if fb:
        extra.append(fb)
    wlk = _walk_text(max_walk, rng)
    if wlk:
        extra.append(wlk)
    if extra:
        parts.append(f"({', '.join(extra)})")
    parts.append("Please look up routes and determine whether the trip is feasible")
    if infeasible:
        parts.append("If it is infeasible, please explain why")
    return ". ".join(parts) + "."


def _template_b(char_name, origin, dest, has_wp, wp, stop_min, time_min,
                forbidden, max_walk, infeasible, rng):
    if has_wp and wp:
        parts = [f"{char_name} wants to travel from {origin} to {dest}, first stopping at {wp} to {_purpose_text(rng)} (about {stop_min} minutes)"]
    else:
        parts = [f"{char_name} wants to travel from {origin} to {dest}"]
    parts.append(f" and needs to arrive within {time_min} minutes")
    extra = []
    fb = _forbidden_text(forbidden, rng)
    if fb:
        extra.append(fb)
    wlk = _walk_text(max_walk, rng)
    if wlk:
        extra.append(wlk)
    if extra:
        parts.append(f", {', '.join(extra)}")
    if infeasible:
        parts.append(". Please provide a feasible route; if the trip is infeasible, explain the minimum time required")
    else:
        parts.append(". Please provide a feasible route")
    return "".join(parts) + "."


def _template_c(char_name, origin, dest, has_wp, wp, stop_min, time_min,
                forbidden, max_walk, infeasible, rng):
    parts = [f"Please help {char_name} check whether this trip can be completed in time: "]
    if has_wp and wp:
        parts.append(f"start from {origin}, first visit {wp} to {_purpose_text(rng)} and stay for {stop_min} minutes, then head to {dest}")
    else:
        parts.append(f"start from {origin} and travel to {dest}")
    parts.append(f"; the trip needs to be completed within {time_min} minutes")
    extra = []
    fb = _forbidden_text(forbidden, rng)
    if fb:
        extra.append(fb)
    wlk = _walk_text(max_walk, rng)
    if wlk:
        extra.append(wlk)
    if extra:
        parts.append(f", {', '.join(extra)}")
    if infeasible:
        parts.append(". If it is infeasible, explain the minimum time required and which constraints are violated")
    else:
        parts.append(". If it is feasible, please provide the route for each leg")
    return "".join(parts) + "."


def _template_d(char_name, origin, dest, has_wp, wp, stop_min, time_min,
                forbidden, max_walk, infeasible, rng):
    if has_wp and wp:
        inner = f"Can I start from {origin}, first visit {wp} to {_purpose_text(rng)} (about {stop_min} minutes), and then, within {time_min} minutes, reach {dest}"
    else:
        inner = f"Can I start from {origin} and, within {time_min} minutes, reach {dest}"
    extra = []
    fb = _forbidden_text(forbidden, rng)
    if fb:
        extra.append(fb)
    wlk = _walk_text(max_walk, rng)
    if wlk:
        extra.append(wlk)
    if extra:
        inner += f", {', '.join(extra)}"
    return f"{char_name} asks: \"{inner}?\" Please help them look up routes and make a plan."
