"""Constraint builder — LLM generates itinerary scenario + structured GT."""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlparse

from task_module.task_types.itinerary.prompts import parse_llm_response
from task_module.task_types.itinerary.natural_prompt import (
    SYSTEM_ITINERARY_NATURAL, build_natural_user_input,
)
from shared.llm_client import api_connection
from shared.external_usage import record_external_usage
from task_module.task_types.itinerary.call_audit import AttemptAudit, AuditWriteError


def build_scenario(
    character: dict,
    hotel: dict,
    menus: list[dict],
    route_data: dict[str, dict],
    num_days: int,
    scenario: dict[str, Any] | None = None,
    public_requirements: list[dict[str, Any]] | None = None,
    config_yaml: str = "config.yaml",
    stage_model_key: str = "scenario_patch",
    audit: AttemptAudit | None = None,
    role_attempt: int = 1,
) -> tuple[dict | None, dict]:
    """Call an LLM to phrase a scenario; private GT is never requested.

    Returns (result_dict, token_usage).
    token_usage: {"prompt": N, "completion": N, "total": N, "duration_s": F}
    """
    requirements = list(public_requirements or [])
    if any(
        constraint.get("type") == "price_level"
        for requirement in requirements
        for constraint in requirement.get("constraints", [])
    ):
        raise ValueError(
            "itinerary public requirements cannot use categorical price_level as a hard constraint"
        )
    user_prompt_text = build_natural_user_input(
        character, hotel, menus, num_days, scenario or {}, requirements,
    )

    from shared.config_loader import PipelineConfig
    config = PipelineConfig(config_yaml)
    ep = config.model_endpoint(config.stage_model(stage_model_key))

    base_url = ep.get("base_url", "").rstrip("/")
    api_key = ep.get("api_key", "")
    model = ep.get("model", "gpt-5.5")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_ITINERARY_NATURAL},
            {"role": "user", "content": user_prompt_text},
        ],
        "temperature": 0.8,
        "max_completion_tokens": 4096,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    conn, path = api_connection(base_url, "chat/completions", timeout=180)
    started_at = time.monotonic()
    call = audit.start_call('writer', model, config.stage_model(stage_model_key),
                            payload['messages'], {k:v for k,v in payload.items() if k not in {'model','messages'}},
                            secret=api_key, role_attempt=role_attempt) if audit else None
    exchange: dict[str, Any] = {}
    usage_recorded = False
    phase = 'transport'
    token_usage = {"prompt": 0, "completion": 0, "total": 0, "duration_s": 0}
    try:
        conn.request("POST", path, body, {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        })
        t0 = __import__("time").time()
        resp = conn.getresponse()
        exchange['http_status'] = resp.status
        raw_body = resp.read()
        exchange['raw_body'] = raw_body.decode('utf-8', errors='replace')
        if call:
            call.record.update(exchange)
            call.checkpoint()
        phase = 'http_response_parse'
        data = json.loads(raw_body.decode("utf-8"))
        if call:
            call.received(data, exchange)
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
            endpoint_name=config.stage_model(stage_model_key), http_status=resp.status,
        )
        usage_recorded = True

        if resp.status >= 400:
            if call:
                call.finish('failed', failure_phase='http', error_class='HTTPError',
                            error_message=f'HTTP {resp.status}')
            print(f"  [ConstraintBuilder] LLM error {resp.status}: {json.dumps(data)[:200]}")
            return None, token_usage

        choices = data.get("choices", [])
        if not choices:
            if call:
                call.finish('rejected', validation_errors=['choices is empty'])
            return None, token_usage

        text = choices[0].get("message", {}).get("content", "")
        if not text:
            if call:
                call.finish('rejected', validation_errors=['assistant content is empty'])
            return None, token_usage

        validation_errors: list[str] = []
        validation_warnings: list[str] = []
        phase = 'output_validation'
        result = _validate_writer_result(
            parse_llm_response(text), requirements, scenario or {}, menus,
            error_out=validation_errors, warning_out=validation_warnings,
        )
        if result is not None:
            result["_writer_messages"] = [
                {"role": "system", "content": SYSTEM_ITINERARY_NATURAL},
                {"role": "user", "content": user_prompt_text},
                {"role": "assistant", "content": text},
            ]
        elif validation_errors:
            print(f"  [ConstraintBuilder] Writer rejected: {validation_errors[0]}")
            print(f"  [ConstraintBuilder] Rejected assistant output: {text}")
        if call:
            call.finish('completed' if result is not None else 'rejected',
                        validation_errors=validation_errors, validation_warnings=validation_warnings)
        return result, token_usage
    except AuditWriteError:
        raise
    except BaseException as exc:
        if call:
            call.fail(exc, phase, exchange)
        if not usage_recorded:
            record_external_usage(
                provider="llm", operation="chat_completions", status="failed" if isinstance(exc, Exception) else 'interrupted',
                elapsed_s=time.monotonic() - started_at, usage={"request_units": 1},
                model=model, endpoint_name=config.stage_model(stage_model_key), error_class=type(exc).__name__,
                http_status=exchange.get('http_status'),
            )
        if not isinstance(exc, Exception):
            raise
        print(f"  [ConstraintBuilder] Exception: {exc}")
        token_usage['duration_s'] = round(time.monotonic() - started_at, 2)
        return None, token_usage
    finally:
        conn.close()


def _validate_writer_result(
    result: dict[str, Any] | None,
    requirements: list[dict[str, Any]],
    scenario: dict[str, Any],
    menus: list[dict[str, Any]],
    *, error_out: list[str] | None = None, warning_out: list[str] | None = None,
) -> dict[str, Any] | None:
    def reject(reason: str) -> None:
        if error_out is not None:
            error_out.append(reason)
        return None

    if not isinstance(result, dict) or set(result) != {"task"}:
        return reject("response is not the exact task object")
    prompt = str(result.get("task") or "").strip()
    if not prompt:
        return reject("task is empty")
    start_weekday = str((scenario.get("construction_dag") or {}).get("start_weekday") or "")
    weekday_zh = {
        "Monday": "周一", "Tuesday": "周二", "Wednesday": "周三", "Thursday": "周四",
        "Friday": "周五", "Saturday": "周六", "Sunday": "周日",
    }.get(start_weekday, "")
    weekday_forms = [start_weekday, weekday_zh]
    if weekday_zh:
        suffix = weekday_zh[-1]
        weekday_forms.extend(["星期" + suffix, "礼拜" + suffix])
        if suffix == "日":
            weekday_forms.extend(["周天", "星期天", "礼拜天"])
    if start_weekday and not any(value and value.casefold() in prompt.casefold() for value in weekday_forms):
        if warning_out is not None:
            warning_out.append(f"start weekday {start_weekday!r} not recognized in user_prompt; review only")

    fixed_names: set[str] = set()
    fixed_times: set[str] = set()
    for menu in menus:
        for event in menu.get("fixed_events", []):
            fixed_names.add(str(event.get("place_name") or "").strip().casefold())
            fixed_times.update({
                str(event.get("earliest") or "").strip(),
                str(event.get("latest") or "").strip(),
            })
    for name in fixed_names:
        if name and name not in prompt.casefold():
            return reject(f"fixed event place {name!r} is missing from task")
    for value in fixed_times:
        if value and not _contains_time(prompt, value):
            return reject(f"fixed event time {value!r} is missing from task")
    private_names: set[str] = set()
    for day_payload in (scenario.get("construction_dag") or {}).get("days", []):
        for node in day_payload.get("timeline", []):
            if node.get("block_kind") != "poi_activity":
                continue
            private_names.add(str(node.get("place_name") or "").strip().casefold())
    prompt_folded = prompt.casefold()
    if any(name and name not in fixed_names and name in prompt_folded for name in private_names):
        return reject("user_prompt exposes an ordinary private GT place name")
    internal_phrases = ("六个日间时段", "时间边界", "相邻时段", "dag", "benchmark")
    if any(phrase in prompt_folded for phrase in internal_phrases):
        return reject("user_prompt exposes an internal construction term")
    return {"user_prompt": prompt}


def _contains_time(text: str, hhmm: str) -> bool:
    """Accept equivalent English/Chinese 12-hour and 24-hour time forms."""
    try:
        hour_text, minute_text = hhmm.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, TypeError, ValueError):
        return hhmm in text
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return False
    # Consume the meridiem with the clock token so 10:00 PM cannot match 10:00.
    clock_pattern = (
        r"(?<![A-Za-z0-9_:：])(?P<hour>[0-9]{1,2})"
        r"(?:[:：](?P<minute>[0-9]{2}))?"
        r"\s*(?P<period>[ap]\s*\.?\s*m\.?)?(?![A-Za-z0-9_:：])"
    )
    for match in re.finditer(clock_pattern, text, flags=re.IGNORECASE):
        parsed_hour = int(match['hour'])
        parsed_minute = int(match['minute'] or 0)
        period_token = match['period']
        if period_token:
            if not 1 <= parsed_hour <= 12:
                continue
            parsed_hour = parsed_hour % 12 + (12 if period_token.lower().startswith('p') else 0)
        elif match['minute'] is None:
            continue
        if (parsed_hour, parsed_minute) == (hour, minute):
            return True
    period = "下午" if 12 <= hour < 18 else "晚上" if hour >= 18 else "上午"
    display_hour = hour - 12 if hour > 12 else hour
    minute_patterns = (
        (rf"{display_hour}\s*[点時时]", rf"{display_hour}:00", rf"{display_hour}：00")
        if minute == 0
        else (rf"{display_hour}:{minute:02d}", rf"{display_hour}：{minute:02d}", rf"{display_hour}\s*点\s*{minute}\s*分")
    )
    return any(re.search(period + r"\s*" + pattern, text) for pattern in minute_patterns)
