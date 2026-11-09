"""Constraint builder — LLM generates multi-entity scenario + structured GT."""

from __future__ import annotations

import json
import time
from typing import Any

from task_module.task_types.multi_entity.prompts import parse_llm_response
from task_module.task_types.multi_entity.natural_prompt import (
    SYSTEM_MULTI_ENTITY_NATURAL, build_natural_user_input,
)
from shared.llm_client import api_connection
from shared.external_usage import record_external_usage


def build_scenario(
    participants: list[dict],
    restaurants: list[dict],
    route_data: dict[str, dict],
    scenario: dict,
    config_yaml: str = "config.yaml",
    stage_model_key: str = "scenario_patch",
) -> tuple[dict | None, dict]:
    """Call LLM to phrase a scenario; private GT is never requested.

    Returns (result_dict, token_usage).
    token_usage: {"prompt": N, "completion": N, "total": N, "duration_s": F}
    """
    user_prompt_text = build_natural_user_input(participants, restaurants, scenario, route_data)

    from shared.config_loader import PipelineConfig
    config = PipelineConfig(config_yaml)
    ep = config.model_endpoint(config.stage_model(stage_model_key))

    base_url = ep.get("base_url", "").rstrip("/")
    api_key = ep.get("api_key", "")
    model = ep.get("model", "gpt-5.5")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_MULTI_ENTITY_NATURAL},
            {"role": "user", "content": user_prompt_text},
        ],
        "temperature": 0.8,
        "max_completion_tokens": 2048,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    conn, path = api_connection(base_url, "chat/completions", timeout=120)
    started_at = time.monotonic()
    try:
        conn.request("POST", path, body, {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        t0 = __import__("time").time()
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        dur = round(__import__("time").time() - t0, 2)

        usage = data.get("usage", {})
        token_usage = {
            "prompt": usage.get("prompt_tokens", 0),
            "completion": usage.get("completion_tokens", 0),
            "total": usage.get("total_tokens", 0),
            "duration_s": dur,
        }

        record_external_usage(
            provider="llm", operation="chat_completions",
            status="success" if resp.status < 400 else "failed",
            elapsed_s=time.monotonic() - started_at,
            usage={"request_units": 1, **token_usage}, model=model,
        )

        if resp.status >= 400:
            print(f"  [ConstraintBuilder] LLM error {resp.status}: {json.dumps(data)[:200]}")
            return None, token_usage

        choices = data.get("choices", [])
        if not choices:
            return None, token_usage

        text = choices[0].get("message", {}).get("content", "")
        if not text:
            return None, token_usage

        result = parse_llm_response(text)
        return result, token_usage
    except Exception as exc:
        record_external_usage(
            provider="llm", operation="chat_completions", status="failed",
            elapsed_s=time.monotonic() - started_at, usage={"request_units": 1},
            model=model, error_class=type(exc).__name__,
        )
        print(f"  [ConstraintBuilder] Exception: {exc}")
        return None, {"prompt": 0, "completion": 0, "total": 0, "duration_s": 0}
    finally:
        conn.close()
