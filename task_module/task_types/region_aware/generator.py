"""Region-Aware contextual-choice generator.

The active Stage-3 path consumes the version-bound Stage-1 rules, unified
Stage-2 assessments, frozen map, characters, and service-detail cache.  The
legacy CulturalKB itinerary implementation remains unreachable compatibility
code below the active return and is not part of formal Region generation.
"""

from __future__ import annotations

import json
import hashlib
import logging
import math
import random
import re
import sys
from itertools import combinations
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.config_loader import PipelineConfig
from shared.io import read_json, write_json
from shared.person_route_identity import person_route_ids, route_endpoint_matches
from shared.maps_client import GoogleMapsClient
from shared.llm_client import OpenAICompatibleChatClient

from task_module.task_schema import BenchmarkTask, WorldSliceSummary
from task_module._shared.base_generator import BaseTaskGenerator, make_folder_name
from task_module._shared.sampler import TaskSampler
from task_module.task_types.route_planning.route_cache import RouteCache
from task_module.task_types.route_planning.sampler import RouteSampler

from task_module.task_types.region_aware.sampler import RegionSampler, activity_for_constraints
from task_module.task_types.region_aware.kb_manager import CulturalKB, list_available_cities
from task_module.task_types.region_aware.constraint_builder import build_scenario
from task_module.task_types.region_aware.validator import run_audit
from task_module.task_types.region_aware.prompts import build_enrich_prompt
from task_module.task_types.region_aware.solver import region_decision_identity, solve_region_plan
from task_module.task_types.region_aware.dependency import build_lineage
from task_module.task_types.region_aware.rule_compiler import compile_structured_candidates
from task_module.task_types.region_aware.stage3_candidate_builder import (
    Stage3CandidateBuilder,
)
from task_module._shared.task_contract import assigned_time_context


LEGACY_FORMAL_GENERATION_BLOCK_REASON = (
    "Formal Region generation is disabled: the legacy character-and-itinerary "
    "generator cannot produce target-first geoword.region_contextual_choice.v1 "
    "tasks with a verified regional selection effect."
)


def _stage3_map_details(place: dict[str, Any]) -> dict[str, Any]:
    projection = place.get("map_projection") if isinstance(place.get("map_projection"), dict) else {}
    details = projection.get("google_place_details") if isinstance(projection.get("google_place_details"), dict) else {}
    return details


def _stage3_price_fact(
    place: dict[str, Any],
) -> tuple[Decimal | None, Decimal | None, str]:
    """Read one already-frozen Google price fact; never request or infer it."""
    price_range = _stage3_map_details(place).get("price_range")
    if not isinstance(price_range, dict):
        return None, None, ""

    def money(key: str) -> Decimal | None:
        value = price_range.get(key)
        if not isinstance(value, dict):
            return None
        try:
            units = Decimal(str(value.get("units") or 0))
            nanos = Decimal(str(value.get("nanos") or 0)) / Decimal("1000000000")
            return units + nanos
        except (InvalidOperation, TypeError, ValueError):
            return None

    start = price_range.get("startPrice") if isinstance(price_range.get("startPrice"), dict) else {}
    end = price_range.get("endPrice") if isinstance(price_range.get("endPrice"), dict) else {}
    start_currency = str(start.get("currencyCode") or "").upper()
    end_currency = str(end.get("currencyCode") or "").upper()
    if not start_currency or start_currency != end_currency:
        return None, None, ""
    return money("startPrice"), money("endPrice"), start_currency


def _stage3_price_bounds(place: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    minimum, maximum, _currency = _stage3_price_fact(place)
    return minimum, maximum


def _stage3_route_index(route_cache: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    """Index only available static routes from a frozen cache artifact."""
    result: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in route_cache.get("cache") or []:
        if not isinstance(entry, dict) or str(entry.get("snapshot_id") or "static_v1") != "static_v1":
            continue
        route = entry.get("route") if isinstance(entry.get("route"), dict) else {}
        if route.get("available") is not True or int(route.get("duration_s") or 0) <= 0:
            continue
        key = (
            str(entry.get("origin_place_id") or ""),
            str(entry.get("dest_place_id") or ""),
            str(entry.get("mode") or "").upper(),
        )
        result[key] = entry
    return result


def _stage3_route_modes() -> list[str]:
    """Return the complete set of modes that Region may expose."""
    return ["WALK", "TRANSIT", "DRIVE"]


def _stage3_route_modes_for(character: dict[str, Any], place: dict[str, Any]) -> list[str]:
    origin = _character_route_endpoint(character)
    distance_m = RouteSampler.distance(origin, place)
    return list(RouteSampler._modes_for_distance(distance_m))


def _resolve_region_routing_place_ids(
    world: dict[str, Any], route_pool_payload: dict, route_entries: list[dict],
) -> set[str]:
    """Resolve active-version Routing coverage without restricting Region semantics."""
    places = [place for place in world.get("places") or [] if isinstance(place, dict)]
    places_by_id = {
        str(place.get("place_id") or ""): place for place in places if place.get("place_id")
    }
    manifest_ids = {
        str(place_id) for place_id in route_pool_payload.get("place_ids") or []
        if str(place_id) in places_by_id
    }
    if manifest_ids:
        return manifest_ids

    coordinate_to_id = {
        (round(float(location["lat"]), 6), round(float(location["lng"]), 6)):
            str(place.get("place_id") or "")
        for place in places
        if isinstance((location := place.get("location")), dict)
        and location.get("lat") is not None and location.get("lng") is not None
        and place.get("place_id")
    }
    compatible_ids: set[str] = set()
    for entry in route_entries:
        if not isinstance(entry, dict):
            continue
        for prefix in ("origin", "dest"):
            place_id = str(entry.get(f"{prefix}_place_id") or "")
            if place_id in places_by_id:
                compatible_ids.add(place_id)
                continue
            try:
                mapped = coordinate_to_id.get((
                    round(float(entry.get(f"{prefix}_lat")), 6),
                    round(float(entry.get(f"{prefix}_lng")), 6),
                ))
            except (TypeError, ValueError):
                mapped = None
            if mapped:
                compatible_ids.add(mapped)
    return compatible_ids


def _stage3_route_for(
    route_index: dict[tuple[str, str, str], dict[str, Any]],
    character: dict[str, Any], place: dict[str, Any], mode: str,
) -> dict[str, Any] | None:
    key = (
        f"character:{character.get('character_id')}",
        str(place.get("place_id") or ""),
        mode,
    )
    entry = route_index.get(key)
    if entry is not None:
        return entry
    bare, _ = person_route_ids(str(character.get("character_id") or ""))
    alternate = route_index.get((bare, key[1], mode))
    home = {"lat": character.get("home_lat"), "lng": character.get("home_lng")}
    if alternate and route_endpoint_matches(alternate, "origin", home, character.get("name")):
        return alternate
    return None


def _stage3_goal_text(rule: dict[str, Any]) -> str:
    """Return the complete Stage-1 statement for author-side goal wording."""
    statement = str(rule.get("statement") or "").strip()
    if not statement:
        raise ValueError("Region Stage 3 requires a non-empty Stage 1 statement")
    return statement


REGION_PROMPT_SYSTEM = """Your responsibility is to turn the supplied, already-decided place-selection facts into a natural, realistic, and executable request.
The construction system has already selected the private target place and computed the complete factual conditions required for the task. Your role is only to write the request. Do not identify the answer, select constraints, calculate boundaries, recommend tools, or explain the construction process.
The input contains three core parts:

1. `task_context` supplies the traveler, city, public real-world starting place, and task date and time. `response_requirement` states what the evaluated assistant must return.
2. `activity_intent` is the traveler's practical need. Use `regional_context` to connect that need to the kind of place the traveler is looking for. Use `source_backed_place_requirements` for the concrete requirements that the selected place must meet.
   If a source-backed clue lets the reader infer a name relevant to the search, use the clue and refer back to it naturally in the request. State a service, dish, or area directly when it is itself part of what the traveler wants. Preserve the scope of the regional fact and the type of place being requested.
   Use background advice to explain the traveler's plans where relevant. A mention of a place, route, or service does not establish that the selected place offers it. Do not add an unsupported requirement or reveal the answer.
   When `service_match_context` is present, read each excerpt in context to determine what the source actually describes or offers. Treat the matched phrase as a search hit. Keep source URLs and any details that identify the answer out of the request.
3. `public_place_constraints` contains the complete and final place-selection conditions. Together with `activity_intent`, these conditions help the evaluated planning assistant identify the target place. Every condition must appear in the request. You may adjust their order and wording for fluency, but must not omit, relax, strengthen, replace, select, or add conditions.

Writing requirements:
Write the complete request in natural English from the traveler's perspective or on the traveler's behalf.

- Write the complete request in natural English from the traveler's perspective or on the traveler's behalf.
- Use the real starting place's name; do not expose raw coordinates.
- Describe transportation in ordinary language, such as walking, public transportation, or driving.
- Integrate route times and distances naturally into real-life preferences.
- You may combine related conditions naturally in one sentence, but must not omit, relax, strengthen, or invent conditions.
- Express prices, services, areas, place types, and opening hours as natural, real-life travel needs.
- You may combine related constraints in one natural sentence, but must not omit, relax, strengthen, or add conditions.
- Do not invent a deadline from the supplied time unless a public constraint explicitly makes it a deadline.

Return JSON only. Output one JSON object:
{
  "task": "The complete natural-language task for the evaluated planning assistant"
}"""


def _serialized_request_audit(payload: dict[str, Any], *, contract: str) -> dict[str, Any]:
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    observed_keys: set[str] = set()

    def collect_keys(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                observed_keys.add(str(key).casefold())
                collect_keys(nested)
        elif isinstance(value, list):
            for nested in value:
                collect_keys(nested)

    collect_keys(payload)
    forbidden = sorted(observed_keys & {
        "steps", "polyline", "instructions", "preferred_modes", "max_walk_m", "has_car",
        "place_id", "route_id", "target_poi", "alternative_poi",
    })
    if forbidden:
        raise ValueError(f"{contract} payload contains fields outside the confirmed contract: {forbidden}")
    return {
        "contract": contract,
        "payload_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "serialized_characters": len(serialized),
        "estimated_tokens": max(1, math.ceil(len(serialized.encode("utf-8")) / 4)),
        "forbidden_fields": [],
        "status": "passed_before_external_call",
    }


def _append_model_call_log(path: Path | None, record: dict[str, Any]) -> None:
    if path is None:
        return
    payload = read_json(path) if path.is_file() else {
        "schema_version": "geoword.region_stage3_model_calls.v1", "calls": [],
    }
    payload.setdefault("calls", []).append(record)
    write_json(path, payload)


def _character_route_endpoint(character: dict[str, Any]) -> dict[str, Any]:
    mobility = character.get("mobility") if isinstance(character.get("mobility"), dict) else {}
    return {
        "place_id": f"character:{character.get('character_id')}",
        "name": str(character.get("name") or ""),
        "location": {"lat": mobility.get("lat"), "lng": mobility.get("lng")},
    }


def _character_public_starting_place(
    character: dict[str, Any], world: dict[str, Any], district_names: dict[str, str],
) -> dict[str, Any]:
    mobility = character.get("mobility") if isinstance(character.get("mobility"), dict) else {}
    current = mobility.get("current_place") if isinstance(mobility.get("current_place"), dict) else {}
    if current.get("name") and isinstance(current.get("location"), dict):
        return {
            "name": str(current["name"]),
            "district": str(
                current.get("district_name")
                or district_names.get(str(current.get("district_id") or ""))
                or ""
            ),
        }
    endpoint = _character_route_endpoint(character)
    location = endpoint.get("location") or {}
    for place in world.get("places") or []:
        place_location = place.get("location") if isinstance(place.get("location"), dict) else {}
        try:
            same = (
                round(float(place_location.get("lat")), 6) == round(float(location.get("lat")), 6)
                and round(float(place_location.get("lng")), 6) == round(float(location.get("lng")), 6)
            )
        except (TypeError, ValueError):
            same = False
        if same and place.get("name"):
            return {
                "name": str(place["name"]),
                "district": _region_place_district(place, district_names),
            }
    # Older character snapshots may retain only a district anchor's coordinates.
    from task_module.task_types.place_discovery.batch_generation.sampler import _resolve_legacy_character_place

    restored = _resolve_legacy_character_place(world, mobility)
    if restored.get("name") and restored.get("source") == "district_anchor":
        return {
            "name": str(restored["name"]),
            "district": str(district_names.get(str(restored.get("district_id") or "")) or ""),
        }
    raise ValueError("Region character has no public real-place starting anchor")


def _haversine_m(a: dict[str, Any], b: dict[str, Any]) -> float:
    from math import asin, cos, radians, sin, sqrt
    la, lb = a.get("location") or {}, b.get("location") or {}
    lat1, lon1, lat2, lon2 = map(radians, [float(la["lat"]), float(la["lng"]), float(lb["lat"]), float(lb["lng"])])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    return 6371000 * 2 * asin(sqrt(sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2))


def _ordered_region_alternatives(
    target: dict[str, Any], alternatives: list[dict[str, Any]],
    alternative_use: dict[str, int], rng: random.Random,
    *, routing_place_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Prefer Routing-covered R-, then retain the existing local ordering."""
    routing_ids = routing_place_ids or set()
    decorated = [(place, rng.random()) for place in alternatives]
    decorated.sort(key=lambda item: (
        str(item[0].get("place_id") or "") not in routing_ids,
        _haversine_m(target, item[0]),
        alternative_use.get(str(item[0].get("place_id") or ""), 0),
        item[1],
    ))
    return [place for place, _tie_break in decorated]


def _routing_preferred_candidates(
    candidates: list[dict[str, Any]], routing_place_ids: set[str],
) -> list[dict[str, Any]]:
    covered = [
        item for item in candidates
        if str((item.get("place") or item).get("place_id") or "") in routing_place_ids
    ]
    return covered or candidates


def _stage3_local_candidate_pool(
    target: dict[str, Any], related_places: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return all related POIs within the rank-5 straight-line radius."""
    target_id = str(target.get("place_id") or "")
    ordered = sorted(
        (place for place in related_places if str(place.get("place_id") or "") != target_id),
        key=lambda place: (_haversine_m(target, place), str(place.get("place_id") or "")),
    )
    if not ordered:
        return []
    radius_m = _haversine_m(target, ordered[min(4, len(ordered) - 1)])
    return [place for place in ordered if _haversine_m(target, place) <= radius_m]


def _validate_prompt_output(
    output: dict[str, Any], *, private_place_names: list[str] | None = None,
) -> dict[str, Any]:
    """Apply the same narrow writer boundary as Itinerary: wording only."""
    if not isinstance(output, dict) or set(output) != {"task"}:
        raise ValueError("Region task writer must return exactly one task field")
    prompt = str(output.get("task") or "").strip()
    if not prompt:
        raise ValueError("Region task writer returned an empty task")
    folded = prompt.casefold()
    if any(str(name or "").strip().casefold() in folded for name in private_place_names or [] if str(name or "").strip()):
        raise ValueError("Region task writer exposed a private candidate place name")
    return {"user_prompt": prompt}


def _write_region_prompt(
    prompt_client: OpenAICompatibleChatClient, prompt_input: dict[str, Any], *,
    private_place_names: list[str] | None = None,
    failure_path: Path | None = None, model_call_log_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if set(prompt_input) != {
        "task_context", "activity_intent", "public_place_constraints", "response_requirement",
    }:
        raise ValueError("Region task-writing payload violates its top-level schema")
    model_input = dict(prompt_input)
    request_audit = _serialized_request_audit(model_input, contract="region_task_writer_v5")
    messages = [
        {"role": "system", "content": REGION_PROMPT_SYSTEM},
        {"role": "user", "content": json.dumps(model_input, ensure_ascii=False)},
    ]
    for attempt in range(2):
        request_parameters = {"temperature": 0, "response_format": {"type": "json_object"}}
        try:
            response = prompt_client.call_chat(messages, extra=request_parameters)
        except Exception as exc:
            _append_model_call_log(model_call_log_path, {
                "contract": "region_task_writer_v5", "attempt": attempt + 1,
                "endpoint_name": prompt_client.endpoint_name, "model": prompt_client.model,
                "request_audit": request_audit, "messages": messages,
                "request_parameters": request_parameters, "status": "request_failed",
                "error_class": type(exc).__name__, "error": str(exc),
            })
            raise
        raw = ((response.get("choices") or [{}])[0].get("message") or {}).get("content")
        if not isinstance(raw, str):
            raise RuntimeError("Region prompt model returned no JSON content")
        try:
            validated = _validate_prompt_output(
                json.loads(raw), private_place_names=private_place_names,
            )
            _append_model_call_log(model_call_log_path, {
                "contract": "region_task_writer_v5", "attempt": attempt + 1,
                "endpoint_name": prompt_client.endpoint_name, "model": prompt_client.model,
                "request_audit": request_audit, "messages": messages,
                "request_parameters": request_parameters, "response": response,
                "raw_model_output": raw, "usage": response.get("usage") or {}, "status": "validated",
            })
            return validated, request_audit
        except (json.JSONDecodeError, ValueError) as exc:
            _append_model_call_log(model_call_log_path, {
                "contract": "region_task_writer_v5", "attempt": attempt + 1,
                "endpoint_name": prompt_client.endpoint_name, "model": prompt_client.model,
                "request_audit": request_audit, "messages": messages,
                "request_parameters": request_parameters, "response": response,
                "raw_model_output": raw, "usage": response.get("usage") or {},
                "status": "validation_failed", "validation_error": str(exc),
            })
            if failure_path is not None:
                existing = read_json(failure_path) if failure_path.is_file() else {"failures": []}
                existing.setdefault("failures", []).append({
                    "contract": "region_task_writer_v5", "attempt": attempt + 1,
                    "validation_error": str(exc), "raw_model_output": raw,
                    "request_audit": request_audit,
                })
                write_json(failure_path, existing)
            if attempt:
                raise
            messages.extend([
                {"role": "assistant", "content": raw},
                {"role": "user", "content": (
                    f"输出未通过校验：{exc}\n"
                    "请保持题面含义不变，并仅按 {\"task\": \"...\"} 返回修正后的完整 JSON。"
                )},
            ])
    raise AssertionError("unreachable")


def _regular_opening_hours(place: dict[str, Any]) -> dict[str, Any] | None:
    """Read effective frozen world hours without deriving a task time."""
    from shared.opening_hours import effective_opening_hours
    return effective_opening_hours(place)


_ORDINARY_SERVICE_FIELDS = (
    "reservable", "dine_in", "takeout", "delivery", "curbside_pickup",
    "outdoor_seating", "live_music", "serves_breakfast", "serves_lunch",
    "serves_dinner", "serves_brunch", "serves_vegetarian_food",
    "wheelchair_accessible_entrance", "wheelchair_accessible_seating",
)


def _region_district_names(world: dict[str, Any]) -> dict[str, str]:
    return {
        str(item.get("district_id") or ""): str(item.get("name") or "")
        for item in world.get("districts") or []
        if isinstance(item, dict) and item.get("district_id") and item.get("name")
    }


def _region_place_district(place: dict[str, Any], district_names: dict[str, str]) -> str:
    return str(
        place.get("district_name")
        or district_names.get(str(place.get("district_id") or ""))
        or ""
    )


def _region_weekday_index(time_context: dict[str, Any]) -> int:
    value = str(time_context.get("instruction_time") or "")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed.weekday() + 1) % 7  # Google Places: Sunday=0.


def _region_opening_intervals(place: dict[str, Any], weekday_index: int) -> list[tuple[int, int]]:
    hours = _regular_opening_hours(place) or {}
    result: list[tuple[int, int]] = []
    for period in hours.get("periods") or []:
        if not isinstance(period, dict):
            continue
        opening = period.get("open") if isinstance(period.get("open"), dict) else {}
        closing = period.get("close") if isinstance(period.get("close"), dict) else {}
        if int(opening.get("day", -1)) != weekday_index:
            continue
        start = int(opening.get("hour") or 0) * 60 + int(opening.get("minute") or 0)
        close_day = int(closing.get("day", weekday_index))
        end = int(closing.get("hour") or 0) * 60 + int(closing.get("minute") or 0)
        if close_day != weekday_index or end <= start:
            end += 24 * 60
        result.append((start, end))
    return result


def _region_price_contains(place: dict[str, Any], amount: Decimal, currency: str) -> bool:
    minimum, maximum, place_currency = _stage3_price_fact(place)
    return (
        minimum is not None and maximum is not None
        and place_currency == currency and minimum <= amount <= maximum
    )


def _region_constraint_matches_place(
    place: dict[str, Any], constraint: dict[str, Any], *, character: dict[str, Any],
    route_index: dict[tuple[str, str, str], dict[str, Any]],
    district_names: dict[str, str], weekday_index: int,
) -> bool:
    kind = str(constraint.get("type") or "")
    if kind == "primary_type":
        return str(place.get("primary_type") or "") == str(constraint.get("value") or "")
    if kind == "district_name":
        return _region_place_district(place, district_names) == str(constraint.get("value") or "")
    if kind == "service_feature":
        return _stage3_map_details(place).get(str(constraint.get("value") or "")) is True
    if kind == "price_maximum":
        _minimum, maximum, currency = _stage3_price_fact(place)
        return (
            currency == str(constraint.get("unit") or "").upper()
            and maximum is not None and maximum <= Decimal(str(constraint["value"]))
        )
    if kind == "price_target_amount":
        return _region_price_contains(
            place, Decimal(str(constraint["value"])),
            str(constraint.get("unit") or "").upper(),
        )
    if kind in {"opens_by", "closes_at_or_after"}:
        intervals = _region_opening_intervals(place, weekday_index)
        if not intervals:
            return False
        value = int(constraint["value"])
        return min(start for start, _end in intervals) <= value if kind == "opens_by" else max(
            end for _start, end in intervals
        ) >= value
    if kind in {"route_duration_range", "walking_distance_range"}:
        mode = str(constraint.get("transportation_mode") or "").upper()
        entry = _stage3_route_for(route_index, character, place, mode)
        if entry is None:
            return False
        route = entry.get("route") or {}
        if kind == "route_duration_range":
            raw = route.get("duration_s")
            if constraint.get("duration_basis") == "exact_seconds":
                lower, upper = constraint.get("minimum"), constraint.get("maximum")
                return raw is not None and (
                    (lower is None or round(lower * 60) <= raw)
                    and (upper is None or raw <= round(upper * 60))
                )
            value = max(1, math.ceil(float(raw) / 60)) if raw is not None else None
        else:
            raw = route.get("distance_m") if mode == "WALK" else route.get("walking_distance_m")
            value = int(raw) if raw is not None else None
        return value is not None and int(constraint["minimum"]) <= value <= int(constraint["maximum"])
    return False


def _region_scalar_range_constraint(
    *, constraint_id: str, kind: str, target_value: int,
    other_values: list[int], unit: str, mode: str,
) -> dict[str, Any]:
    lower_values = [value for value in other_values if value < target_value]
    upper_values = [value for value in other_values if value > target_value]
    minimum = max(lower_values) + 1 if lower_values else 0
    maximum = min(upper_values) - 1 if upper_values else target_value + max(
        1, math.ceil(target_value * 0.25),
    )
    return {
        "constraint_id": constraint_id,
        "type": kind,
        "operator": "between",
        "minimum": minimum,
        "maximum": maximum,
        "unit": unit,
        "actual_value": target_value,
        "transportation_mode": mode,
        "origin_relation": "starting_place_to_target",
    }


def _region_atomic_constraints(
    target: dict[str, Any], comparison_places: list[dict[str, Any]], *,
    character: dict[str, Any], route_index: dict[tuple[str, str, str], dict[str, Any]],
    district_names: dict[str, str], weekday_index: int,
) -> list[dict[str, Any]]:
    """Enumerate source-backed public facts after the Region target is fixed."""
    atoms: list[dict[str, Any]] = []
    primary_type = str(target.get("primary_type") or "")
    if primary_type:
        atoms.append({
            "constraint_id": "c_primary_type", "type": "primary_type",
            "operator": "equals", "value": primary_type,
        })
    district = _region_place_district(target, district_names)
    if district:
        atoms.append({
            "constraint_id": "c_district_name", "type": "district_name",
            "operator": "equals", "value": district,
        })
    target_details = _stage3_map_details(target)
    for field in _ORDINARY_SERVICE_FIELDS:
        if target_details.get(field) is True:
            atoms.append({
                "constraint_id": f"c_service_{field}", "type": "service_feature",
                "operator": "includes", "value": field,
            })

    target_minimum, target_maximum, target_currency = _stage3_price_fact(target)
    if target_minimum is not None and target_maximum is not None and target_currency:
        atoms.append({
            "constraint_id": "c_price_maximum", "type": "price_maximum",
            "operator": "at_most", "value": str(target_maximum), "unit": target_currency,
        })
        price_points = {(target_minimum + target_maximum) / Decimal(2)}
        for place in comparison_places:
            other_minimum, other_maximum, other_currency = _stage3_price_fact(place)
            if (
                other_minimum is None or other_maximum is None
                or other_currency != target_currency
            ):
                continue
            if other_maximum < target_maximum:
                price_points.add((max(target_minimum, other_maximum) + target_maximum) / Decimal(2))
            if other_minimum > target_minimum:
                price_points.add((target_minimum + min(target_maximum, other_minimum)) / Decimal(2))
        for index, value in enumerate(sorted(price_points), start=1):
            if target_minimum <= value <= target_maximum:
                atoms.append({
                    "constraint_id": f"c_price_target_{index}", "type": "price_target_amount",
                    "operator": "supports_spend", "value": str(value.normalize()),
                    "unit": target_currency,
                    "source_minimum": str(target_minimum), "source_maximum": str(target_maximum),
                })

    intervals = _region_opening_intervals(target, weekday_index)
    if intervals:
        atoms.extend([
            {
                "constraint_id": "c_opens_by", "type": "opens_by", "operator": "at_most",
                "value": min(start for start, _end in intervals), "unit": "minute_of_day",
                "weekday_index": weekday_index,
            },
            {
                "constraint_id": "c_closes_at_or_after", "type": "closes_at_or_after",
                "operator": "at_least", "value": max(end for _start, end in intervals),
                "unit": "minute_of_day", "weekday_index": weekday_index,
            },
        ])

    all_places = [target, *comparison_places]
    for mode in _stage3_route_modes():
        entries = [_stage3_route_for(route_index, character, place, mode) for place in all_places]
        if entries[0] is None:
            continue
        duration_values = [
            max(1, math.ceil(float((entry.get("route") or {})["duration_s"]) / 60))
            for entry in entries[1:] if entry is not None and (entry.get("route") or {}).get("duration_s")
        ]
        target_duration = max(1, math.ceil(float((entries[0].get("route") or {})["duration_s"]) / 60))
        atoms.append(_region_scalar_range_constraint(
            constraint_id=f"c_route_duration_{mode.casefold()}", kind="route_duration_range",
            target_value=target_duration, other_values=duration_values, unit="minute", mode=mode,
        ))
        metric_key = "distance_m" if mode == "WALK" else "walking_distance_m"
        target_distance = (entries[0].get("route") or {}).get(metric_key)
        if target_distance is not None and target_distance > 0:
            other_distances = [
                int((entry.get("route") or {})[metric_key])
                for entry in entries[1:] if entry is not None and (entry.get("route") or {}).get(metric_key) is not None
            ]
            atoms.append(_region_scalar_range_constraint(
                constraint_id=f"c_walking_distance_{mode.casefold()}", kind="walking_distance_range",
                target_value=int(target_distance), other_values=other_distances, unit="meter", mode=mode,
            ))
    return atoms


def _region_group_duration_constraints(target_id, required_ids, places, character, route_index):
    """Enumerate natural minute bounds covering the fixed group's frozen seconds."""
    for mode in _stage3_route_modes():
        durations = {}
        for place in places:
            entry = _stage3_route_for(route_index, character, place, mode)
            raw = ((entry or {}).get("route") or {}).get("duration_s")
            if isinstance(raw, (int, float)) and math.isfinite(raw) and raw > 0:
                durations[place["place_id"]] = raw
        if not required_ids <= durations.keys():
            continue
        required = [durations[pid] for pid in required_ids]
        low, high = min(required), max(required)
        # Round candidate boundaries, not route observations. Use nearby whole
        # and half minutes, plus exact observed seconds for narrow gaps.
        boundaries = {
            bound for value in durations.values() for step in (1, 30, 60)
            for bound in (math.floor(value / step) * step, math.ceil(value / step) * step)
            if bound > 0 and (step != 1 or value in required)
        }
        lower = [b for b in boundaries if b <= low]
        upper = [b for b in boundaries if b >= high]
        bounds = [(None, b) for b in upper]
        bounds.extend((lo, hi) for lo in lower for hi in upper)
        bounds.sort(key=lambda pair: (
            sum(b is not None for b in pair),
            sum(b % 30 != 0 for b in pair if b is not None),
            sum(abs(b - durations[target_id]) for b in pair if b is not None),
            tuple(b or 0 for b in pair),
        ))
        seen = set()
        for lo, hi in bounds:
            passed = frozenset(pid for pid, seconds in durations.items()
                               if (lo is None or lo <= seconds) and (hi is None or seconds <= hi))
            if passed in seen:
                continue
            seen.add(passed)
            yield {
                "constraint_id": f"c_route_duration_{mode.casefold()}_{lo}_{hi}",
                "type": "route_duration_range",
                "operator": "at_most" if lo is None else "between",
                "minimum": lo / 60 if lo is not None else None,
                "maximum": hi / 60 if hi is not None else None,
                "unit": "minute", "duration_basis": "exact_seconds",
                "actual_value": durations[target_id] / 60,
                "transportation_mode": mode, "origin_relation": "starting_place_to_target",
            }


def _select_region_public_constraints(
    target: dict[str, Any], regional_matches: list[dict[str, Any]],
    local_non_matches: list[dict[str, Any]], *, character: dict[str, Any],
    route_index: dict[tuple[str, str, str], dict[str, Any]],
    district_names: dict[str, str], weekday_index: int, rng: random.Random | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """Choose the R-minus group before searching for ordinary conditions."""
    target_id = str(target.get("place_id") or "")
    regional_places = [
        place for place in regional_matches
        if str(place.get("place_id") or "") != target_id
    ]
    comparison = list({
        str(place.get("place_id") or ""): place
        for place in [*regional_places, *local_non_matches]
        if place.get("place_id")
    }.values())
    if not local_non_matches:
        return None
    rng = rng if rng is not None else random.Random(0)
    pool = sorted(local_non_matches, key=lambda place: place["place_id"])
    k = rng.randint(1, max(1, min(3, len(pool) // 2)))
    groups = list(combinations(pool, k))
    rng.shuffle(groups)
    facts = dict(character=character, route_index=route_index,
                 district_names=district_names, weekday_index=weekday_index)
    base_atoms = [atom for atom in _region_atomic_constraints(target, comparison, **facts)
                  if atom["type"] != "route_duration_range"]
    places = [target, *comparison]
    all_ids = {place["place_id"] for place in places}
    logger = logging.getLogger("region")
    for group in groups:
        required_ids = {target_id, *(place["place_id"] for place in group)}
        logger.info("Region C0 k=%s retained=%s", k, sorted(required_ids - {target_id}))
        atoms = [*base_atoms, *_region_group_duration_constraints(
            target_id, required_ids, places, character, route_index)]
        annotated = []
        for atom in atoms:
            passed = {place["place_id"] for place in places
                      if _region_constraint_matches_place(place, atom, **facts)}
            if required_ids <= passed and passed != all_ids:
                annotated.append((atom, passed))
            elif required_ids == all_ids and passed == all_ids:
                annotated.append((atom, passed))
        # Keep the shortest path to each survivor set and route-mode state.
        pending = [(frozenset(all_ids), "", False, [])]
        visited = set()
        for remaining, mode, has_duration, selected in pending:
            for atom, passed in annotated:
                atom_mode = atom.get("transportation_mode", "")
                is_duration = atom["type"] == "route_duration_range"
                if (mode and atom_mode and mode != atom_mode) or (has_duration and is_duration):
                    continue
                survivors = remaining & passed
                if survivors == required_ids:
                    logger.info("Region C0 accepted k=%s retained=%s", k, sorted(required_ids - {target_id}))
                    return [*selected, atom], list(group)
                state = (survivors, mode or atom_mode, has_duration or is_duration)
                if survivors != remaining and state not in visited:
                    visited.add(state)
                    pending.append((*state, [*selected, atom]))
        logger.info("Region C0 no qualifying condition combination for k=%s retained=%s",
                    k, sorted(required_ids - {target_id}))
    return None


def _region_writer_constraint(constraint: dict[str, Any], starting_place: dict[str, Any]) -> dict[str, Any]:
    item = {key: value for key, value in constraint.items() if key != "constraint_id"}
    item["constraint_id"] = constraint["constraint_id"]
    if item.pop("duration_basis", None) == "exact_seconds":
        item.pop("actual_value", None)
        for bound in ("minimum", "maximum"):
            if item[bound] is None:
                item.pop(bound)
            else:
                seconds = round(item[bound] * 60)
                minutes, remainder = divmod(seconds, 60)
                if remainder not in (0, 30):
                    item["natural_" + bound] = f"{minutes} min {remainder} sec"
    if constraint.get("origin_relation") == "starting_place_to_target":
        item["public_origin"] = {"name": starting_place.get("name")}
    value = constraint.get("value")
    if constraint.get("type") in {"opens_by", "closes_at_or_after"} and value is not None:
        minute = int(value) % (24 * 60)
        item["natural_time"] = f"{minute // 60:02d}:{minute % 60:02d}"
    return item


def _materialize_character_routes(
    route_cache: RouteCache, maps_client: GoogleMapsClient, character: dict[str, Any],
    places: list[dict[str, Any]], *, snapshot_id: str = "static_v1",
) -> None:
    origin = _character_route_endpoint(character)
    location = origin.get("location") or {}
    if location.get("lat") is None or location.get("lng") is None:
        raise ValueError(f"Region character lacks frozen coordinates: {character.get('character_id')}")
    for place in places:
        for mode in _stage3_route_modes_for(character, place):
            entry = route_cache.get(origin, place, mode, snapshot_id=snapshot_id)
            if entry is None:
                result = route_cache.get_or_compute(origin, place, mode, maps_client, snapshot_id=snapshot_id)
                if result.get("api_error") or result.get("error") == "API_FAILED":
                    raise RuntimeError(
                        f"Region route infrastructure failed for {origin['place_id']} -> "
                        f"{place.get('place_id')} ({mode})"
                    )
    route_cache.save()


def _build_stage3_contextual_choice_candidate(
    *,
    rule: dict[str, Any],
    target: dict[str, Any],
    all_type_places: list[dict[str, Any]],
    supported_places: list[dict[str, Any]],
    character: dict[str, Any],
    city_name: str,
    route_index: dict[tuple[str, str, str], dict[str, Any]],
    time_context: dict[str, str],
) -> dict[str, Any] | None:
    """Create C only after a concrete evidence-supported P* is selected.

    The ordinary constraints use frozen map facts available in the normal
    construction flow.  A candidate lacking relevance evidence is an ordinary
    alternative only for this frozen-evidence construction pass; it is not a
    claim about the merchant's real-world inventory.
    """
    target_place = target["place"]
    target_details = _stage3_map_details(target_place)
    target_min_price, target_max_price = _stage3_price_bounds(target_place)
    require_dine_in = target_details.get("dine_in") is True
    require_operational = str((target_place.get("map_projection") or {}).get("business_status") or "") == "OPERATIONAL"
    target_type = str(target_place.get("primary_type") or "")

    def meets_frozen_place_constraints(place: dict[str, Any]) -> bool:
        if str(place.get("primary_type") or "") != target_type:
            return False
        details = _stage3_map_details(place)
        if require_dine_in and details.get("dine_in") is not True:
            return False
        status = str((place.get("map_projection") or {}).get("business_status") or "")
        if require_operational and status != "OPERATIONAL":
            return False
        if target_max_price is not None:
            candidate_min, _candidate_max = _stage3_price_bounds(place)
            if candidate_min is None or candidate_min > target_max_price:
                return False
        return True

    supported_place_ids = {str(item["place"].get("place_id") or "") for item in supported_places}
    supported_ordinary = [
        item for item in supported_places if meets_frozen_place_constraints(item["place"])
    ]
    if str(target_place.get("place_id") or "") not in {
        str(item["place"].get("place_id") or "") for item in supported_ordinary
    }:
        return None
    unsupported_ordinary = [
        place for place in all_type_places
        if str(place.get("place_id") or "") not in supported_place_ids
        and meets_frozen_place_constraints(place)
    ]
    if not unsupported_ordinary:
        return None

    try:
        departure = datetime.fromisoformat(time_context["instruction_time"])
    except (KeyError, TypeError, ValueError):
        return None
    selected_route: dict[str, Any] | None = None
    alternative_route: dict[str, Any] | None = None
    alternative: dict[str, Any] | None = None
    travel_limit_min: int | None = None
    selected_mode = ""
    for mode in _stage3_route_modes():
        target_route = _stage3_route_for(route_index, character, target_place, mode)
        if target_route is None:
            continue
        other_supported_durations = [
            int(route["route"]["duration_s"])
            for item in supported_ordinary
            if str(item["place"].get("place_id") or "") != str(target_place.get("place_id") or "")
            if (route := _stage3_route_for(route_index, character, item["place"], mode)) is not None
        ]
        next_supported_s = min(other_supported_durations) if other_supported_durations else None
        routed_alternatives = [
            (place, route)
            for place in unsupported_ordinary
            if (route := _stage3_route_for(route_index, character, place, mode)) is not None
        ]
        routed_alternatives.sort(key=lambda pair: (
            int(pair[1]["route"]["duration_s"]), str(pair[0].get("place_id") or ""),
        ))
        for candidate_alternative, candidate_route in routed_alternatives:
            required_s = max(
                int(target_route["route"]["duration_s"]),
                int(candidate_route["route"]["duration_s"]),
            )
            candidate_limit_min = max(1, math.ceil(required_s / 60))
            if next_supported_s is not None and candidate_limit_min * 60 >= next_supported_s:
                continue
            selected_route = target_route
            alternative_route = candidate_route
            alternative = candidate_alternative
            travel_limit_min = candidate_limit_min
            selected_mode = mode
            break
        if alternative is not None:
            break
    if selected_route is None or alternative_route is None or alternative is None or travel_limit_min is None:
        return None

    deadline = departure + timedelta(minutes=travel_limit_min)
    mobility = character.get("mobility") if isinstance(character.get("mobility"), dict) else {}
    ordinary_constraints: dict[str, Any] = {
        "primary_type": target_type,
        "dine_in_required": require_dine_in,
        "operational_required": require_operational,
        "origin": {
            "character_id": character.get("character_id"),
            "lat": mobility.get("lat"),
            "lng": mobility.get("lng"),
        },
        "travel_mode": selected_mode,
        "departure_time": departure.isoformat(),
        "latest_arrival_time": deadline.isoformat(),
        "maximum_travel_duration_s": travel_limit_min * 60,
    }
    if target_max_price is not None:
        ordinary_constraints["maximum_price_sgd"] = str(target_max_price)

    type_label = target_type.replace("_", " ")
    ordinary_text = ""
    if target_max_price is not None:
        ordinary_text += f" Keep the expected spend at or below SGD {target_max_price}."
    if require_dine_in:
        ordinary_text += " The place should support dining in."
    goal = _stage3_goal_text(rule)
    origin_lat = mobility.get("lat")
    origin_lng = mobility.get("lng")
    user_prompt = (
        f"At {departure.strftime('%Y-%m-%d %H:%M')}, {character['name']} starts at "
        f"coordinates ({origin_lat}, {origin_lng}) in {city_name}. The goal is: {goal}. "
        f"Choose one real {type_label} that is appropriate in the local context and can be "
        f"reached by {selected_mode.lower()} no later than {deadline.strftime('%H:%M')}."
        f"{ordinary_text} Submit the place name and location."
    )
    evidence = {
        "schema_version": "geoword.region_stage3_private_evidence.v1",
        "selected_regional_rule": {
            "rule_id": rule.get("rule_id"), "statement": rule.get("statement"),
        },
        "target": {
            "name": target_place.get("name"), "location": target_place.get("location"),
            "evidence_channels": target["evidence_channels"],
            "name_evidence": target["name_evidence"], "detail_evidence": target["detail_evidence"],
            "retrieval_matches": target.get("retrieval_matches", {}),
        },
        "ordinary_constraints": ordinary_constraints,
        "ordinary_alternative": {
            "name": alternative.get("name"), "location": alternative.get("location"),
            "frozen_evidence_result": "no_name_or_source_backed_detail_supported_rule",
        },
        "route_evidence": {
            "target_route_id": selected_route.get("route_id"),
            "target_duration_s": selected_route["route"].get("duration_s"),
            "alternative_route_id": alternative_route.get("route_id"),
            "alternative_duration_s": alternative_route["route"].get("duration_s"),
            "route_snapshot_id": selected_route.get("snapshot_id"),
        },
        "counterfactual": {
            "ordinary_alternative_meets_C": True,
            "target_meets_C_and_R": True,
            "supported_options_meeting_C_and_R": 1,
            "decision_changed_by_R": True,
        },
    }
    evidence["evidence_sha256"] = hashlib.sha256(
        json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "target": target_place,
        "accepted_places": [target_place],
        "ordinary_alternative": alternative,
        "route_entries": [selected_route, alternative_route],
        "user_prompt": user_prompt,
        "private_evidence": evidence,
    }


def _build_stage3_public_summary(
    target_place: dict[str, Any], accepted_option_count: int, *, user_prompt: str = "",
) -> dict[str, Any]:
    """Build public task metadata without leaking target or comparator identities."""
    return {
        "prompt_language": "zh" if re.search(r"[\u3400-\u9fff]", user_prompt) else "en",
        "region_task_shape": "contextual_choice",
        "accepted_option_count": accepted_option_count,
        "requested_primary_type": str(target_place.get("primary_type") or ""),
    }


class RegionAwareGenerator(BaseTaskGenerator):
    task_type = "region_aware"
    label_zh = "地域感知选择"
    default_count = 10
    min_chars = 1
    max_chars = 1

    def __init__(self, world: dict, characters: dict,
                 env_events: list, judge_nodes: list,
                 output_dir: str, seed: int = 42,
                 use_llm_prompt: bool = True,
                 config_yaml: str = "config.yaml",
                 stage_model_key: str = "scenario_patch",
                 admission_mode: str = "prototype_validated",
                 cultural_constraints_path: str = "", cultural_evidence_path: str = "",
                 regional_rules_path: str = "", regional_bindings_path: str = "",
                 service_detail_cache_path: str = "",
                 route_cache_path: str = "",
                 dynamic_source_ready: bool = False):
        super().__init__(
            world=world, characters=characters, env_events=env_events,
            judge_nodes=judge_nodes, output_dir=output_dir, seed=seed,
            use_llm_prompt=use_llm_prompt, config_yaml=config_yaml,
        )
        self.stage_model_key = stage_model_key
        if admission_mode not in {"prototype_validated", "audit_approved_only"}:
            raise ValueError(f"Unsupported Region admission mode: {admission_mode}")
        self.admission_mode = admission_mode
        self.cultural_constraints_path = cultural_constraints_path
        self.cultural_evidence_path = cultural_evidence_path
        self.regional_rules_path = regional_rules_path
        self.regional_bindings_path = regional_bindings_path
        self.service_detail_cache_path = service_detail_cache_path
        self.route_cache_path = route_cache_path
        self.dynamic_source_ready = bool(dynamic_source_ready)
        self.sampler_shared = self.sampler

    def generate(self, count: int | None = None) -> list[BenchmarkTask]:
        return self._generate_contextual_choices(count or self.default_count)

    def _generate_contextual_choices(self, count: int) -> list[BenchmarkTask]:
        """Materialize the target-first Region contract from Stage 1/2 assets."""
        rules_path = Path(self.regional_rules_path)
        bindings_path = Path(self.regional_bindings_path)
        service_detail_path = Path(getattr(self, "service_detail_cache_path", ""))
        route_cache_path = Path(getattr(self, "route_cache_path", ""))
        if (
            not rules_path.is_file() or not bindings_path.is_file()
            or not service_detail_path.is_file()
        ):
            raise RuntimeError(
                "Formal Region Stage 3 requires regional_rules, "
                "regional_rule_stage2_assessments, complete service_detail_cache, "
                "and a version-bound shared route cache location."
            )
        rules_asset = read_json(rules_path)
        bindings_asset = read_json(bindings_path)
        human_review_path = bindings_path.parent / "region_human_reviews.json"
        human_revisions = read_json(human_review_path).get("revisions") if human_review_path.is_file() else {}
        if not isinstance(human_revisions, dict):
            human_revisions = {}
        if human_revisions:
            rules_asset = json.loads(json.dumps(rules_asset))
            bindings_asset = json.loads(json.dumps(bindings_asset))
            rules_by_id = {str(item.get("rule_id") or ""): item for item in rules_asset.get("rules") or [] if isinstance(item, dict)}
            assessments_by_id = {str(item.get("rule_id") or ""): item for item in bindings_asset.get("assessments") or [] if isinstance(item, dict)}
            for rule_id, revision in human_revisions.items():
                if not isinstance(revision, dict):
                    continue
                rule = rules_by_id.get(str(rule_id))
                assessment = assessments_by_id.get(str(rule_id))
                if isinstance(rule, dict) and isinstance(revision.get("rule"), dict):
                    for key in ("statement", "extraction_status"):
                        if key in revision["rule"]:
                            rule[key] = revision["rule"][key]
                if isinstance(assessment, dict) and isinstance(revision.get("assessment"), dict):
                    for key in ("atomicity", "independent_restrictions", "checks", "status",
                                "affected_poi_type_indexes", "affected_poi_primary_types",
                                "poi_retrieval_phrases", "task_source_eligible", "reason", "human_review"):
                        if key in revision["assessment"]:
                            assessment[key] = revision["assessment"][key]
        service_detail_cache = read_json(service_detail_path)
        if bindings_asset.get("world_id") != (self._task_world_binding or {}).get("world_id"):
            raise RuntimeError("Region Stage 3 assessment asset does not match this formal world.")
        if bindings_asset.get("city_id") != self.city_id:
            raise RuntimeError("Region Stage 3 assessment asset does not match the selected city.")
        builder = Stage3CandidateBuilder(
            world=self.world,
            characters=self.characters,
            rules_asset=rules_asset,
            stage2_asset=bindings_asset,
            service_detail_cache=service_detail_cache,
            human_revisions_applied=bool(human_revisions),
        )
        inspection = builder.inspect()
        write_json(self.output_dir / "stage3_candidate_audit.json", {
            "schema_version": "geoword.region_stage3_batch_audit.v3",
            "world_id": bindings_asset.get("world_id"),
            "city_id": self.city_id,
            "city_version": bindings_asset.get("city_version"),
            "rules_path": str(rules_path),
            "bindings_path": str(bindings_path),
            "service_detail_cache_path": str(service_detail_path),
            "route_cache_path": str(route_cache_path),
            "requested_count": count,
            "inspection": inspection,
        })
        config = PipelineConfig(self.config_yaml)
        endpoint_name = config.stage_model(self.stage_model_key)
        endpoint = dict(config.model_endpoint(endpoint_name))
        endpoint["endpoint_name"] = endpoint_name
        rng = self.next_generation_rng()
        all_characters = [
            item for item in self.characters.get("characters") or []
            if isinstance(item, dict) and str(item.get("character_id") or "") and str(item.get("name") or "")
        ]
        district_names = _region_district_names(self.world)
        character_starting_places: dict[str, dict[str, Any]] = {}
        characters = []
        for character in all_characters:
            try:
                character_starting_places[str(character["character_id"])] = _character_public_starting_place(
                    character, self.world, district_names,
                )
            except ValueError:
                continue
            characters.append(character)
        if not characters:
            raise RuntimeError(
                "Region Stage 3 requires at least one character with a public POI anchor"
            )
        city_record = next(
            (item for item in self.world.get("cities") or [] if item.get("city_id") == self.city_id),
            {},
        )
        city_name = str(city_record.get("name") or self.city_id.replace("_", " ").title())
        pools = list(inspection["rule_type_pools"])
        rng.shuffle(pools)
        task_root = self.output_dir / "tasks"
        task_root.mkdir(parents=True, exist_ok=True)
        used_indices = {
            int(match.group(1)) for path in task_root.iterdir() if path.is_dir()
            for match in [re.match(r"task_(\d+)", path.name)] if match
        }
        task_index = max(used_indices, default=0) + 1

        tasks: list[BenchmarkTask] = []
        retrieval_cache: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {}
        retrieval_audit: list[dict[str, Any]] = []
        seen_combinations: set[tuple[str, str, str]] = set()
        character_use = {str(item["character_id"]): 0 for item in characters}
        rule_use = {str(item["rule_id"]): 0 for item in pools}
        exhausted_character_rules: set[tuple[str, str]] = set()
        for existing in task_root.iterdir():
            evidence_path = existing / "task_sandbox.json"
            if not existing.is_dir() or not evidence_path.is_file():
                continue
            try:
                old = read_json(evidence_path).get("stage3_private_evidence") or {}
                char_id = str((old.get("character") or {}).get("character_id") or "")
                rule_id = str((old.get("selected_regional_rule") or {}).get("rule_id") or "")
                target_id = str((old.get("target") or {}).get("place_id") or "")
                if char_id in character_use:
                    character_use[char_id] += 1
                if rule_id in rule_use:
                    rule_use[rule_id] += 1
                if char_id and rule_id and target_id:
                    seen_combinations.add((char_id, rule_id, target_id))
            except Exception:
                continue

        maps_client = GoogleMapsClient(config)
        shared_route_cache = RouteCache(self.city_id)
        if shared_route_cache.cache_path.resolve() != route_cache_path.resolve() and route_cache_path.is_file():
            raise RuntimeError("Region shared RouteCache path does not match the managed world/version route asset")
        route_pool_path = shared_route_cache.cache_dir / "route_poi_pool.json"
        route_pool_payload = read_json(route_pool_path) if route_pool_path.is_file() else {}
        routing_place_ids = _resolve_region_routing_place_ids(
            self.world, route_pool_payload, list(shared_route_cache._cache.values()),
        )
        prompt_client = OpenAICompatibleChatClient(
            base_url=str(endpoint.get("base_url") or ""), api_key=str(endpoint.get("api_key") or ""),
            model=str(endpoint.get("model") or ""),
            timeout_seconds=int(endpoint.get("request_timeout_seconds") or 90),
            endpoint_name=endpoint_name, pricing_reference=endpoint.get("pricing_reference"),
        )

        slot_attempt = 0
        while len(tasks) < count and pools:
            slot_attempt += 1
            rng = self.activate_task_attempt(task_index, slot_attempt)
            available_pairs = [
                (character, pool) for character in characters for pool in pools
                if (str(character["character_id"]), str(pool["rule_id"])) not in exhausted_character_rules
            ]
            if not available_pairs:
                break
            minimum_pair_use = min(
                (character_use[str(character["character_id"])], rule_use[str(pool["rule_id"])])
                for character, pool in available_pairs
            )
            character, pool = rng.choice([
                pair for pair in available_pairs
                if (character_use[str(pair[0]["character_id"])], rule_use[str(pair[1]["rule_id"])])
                == minimum_pair_use
            ])
            rule_id = str(pool["rule_id"])
            if rule_id not in retrieval_cache:
                matched, telemetry = builder.retrieve_rule_candidates(rule_id)
                retrieval_cache[rule_id] = (matched, telemetry)
                retrieval_audit.append({
                    "rule_id": rule_id,
                    "candidate_count": len(builder.candidate_rows(rule_id)),
                    "matched_count": len(matched),
                    "telemetry": telemetry,
                })
            matched, _telemetry = retrieval_cache[rule_id]
            if not matched:
                pools = [item for item in pools if str(item["rule_id"]) != rule_id]
                continue
            eligible_targets = [
                item for item in matched
                if (str(character["character_id"]), rule_id, str(item["place"].get("place_id") or ""))
                not in seen_combinations
            ]
            if not eligible_targets:
                exhausted_character_rules.add((str(character["character_id"]), rule_id))
                continue
            target = rng.choice(_routing_preferred_candidates(
                eligible_targets, routing_place_ids,
            ))
            target_place = target["place"]
            matched_ids = {str(item["place"].get("place_id") or "") for item in matched}
            related_places = builder.places_for_rule(rule_id)
            local_places = _stage3_local_candidate_pool(target_place, related_places)
            if not local_places:
                seen_combinations.add((str(character["character_id"]), rule_id, str(target_place.get("place_id") or "")))
                continue
            local_ids = {str(place.get("place_id") or "") for place in local_places}
            local_supported = [
                item for item in matched
                if str(item["place"].get("place_id") or "") in local_ids
            ]
            alternatives = [
                place for place in local_places
                if str(place.get("place_id") or "") not in matched_ids
            ]
            alternatives = _ordered_region_alternatives(
                target_place, alternatives, {}, rng,
                routing_place_ids=routing_place_ids,
            )
            candidate = None
            combination = (str(character["character_id"]), rule_id, str(target_place.get("place_id") or ""))
            pending_task_id = f"task_{self.city_id}_{self.task_type}_{task_index:04d}"
            base_time_context = assigned_time_context(
                pending_task_id, self.city_id, str(city_record.get("timezone") or "UTC"),
            )
            route_places_by_id = {
                str(place.get("place_id") or ""): place
                for place in [target_place, *local_places, *[item["place"] for item in matched]]
            }
            _materialize_character_routes(
                shared_route_cache, maps_client, character,
                list(route_places_by_id.values()),
            )
            route_index = _stage3_route_index(read_json(shared_route_cache.cache_path))
            time_context = base_time_context
            district_names = _region_district_names(self.world)
            weekday_index = _region_weekday_index(time_context)
            selected = _select_region_public_constraints(
                target_place, [item["place"] for item in matched], alternatives,
                character=character, route_index=route_index,
                district_names=district_names, weekday_index=weekday_index, rng=rng,
            )
            if selected is not None:
                constraints, satisfying_alternatives = selected
                starting_place = character_starting_places[str(character["character_id"])]
                matched_phrases = list(dict.fromkeys([
                    *list((target.get("retrieval_matches") or {}).get("poi_name") or []),
                    *list((target.get("retrieval_matches") or {}).get("service_detail") or []),
                ]))
                prompt_input = {
                    "task_context": {
                        "city": city_name,
                        "traveler": {"name": character["name"]},
                        "starting_place": starting_place,
                        "date_time": {
                            "instruction_time": time_context["instruction_time"],
                            "timezone": time_context["timezone"],
                        },
                    },
                    "activity_intent": {
                        "regional_context": _stage3_goal_text(builder.rules_by_id[rule_id]),
                        "source_backed_place_requirements": matched_phrases,
                    },
                    "public_place_constraints": {
                        "selected_constraints": [
                            _region_writer_constraint(item, starting_place) for item in constraints
                        ],
                    },
                    "response_requirement": "Return the selected place's name and location.",
                }
                if match_context := builder.writer_match_context(target):
                    prompt_input["activity_intent"]["service_match_context"] = match_context
                try:
                    prompt_output, task_writer_request_audit = _write_region_prompt(
                        prompt_client, prompt_input,
                        private_place_names=[
                            *[str(item["place"].get("name") or "") for item in matched],
                            *[str(place.get("name") or "") for place in alternatives],
                        ],
                        failure_path=self.output_dir / "stage3_model_output_failures.json",
                        model_call_log_path=self.output_dir / "stage3_model_calls.json",
                    )
                except ValueError as exc:
                    if "private candidate place name" not in str(exc):
                        raise
                    # This candidate cannot be repaired without changing its public/private
                    # boundary. Reject it and let the batch sampler construct another one.
                    logging.getLogger("region").warning(
                        "[Region] reject candidate %s: %s", combination, exc,
                    )
                    seen_combinations.add(combination)
                    continue
                selected_modes = {
                    str(item.get("transportation_mode") or "")
                    for item in constraints if item.get("transportation_mode")
                }
                mode = next(iter(selected_modes), "")
                route_entries = []
                if mode:
                    for place in [target_place, *satisfying_alternatives, *[item["place"] for item in matched]]:
                        route = _stage3_route_for(route_index, character, place, mode)
                        if route is not None and route.get("route_id") not in {
                            item.get("route_id") for item in route_entries
                        }:
                            route_entries.append(route)
                evidence = {
                    "schema_version": "geoword.region_stage3_private_evidence.v5",
                    "character": {"character_id": character["character_id"], "name": character["name"]},
                    "selected_regional_rule": builder.rules_by_id[rule_id],
                    "target": {"place_id": target_place["place_id"], "name": target_place["name"],
                               "location": target_place["location"], "retrieval_matches": target["retrieval_matches"]},
                    "local_candidate_pool": {
                        "radius_basis": "rank_5_straight_line_distance",
                        "place_ids": [str(place.get("place_id") or "") for place in local_places],
                        "region_match_place_ids": [str(item["place"].get("place_id") or "") for item in local_supported],
                        "non_region_match_place_ids": [str(place.get("place_id") or "") for place in alternatives],
                    },
                    "ordinary_alternatives_meeting_C": [
                        {"place_id": place["place_id"], "name": place["name"], "location": place["location"]}
                        for place in satisfying_alternatives
                    ],
                    "ordinary_constraints": constraints,
                    "selected_travel_mode": mode,
                    "activity_intent": prompt_input["activity_intent"],
                    "writer_public_place_constraints": prompt_input["public_place_constraints"],
                    "external_request_preflight": {"task_writer": task_writer_request_audit},
                    "route_ids": [route.get("route_id") for route in route_entries],
                    "counterfactual": {"target_meets_C_and_R": True,
                                       "ordinary_alternative_count_meeting_C": len(satisfying_alternatives),
                                       "ordinary_alternatives_match_R": False,
                                       "full_city_region_match_count": len(matched),
                                       "unique_with_C_and_R": True},
                }
                evidence["evidence_sha256"] = hashlib.sha256(
                    json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                candidate = {
                    "target": target_place, "ordinary_alternatives": satisfying_alternatives,
                    "accepted_places": [target_place], "user_prompt": prompt_output["user_prompt"],
                    "private_evidence": evidence,
                    "route_entries": route_entries,
                    "time_context": time_context,
                }
            if candidate is None:
                seen_combinations.add(combination)
                continue
            seen_combinations.add(combination)
            character_use[str(character["character_id"])] += 1
            rule_use[rule_id] += 1
            self.set_cost_unit(f"slot_{task_index:04d}")
            target_place = candidate["target"]
            involved_places = [target_place, *candidate["ordinary_alternatives"]]
            chars_for_slice = [{"character_id": character["character_id"], "name": character["name"]}]
            world_slice = self.sampler_shared.build_world_slice(chars_for_slice, involved_places)
            summary_data = self.sampler_shared.build_world_slice_summary(chars_for_slice, involved_places)
            public_options = [
                {"name": place["name"], "location": dict(place["location"])}
                for place in candidate["accepted_places"]
            ]
            decision = {
                "schema_version": "geoword.region_contextual_choice.v1",
                "accepted_options": public_options,
            }
            prompt = str(candidate["user_prompt"])
            time_context = candidate["time_context"]
            folder_name = make_folder_name(task_index, prompt)
            task_dir = task_root / folder_name
            task_dir.mkdir(parents=True, exist_ok=False)
            (task_dir / "generation_log").mkdir(exist_ok=True)

            task_id = f"task_{self.city_id}_{self.task_type}_{task_index:04d}"
            task = BenchmarkTask(
                task_id=task_id,
                task_type=self.task_type,
                difficulty="medium",
                city_id=self.city_id,
                variant="region_contextual_choice",
                generated_at=self.generated_at,
                time_context=time_context,
                user_prompt=prompt,
                summary=_build_stage3_public_summary(
                    target_place, len(public_options), user_prompt=prompt,
                ),
                world_slice=world_slice,
                world_slice_summary=WorldSliceSummary(**summary_data),
                metadata={"seed": self.seed, "folder": folder_name, "stage3_algorithm": "retrieval_route_unique_v1"},
            )
            sandbox = {
                "schema_version": "geoword.task.v1",
                "task_type": self.task_type,
                "task_id": task_id,
                "expected_feasible": True,
                "region_decision": decision,
                "stage3_private_evidence": candidate["private_evidence"],
                "frozen_route_entries": candidate["route_entries"],
                "audit_state": {"status": "fresh_pending_task_audit"},
            }
            static_state = {
                "schema_version": "geoword.static_app_state.v1",
                "lifecycle": "episode_start_frozen",
                "apps": {},
            }
            write_json(task_dir / "task.json", {
                "task_id": task.task_id, "task_type": task.task_type,
                "difficulty": task.difficulty, "city_id": task.city_id,
                "variant": task.variant, "generated_at": task.generated_at,
                "time_context": task.time_context,
                "user_prompt": task.user_prompt, "summary": task.summary,
                "metadata": task.metadata,
            })
            write_json(task_dir / "task_sandbox.json", sandbox)
            write_json(task_dir / "static_app_state.json", static_state)
            write_json(task_dir / "world_slice.json", task.world_slice_summary.to_dict())
            write_json(task_dir / "env_events.json", {"env_events": []})
            write_json(task_dir / "judge_sequence.json", {"stages": []})
            write_json(task_dir / "generation_log" / "stage3_candidate.json", {
                "target_options": public_options,
                "private_evidence_sha256": candidate["private_evidence"]["evidence_sha256"],
            })
            report = self.finalize_task_folder(task_dir)
            if report.get("valid"):
                tasks.append(task)
            task_index += 1
            slot_attempt = 0
        write_json(self.output_dir / "stage3_retrieval_audit.json", {
            "schema_version": "geoword.region_stage3_retrieval_audit.v1",
            "endpoint": endpoint_name,
            "rules_retrieved": retrieval_audit,
            "formalized": len(tasks),
        })
        if len(tasks) < count:
            raise RuntimeError(
                f"Region Stage 3 formalized only {len(tasks)}/{count} tasks; "
                "the finite frozen person/rule/target/alternative inventory is exhausted."
            )
        return tasks

        count = count or self.default_count
        rng = self.next_generation_rng()

        task_type_dir = self.output_dir / "tasks"
        task_type_dir.mkdir(parents=True, exist_ok=True)

        log_path = self.output_dir / "generate.log"
        _setup_log(log_path)
        logger = logging.getLogger("region")

        route_cache = RouteCache(self.city_id)
        # Route facts must already belong to the selected frozen city version.
        # Candidate sampling may read that cache but must not launch an
        # opportunistic Google Routes backfill during task construction.
        maps_client = None

        model_name = "unknown"
        try:
            ep = PipelineConfig(self.config_yaml).model_endpoint(
                PipelineConfig(self.config_yaml).stage_model(self.stage_model_key)
            )
            model_name = ep.get("model", "?")
        except Exception: pass

        logger.info("━" * 60)
        logger.info(f"BATCH START {self.output_dir.name}")
        logger.info(f"  TYPE {self.task_type}   COUNT {count}   SEED {self.seed}")
        logger.info(f"  LLM {self.stage_model_key}/{model_name}")
        logger.info("━" * 60)

        taken = set()
        for f in task_type_dir.iterdir():
            if f.is_dir() and f.name.startswith("task_"):
                try: taken.add(int(f.name.split("_")[1]))
                except (ValueError, IndexError): pass
        next_idx = 1
        while next_idx in taken: next_idx += 1

        kb = CulturalKB(
            self.city_id,
            constraints_path=self.cultural_constraints_path or None,
            evidence_path=self.cultural_evidence_path or None,
        )
        structured_candidates = compile_structured_candidates(kb, self.world)
        if structured_candidates:
            # Candidates are private authoring data for this formal batch.
            # They are deliberately not written into world or global KB files.
            kb._data = {**kb._data, "constraints": [*kb.all_constraints, *structured_candidates]}
        write_json(self.output_dir / "regional_rule_candidates.json", {
            "schema_version": "geoword.region_rule_candidates.v1",
            "city_id": self.city_id,
            "admission_mode": self.admission_mode,
            "constraints_path": self.cultural_constraints_path,
            "evidence_path": self.cultural_evidence_path,
            "candidates": structured_candidates,
        })
        sampler = RegionSampler(self.world, self.characters, route_cache, rng=rng,
                                target_city=self.city_id, admission_mode=self.admission_mode, kb=kb)

        tasks: list[BenchmarkTask] = []
        batch_tokens = {"prompt": 0, "completion": 0, "total": 0}
        batch_llm_dur = 0.0
        batch_api_calls = 0
        batch_success = 0
        batch_skip = 0
        batch_enrich_added = 0
        candidate_attempts = 0
        seen_decision_ids = _existing_region_decision_ids(task_type_dir)
        # Route facts are deliberately read-only in managed construction.  The
        # cache can cover only a subset of actor-to-POI pairs, so a cache miss
        # rejects one local candidate, not one requested formal task slot.
        # This budget performs no external calls and prevents an unbounded
        # search if the frozen asset has insufficient usable coverage.
        max_candidate_attempts = max(count * 25, count)
        llm_start_time = __import__("time").time()

        while batch_success < count and candidate_attempts < max_candidate_attempts:
            candidate_attempts += 1
            sub_seed = self.seed + candidate_attempts - 1
            task_idx = next_idx
            self.set_cost_unit(f"slot_{task_idx:04d}")
            task_id_short = f"task_{task_idx:04d}"

            city_name, task_kb = sampler.sample_city()
            constraints, chosen_keywords, pool_small = sampler.sample_constraints(task_kb, n_keywords=2, n_per_keyword=2)
            if not constraints:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP no approved constraints for {city_name}")
                continue

            constraint_ids = [c["id"] for c in constraints]

            planning_profile = sampler.build_planning_profile(constraints)
            if not planning_profile:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP sampled rules have no common city-backed place type")
                continue

            character = sampler.sample_character()
            if not character:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP no characters")
                continue

            main_pois = sampler.sample_pois_for_constraints(
                constraints, count=5, planning_profile=planning_profile,
            )
            required_types = {
                str(item) for constraint in constraints
                for item in (constraint.get("hidden_rule", {}).get("required_place_types", []) or [])
            }
            min_pois = 1 if required_types else 3
            if len(main_pois) < min_pois:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP only {len(main_pois)} POIs; need {min_pois}")
                continue

            distractors = sampler.sample_distractors(main_pois, constraints, count=2)

            route_stats_before = route_cache.stats_snapshot()
            route_data, route_queries = sampler.precheck_commutes(
                main_pois, distractors, character, maps_client,
            )
            route_stats_delta = _numeric_delta(route_stats_before, route_cache.stats_snapshot())
            batch_api_calls += int(route_stats_delta.get("api_requests", 0))

            # Verify private feasibility before the prompt-model call. Missing
            # frozen routes are a prerequisite failure, not a reason to retry
            # an external map API or spend an LLM request on an invalid task.
            gt = solve_region_plan(
                constraints, main_pois, distractors, route_data=route_data, character=character,
                planning_profile=planning_profile,
            )
            if not gt.get("expected_feasible"):
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP no cached forward route for a feasible Region plan")
                continue
            decision_contract = gt.get("decision_contract", {}) or {}
            if not decision_contract.get("material"):
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP cultural rule has no material plan effect")
                continue

            candidate_identity = region_decision_identity({
                "constraint_ids": constraint_ids,
                "planning_profile": planning_profile,
                "character": character,
                "expected_plan": gt.get("expected_plan", {}),
            })
            if candidate_identity in seen_decision_ids:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP duplicate private decision before prompt generation")
                continue
            # Reserve this decision before the wording model runs.  Rewording
            # an already-seen decision never creates a new benchmark item.
            seen_decision_ids.add(candidate_identity)
            task_kb.increment_used(constraint_ids)
            dynamic_source_graph_summary: dict[str, Any] | None = None
            if self.dynamic_source_ready:
                graph_pois, graph_routes = sampler.build_dynamic_source_graph(constraints, character, main_pois)
                if not _has_dynamic_region_successor(
                        constraints, graph_pois, [], graph_routes, character, gt,
                ):
                    batch_skip += 1
                    logger.info(
                        f"[{task_id_short}] SKIP no route- and rule-validated Dynamic successor"
                    )
                    continue
                dynamic_source_graph_summary = {
                    "candidate_count": len(graph_pois), "route_edge_count": len(graph_routes),
                    "asset_scope": "frozen_active_city_version_route_cache",
                }

            kw_label = ", ".join(chosen_keywords) if chosen_keywords else "general"
            scenario_label = next((c.get("scene", "") for c in constraints if c.get("scene")), kw_label)
            public_activity = activity_for_constraints(constraints)

            if self.use_llm_prompt:
                llm_result, token_usage = build_scenario(
                    character, city_name, scenario_label, public_activity,
                    planning_profile=planning_profile,
                    config_yaml=self.config_yaml, stage_model_key=self.stage_model_key,
                )
            else:
                llm_result = {"user_prompt": _template_user_prompt(
                    character, city_name, scenario_label, public_activity, planning_profile,
                )}
                token_usage = {"prompt": 0, "completion": 0, "total": 0, "duration_s": 0}

            if not llm_result:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP LLM API failed | {token_usage.get('total',0)} tok")
                continue

            user_prompt = llm_result.get("user_prompt", "")
            # The LLM phrases the request only. Source-backed cultural rules
            # and the private plan oracle cannot be overwritten by it.
            if not user_prompt or len(user_prompt) < 20:
                batch_skip += 1
                logger.info(f"[{task_id_short}] SKIP empty prompt | {token_usage.get('total',0)} tok")
                continue
            if not _has_named_subject(user_prompt, character):
        # A public task must identify the person whose context
                # the tested Agent may inspect. Replace ungrounded first-person
                # wording rather than admitting an ambiguous task.
                user_prompt = _template_user_prompt(
                    character, city_name, scenario_label, public_activity, planning_profile,
                )
                llm_result["user_prompt"] = user_prompt
                llm_result["subject_repair"] = "template_replaced_missing_named_subject"

            audit = run_audit(
                {"gt": gt, "user_prompt": user_prompt}, constraint_ids, planning_profile, constraints,
            )
            if audit.get("errors"):
                batch_skip += 1
                logger.info(f"[{task_id_short}] REJECTED decision contract | {'; '.join(audit['errors'][:3])}")
                continue
            if audit.get("warnings"):
                logger.warning(f"[{task_id_short}] audit: {len(audit['warnings'])} warnings")
                for w in audit["warnings"][:5]:
                    logger.warning(f"  WARN {w}")

            bt = token_usage
            batch_tokens["prompt"] += bt.get("prompt", 0)
            batch_tokens["completion"] += bt.get("completion", 0)
            batch_tokens["total"] += bt.get("total", 0)
            batch_llm_dur += bt.get("duration_s", 0)

            folder_name = make_folder_name(task_idx, user_prompt)
            task_folder = task_type_dir / folder_name
            task_folder.mkdir(parents=True, exist_ok=False)
            gen_dir = task_folder / "generation_log"
            gen_dir.mkdir(parents=True, exist_ok=True)

            llm_in = _build_llm_input_doc(
                character, city_name, scenario_label, public_activity, planning_profile,
            )
            (gen_dir / "llm_in.txt").write_text(llm_in, encoding="utf-8")
            (gen_dir / "llm_out.json").write_text(json.dumps(llm_result, ensure_ascii=False, indent=2), encoding="utf-8")

            # The reviewed Wikivoyage-backed set is private GT.  The prompt
            # writer is not permitted to discard a sampled cultural rule.
            matched_ids = list(constraint_ids)
            lineage = build_lineage(task_kb, constraints, self.admission_mode)
            review_projection = _build_review_projection(
                city_name, public_activity, constraints, decision_contract,
                gt.get("expected_plan", {}), main_pois, distractors,
            )
            (gen_dir / "details.json").write_text(json.dumps({
                "character": {k: v for k, v in character.items() if k not in ("preferences",)},
                "city": city_name, "keywords": chosen_keywords, "scenario": scenario_label,
                "public_activity": public_activity,
                "planning_profile": planning_profile,
                "kb_constraint_count": len(constraints),
                "matched_constraint_ids": matched_ids,
                "poi_count": len(main_pois), "distractor_count": len(distractors),
                "gt_summary": {
                    "feasible": gt.get("expected_feasible"),
                    "wrong_plans": len(gt.get("wrong_plans", [])),
                    "keywords": gt.get("explanation_keywords", []),
                    "cultural_requirements": gt.get("cultural_requirements", {}),
                    "decision_contract": decision_contract,
                },
                "generation": {
                    "llm_model": model_name,
                    "llm_duration_s": bt.get("duration_s", 0),
                    "tokens_prompt": bt.get("prompt", 0),
                    "tokens_completion": bt.get("completion", 0),
                    "tokens_total": bt.get("total", 0),
                    "route_graph_edge_count": len(route_data),
                    "route_cache_statistics": route_stats_delta,
                    "audit_warnings": len(audit.get("warnings", [])),
                },
            }, ensure_ascii=False, indent=2), encoding="utf-8")

            (gen_dir / "constraints.json").write_text(json.dumps({
                "city": city_name, "keywords": chosen_keywords,
                "constraint_ids": matched_ids,
                "planning_profile": planning_profile,
                "constraints_full": [{k: c.get(k) for k in ("id", "type", "rule_text", "keywords", "time", "severity", "source_type", "source_section", "source_quote")}
                                      for c in constraints if c["id"] in matched_ids],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            (gen_dir / "lineage.json").write_text(
                json.dumps(lineage, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (gen_dir / "compatibility.json").write_text(json.dumps({
                "city": city_name, "keywords": chosen_keywords,
                "main_pois": [p.get("name") for p in main_pois],
                "distractors": [d.get("name") for d in distractors],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            (gen_dir / "route_queries.json").write_text(json.dumps(route_queries, ensure_ascii=False, indent=2), encoding="utf-8")

            task_sandbox = {
                "task_type": "region_aware",
                "city": city_name,
                "keywords": chosen_keywords,
                "character": character,
                "constraint_ids": matched_ids,
                # Durable private metadata for reviewer/UI projection.  The
                # public task prompt still receives only the destination label.
                "planning_profile": planning_profile,
                "constraints": [{k: c.get(k) for k in ("id", "type", "rule_text", "keywords", "time", "severity", "review", "source_type", "source_section", "source_quote")}
                                 for c in constraints if c["id"] in matched_ids],
                "main_pois": main_pois,
                "distractors": distractors,
                "route_data": route_data,
                "expected_feasible": gt.get("expected_feasible", True),
                "expected_plan": gt.get("expected_plan", {}),
                "acceptable_plans": gt.get("acceptable_plans", []),
                "wrong_plans": gt.get("wrong_plans", []),
                "explanation_keywords": gt.get("explanation_keywords", []),
                "cultural_decisions": gt.get("cultural_decisions", []),
                "cultural_requirements": gt.get("cultural_requirements", {}),
                "decision_contract": decision_contract,
                # Reviewer-only source and counterfactual projection.  It is
                # never copied into task.json or the tested Agent's App state.
                "review_projection": review_projection,
                "region_lineage": lineage,
                "dynamic_source_ready": self.dynamic_source_ready,
                "dynamic_source_graph_summary": dynamic_source_graph_summary,
                "audit_state": {"status": "fresh_pending_task_audit"},
                "_audit": audit,
            }

            all_pois = main_pois + distractors
            chars_for_slice = [{"character_id": character["character_id"], "name": character["name"]}]
            world_slice = self.sampler_shared.build_world_slice(chars_for_slice, all_pois[:10])

            task_id = f"task_{self.city_id}_{self.task_type}_{task_idx:04d}"
            city_record = next((city for city in self.world.get("cities", [])
                                if city.get("city_id") == self.city_id), {})
            time_context = assigned_time_context(task_id, self.city_id, city_record.get("timezone", "UTC"))
            task = BenchmarkTask(
                task_id=task_id,
                task_type=self.task_type, difficulty="medium",
                city_id=self.city_id, variant="",
                generated_at=self.generated_at, time_context=time_context, user_prompt=user_prompt,
                summary={
                    "character_count": 1,
                    "place_count": len(all_pois),
                    "kb_city": city_name,
                    "constraint_count": len(matched_ids),
                    "keywords": chosen_keywords,
                    "expected_feasible": gt.get("expected_feasible", True),
                    "public_activity": public_activity,
                    "admission_mode": self.admission_mode,
                },
                world_slice=world_slice,
                world_slice_summary=WorldSliceSummary(
                    characters=chars_for_slice,
                    places=[{"place_id": p.get("place_id", ""), "name": p.get("name", "")}
                            for p in all_pois[:10]],
                    districts=[], corridors=[],
                ),
                involved_env_events=[], env_event_details=[], judge_stages=[],
                metadata={"seed": sub_seed, "task_type": self.task_type,
                          "admission_mode": self.admission_mode},
            )

            write_json(task_folder / "task.json", {
                "task_id": task.task_id, "task_type": self.task_type,
                "difficulty": "medium", "city_id": self.city_id,
                "generated_at": self.generated_at,
                "user_prompt": user_prompt, "summary": task.summary, "time_context": time_context,
            })
            write_json(task_folder / "task_sandbox.json", task_sandbox)
            # Baseline cultural and regional rules remain private construction
            # and Judge constraints.  They are not a CityNewsApp knowledge
            # base for the tested Agent; only later operational changes may be
            # added to CityNewsApp by a Dynamic overlay.
            write_json(task_folder / "static_app_state.json", _region_initial_snapshot())
            write_json(task_folder / "world_slice.json", task.world_slice_summary.to_dict())
            write_json(task_folder / "env_events.json", {"env_events": []})
            write_json(task_folder / "judge_sequence.json", {"stages": []})
            report = self.finalize_task_folder(task_folder)
            if not report.get("valid"):
                batch_skip += 1
                logger.info(
                    f"[{task_id_short}] REJECTED contract | "
                    f"errors={'; '.join(report.get('errors', [])[:3])}"
                )
                continue
            tasks.append(task)
            batch_success += 1
            next_idx += 1

            logger.info(
                f"[{task_id_short}] OK {city_name} | feasible={gt.get('expected_feasible')} | "
                f"c={len(constraint_ids)} kw={','.join(chosen_keywords[:3])} | "
                f"{bt.get('prompt',0)}->{bt.get('completion',0)} tok | "
                f"{bt.get('duration_s',0)}s | "
                f"{route_stats_delta.get('api_requests', 0)} API"
                f"{' | pool_small' if pool_small else ''}"
            )

        total_dur = round(__import__("time").time() - llm_start_time, 1)
        logger.info("━" * 60)
        logger.info(
            f"BATCH SUMMARY  success={batch_success}  skip={batch_skip} "
            f"candidate_attempts={candidate_attempts}/{max_candidate_attempts}"
        )
        logger.info(f"  Tokens: {batch_tokens['prompt']}+{batch_tokens['completion']}={batch_tokens['total']} | "
                     f"LLM: {batch_llm_dur}s | Total: {total_dur}s")
        logger.info(f"  API: {batch_api_calls} route calls")

        route_cache.save()

        from task_module._shared.generation_telemetry import write_generation_telemetry
        write_generation_telemetry(
            self.output_dir, task_type=self.task_type, city_id=self.city_id,
            requested_count=count, generated_count=len(tasks), elapsed_s=total_dur,
            route_cache=route_cache, llm_usage=batch_tokens, skipped_count=batch_skip,
        )

        from task_module._shared.task_contract import canonicalize_batch
        city = (self.world.get("cities") or [{}])[0]
        reports = canonicalize_batch(self.output_dir, self.task_type, self.city_id, city.get("timezone", "UTC"))
        if any(not report["valid"] for report in reports):
            raise ValueError("region task contract validation failed")

        return tasks


def _numeric_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, float | int]:
    return {
        key: round(value - before[key], 4) if isinstance(value, float) else value - before[key]
        for key, value in after.items()
        if isinstance(value, (int, float)) and isinstance(before.get(key), (int, float))
    }


def _has_dynamic_region_successor(
    constraints: list[dict[str, Any]],
    main_pois: list[dict[str, Any]],
    distractors: list[dict[str, Any]],
    route_data: dict[str, Any],
    character: dict[str, Any],
    baseline: dict[str, Any],
) -> bool:
    """Require a different P1 after a legal public availability delta.

    Cultural and administrative constraints are deliberately passed unchanged
    into both solver calls. The only overlay is one LocalServiceApp state.
    """
    p0_ids = list((baseline.get("expected_plan") or {}).get("selected_place_ids", []) or [])
    if not p0_ids:
        return False
    post = solve_region_plan(
        constraints, main_pois, distractors, route_data=route_data, character=character,
        operational_state={"unavailable_place_ids": [p0_ids[0]]},
    )
    p1_ids = list((post.get("expected_plan") or {}).get("selected_place_ids", []) or [])
    return bool(post.get("expected_feasible") and p1_ids and p1_ids != p0_ids)


def _setup_log(path: Path) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stdout),
                                  logging.FileHandler(str(path), encoding="utf-8")])


def _extract_prefix(prompt: str, n: int = 5) -> str:
    chinese = re.findall(r'[\u4e00-\u9fff]', prompt)
    return re.sub(r'[<>:"/\\\\|?*]', '', ''.join(chinese[:n])) or "task"


def _has_named_subject(prompt: str, character: dict[str, Any]) -> bool:
    """Require an explicit sampled-person anchor in the public task wording."""
    name = str(character.get("name") or "").strip()
    if not name or name not in str(prompt):
        return False
    return not bool(re.search(r"(?:\bI\b|\bme\b|\bmy\b|我|我们)", prompt, re.IGNORECASE))


def _build_llm_input_doc(character: dict, city_name: str, scenario: str, public_activity: str,
                         planning_profile: dict[str, Any] | None = None) -> str:
    from task_module.task_types.region_aware.natural_prompt import (
        SYSTEM_REGION_NATURAL, build_natural_user_input,
    )
    return f"=== SYSTEM ===\n{SYSTEM_REGION_NATURAL}\n\n=== USER ===\n{build_natural_user_input(character, city_name, scenario, public_activity, str((planning_profile or {}).get('destination_label') or ''))}"


def _existing_region_decision_ids(tasks_dir: Path) -> set[str]:
    """Read completed sibling decisions so a resumed batch stays unique."""
    identities: set[str] = set()
    for folder in tasks_dir.iterdir() if tasks_dir.exists() else []:
        report_path = folder / "task_validation.json"
        sandbox_path = folder / "task_sandbox.json"
        if not folder.is_dir() or not report_path.exists() or not sandbox_path.exists():
            continue
        try:
            report = read_json(report_path)
            if report.get("state") == "formal":
                identities.add(region_decision_identity(read_json(sandbox_path)))
        except Exception:
            continue
    return identities


def _build_review_projection(city_name: str, public_activity: str, constraints: list[dict[str, Any]],
                             decision_contract: dict[str, Any], expected_plan: dict[str, Any],
                             main_pois: list[dict[str, Any]], distractors: list[dict[str, Any]]) -> dict[str, Any]:
    """Create a reviewer-facing explanation without exposing internal IDs.

    This projection is private task evidence.  It deliberately contains source
    excerpts and human-readable counterfactuals, not evaluator identifiers or
    solver-only field names.
    """
    place_by_id = {
        str(place.get("place_id") or ""): place
        for place in [*main_pois, *distractors] if place.get("place_id")
    }
    selected_ids = list(expected_plan.get("selected_place_ids", []) or [])
    selected = [place_by_id[place_id] for place_id in selected_ids if place_id in place_by_id]
    districts = sorted({str(place.get("district_name") or "") for place in selected if place.get("district_name")})
    evidence_by_id = {
        str(constraint.get("id") or ""): {
            "source": str(constraint.get("source_title") or "Wikivoyage"),
            "section": str(constraint.get("source_section") or ""),
            "quote": str(constraint.get("source_quote") or ""),
            "applicability": _review_applicability(constraint, public_activity, selected),
        }
        for constraint in constraints
    }
    impacts = []
    for item in decision_contract.get("material_constraints", []) or []:
        evidence = evidence_by_id.get(str(item.get("constraint_id") or ""), {})
        impacts.append({
            "dimension": _review_dimension(item.get("changed_dimensions", [])),
            "with_constraint": _review_plan(item.get("with_constraint", {}), place_by_id),
            "without_constraint": _review_plan(item.get("without_constraint", {}), place_by_id),
            "execution_requirements_changed": bool(item.get("execution_requirements_changed")),
            "source": evidence.get("source", "Wikivoyage"),
            "quote": evidence.get("quote", ""),
        })
    return {
        "schema_version": "geoword.region_review_projection.v1",
        "region_context": {
            "city": city_name,
            "districts": districts,
            "activity": _review_activity(public_activity),
        },
        "evidence": list(evidence_by_id.values()),
        "decision_impacts": impacts,
        "complete": bool(impacts) and all(item.get("quote") for item in evidence_by_id.values()),
    }


def _review_applicability(constraint: dict[str, Any], public_activity: str,
                          selected: list[dict[str, Any]]) -> str:
    types = list((constraint.get("hidden_rule", {}) or {}).get("required_place_types", []) or [])
    place_names = [str(place.get("name") or "") for place in selected if place.get("name")]
    if place_names:
        return f"适用于本题的{public_activity}安排及已选地点：{'、'.join(place_names)}"
    if types:
        return f"适用于本题的{public_activity}安排和相关场所类型"
    return f"适用于本题的{public_activity}安排"


def _review_dimension(dimensions: list[Any]) -> str:
    labels = {
        "selected_places": "地点选择",
        "schedule": "时间安排",
        "itinerary_structure": "行程结构",
        "route": "路线选择",
        "travel_mode": "出行方式",
        "duration": "停留时长",
        "participants": "同行安排",
        "service_assignment": "服务分配",
    }
    return "、".join(labels.get(str(value), str(value)) for value in dimensions) or "规划决策"


def _review_plan(plan: dict[str, Any], place_by_id: dict[str, dict[str, Any]]) -> str:
    names = [str(place_by_id.get(str(place_id), {}).get("name") or "")
             for place_id in plan.get("selected_place_ids", []) or []]
    names = [name for name in names if name]
    starts = [str(value) for value in plan.get("starts", []) or [] if value]
    parts = []
    if names:
        parts.append("地点：" + "、".join(names))
    if starts:
        parts.append("时间：" + "、".join(starts))
    return "；".join(parts) or "无可读规划差异"


def _review_activity(activity: str) -> str:
    return {
        "shared_meal": "共同用餐",
        "worship_visit": "宗教场所参访",
        "public_outing": "公共外出",
    }.get(str(activity), str(activity) or "本地规划")


def _template_user_prompt(character: dict, city_name: str, scenario: str, public_activity: str,
                          planning_profile: dict[str, Any] | None = None) -> str:
    activity_text = {
        "shared_meal": "与当地朋友安排一次用餐",
        "worship_visit": "安排一次宗教场所参访",
        "public_outing": "安排一次公共区域短途出行",
    }.get(public_activity, "安排一次本地行程")
    destination_label = str((planning_profile or {}).get("destination_label") or "")
    if destination_label:
        return (
            f"请为{character.get('name', '访客')}在{city_name}安排一次可执行的{public_activity}行程，"
            f"目的地须为真实存在的{destination_label}。请说明具体地点、抵达时间与实际活动步骤，"
            "并自然纳入当地情境，不要单独讲解任何规则。"
        )
    return (
        f"{character.get('name', '访客')}计划前往{city_name}{activity_text}，请根据{scenario or '实际安排'}给出具体地点、抵达时间和可执行的行程顺序。"
        "请将当地情境自然纳入整体安排，不需要单独讲解任何规则。"
    )


def _region_initial_snapshot() -> dict[str, Any]:
    """Return public initial state without projecting private cultural rules."""
    return {
        "schema_version": "geoword.static_app_state.v1",
        "lifecycle": "episode_start_frozen",
        "apps": {},
    }


def _extract_decisions(expected_plan: dict) -> list[dict]:
    events = expected_plan.get("events", [])
    decisions = []
    for e in events:
        cid = e.get("applied_constraint", "")
        reasoning = e.get("reasoning", "")
        pid = e.get("place_id", "")
        if cid and reasoning:
            decisions.append({
                "constraint_id": cid,
                "event_type": e.get("event_type", ""),
                "decision": f"{e.get('time','?')} @ {e.get('place_id','?')}",
                "place_id": pid,
                "reasoning": reasoning,
            })
    return decisions
