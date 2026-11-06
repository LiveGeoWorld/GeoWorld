"""Canonical task-folder contract shared by build and test stages."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shared.io import read_json, write_json


TASK_SCHEMA_VERSION = "geoword.task.v1"
EVENT_SCHEMA_VERSION = "geoword.dynamic_events.v1"
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_HIRAGANA_KATAKANA_RE = re.compile(r"[\u3040-\u30ff]")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
_COMMON_RAW_PLACE_TYPES = frozenset({
    "restaurant", "cafe", "coffee_shop", "hotel", "lodging", "hospital",
    "park", "museum", "cinema", "movie_theater", "shopping_mall",
    "department_store", "tourist_attraction", "subway_station", "doctor",
    "medical_clinic", "dentist", "chinese_restaurant", "fast_food_restaurant",
})


def canonicalize_batch(task_root: Path, task_type: str, city_id: str,
                       city_timezone: str = "UTC") -> list[dict[str, Any]]:
    """Normalize every generated task folder and return validation reports."""
    reports = []
    for task_json in task_root.rglob("task.json"):
        reports.append(canonicalize_task_folder(
            task_json.parent, task_type=task_type, city_id=city_id,
            city_timezone=city_timezone,
        ))
    return reports


def canonicalize_task_folder(task_dir: Path, task_type: str, city_id: str,
                             city_timezone: str = "UTC") -> dict[str, Any]:
    """Apply the release contract without changing task semantic ground truth."""
    task_path = task_dir / "task.json"
    task = read_json(task_path)
    resolved_type = task.get("task_type") or task_type
    resolved_city = task.get("city_id") or city_id
    _ensure_prompt_language_metadata(task)
    context = task.get("time_context") or assigned_time_context(
        task.get("task_id", task_dir.name), resolved_city, city_timezone,
    )

    task["schema_version"] = TASK_SCHEMA_VERSION
    task["artifact_contract_version"] = TASK_SCHEMA_VERSION
    task["task_type"] = resolved_type
    task["city_id"] = resolved_city
    task["time_context"] = context
    write_json(task_path, task)

    sandbox_names = ["task_sandbox.json"]
    if resolved_type == "route_planning":
        sandbox_names.append("route_sandbox.json")
    for name in sandbox_names:
        path = task_dir / name
        if not path.exists():
            continue
        sandbox = read_json(path)
        sandbox.setdefault("schema_version", TASK_SCHEMA_VERSION)
        sandbox.setdefault("task_type", resolved_type)
        sandbox.setdefault("task_id", task.get("task_id", ""))
        sandbox["time_context"] = context
        write_json(path, sandbox)

    events_path = task_dir / "env_events.json"
    if resolved_type != "dynamic_replanning":
        # Static capability tasks must not have active overlay events.
        write_json(events_path, {"schema_version": EVENT_SCHEMA_VERSION, "env_events": []})
    elif events_path.exists():
        events = read_json(events_path)
        if isinstance(events, list):
            events = {"schema_version": EVENT_SCHEMA_VERSION, "env_events": events}
        elif isinstance(events, dict):
            events.setdefault("schema_version", EVENT_SCHEMA_VERSION)
        write_json(events_path, events)

    return validate_task_folder(task_dir, resolved_type)


def validate_task_folder(task_dir: Path, task_type: str = "") -> dict[str, Any]:
    """Validate the shared release contract before a task may become formal."""
    errors: list[str] = []
    warnings: list[str] = []
    task_path = task_dir / "task.json"
    if not task_path.exists():
        return {"task_dir": str(task_dir), "valid": False, "errors": ["missing task.json"]}
    task = read_json(task_path)
    actual_type = task.get("task_type") or task_type
    if task.get("schema_version") != TASK_SCHEMA_VERSION:
        errors.append("task.json schema_version missing or unsupported")
    if not task.get("task_id"):
        errors.append("task.json task_id missing")
    if not task.get("city_id"):
        errors.append("task.json city_id missing")
    if not task.get("user_prompt", "").strip():
        errors.append("task.json user_prompt missing")
    errors.extend(validate_task_language_contract(task, public_names=_world_slice_place_names(task_dir), warnings=warnings))
    context = task.get("time_context")
    if not isinstance(context, dict) or not context.get("instruction_time"):
        errors.append("task.json time_context.instruction_time missing")

    sandbox = task_dir / ("route_sandbox.json" if actual_type == "route_planning" else "task_sandbox.json")
    if not sandbox.exists():
        errors.append(f"missing {sandbox.name}")
    if not (task_dir / "world_slice.json").exists():
        errors.append("missing world_slice.json")
    events_path = task_dir / "env_events.json"
    if not events_path.exists():
        errors.append("missing env_events.json")
    else:
        events = read_json(events_path)
        if not isinstance(events, dict) or not isinstance(events.get("env_events"), list):
            errors.append("env_events.json must be an envelope with env_events list")
        elif actual_type != "dynamic_replanning" and events["env_events"]:
            errors.append("static task contains active env_events")
        elif actual_type == "dynamic_replanning":
            errors.extend(_validate_dynamic_event_envelope(events))
    if not (task_dir / "judge_sequence.json").exists():
        errors.append("missing judge_sequence.json")
    if actual_type == "multi_entity" and sandbox.exists():
        private = read_json(sandbox)
        plans = private.get("acceptable_plans", []) if isinstance(private, dict) else []
        if private.get("expected_feasible") is not True or not isinstance(plans, list) or not plans:
            errors.append(
                "multi_entity feasible-only task requires expected_feasible=true and at least one acceptable plan"
            )
    if actual_type == "safety_planning":
        static_state_path = task_dir / "static_app_state.json"
        if not static_state_path.exists():
            errors.append("safety task missing static_app_state.json")
        else:
            static_state = read_json(static_state_path)
            apps = static_state.get("apps", {}) if isinstance(static_state, dict) else {}
            if static_state.get("schema_version") != "geoword.static_app_state.v1":
                errors.append("safety static_app_state schema_version missing or unsupported")
            if static_state.get("lifecycle") != "episode_start_frozen":
                errors.append("safety static_app_state must be episode_start_frozen")
            if not isinstance(apps, dict) or "MapApp" in apps:
                errors.append("safety static_app_state must not mutate MapApp")
            private = read_json(sandbox) if sandbox.exists() else None
            if isinstance(private, dict) and "safety_choice" in private:
                from test_runner.verification.safety_planning.choice_artifacts import validate_choice_folder
                errors.extend(validate_choice_folder(task_dir, task, private, static_state))
            else:
                errors.extend(_validate_legacy_safety_contract(static_state, private))
    if actual_type == "region_aware":
        static_state_path = task_dir / "static_app_state.json"
        if not static_state_path.exists():
            errors.append("region task missing static_app_state.json")
        else:
            static_state = read_json(static_state_path)
            apps = static_state.get("apps", {}) if isinstance(static_state, dict) else {}
            entries = ((apps.get("CityNewsApp") or {}).get("entries") if isinstance(apps, dict) else None)
            if static_state.get("schema_version") != "geoword.static_app_state.v1":
                errors.append("region static_app_state schema_version missing or unsupported")
            if static_state.get("lifecycle") != "episode_start_frozen":
                errors.append("region static_app_state must be episode_start_frozen")
            if not isinstance(apps, dict) or "MapApp" in apps:
                errors.append("region static_app_state must not mutate MapApp")
            # Cultural, regional, and administrative rules are private
            # construction/Judge constraints. A Region task must not pass
            # formalization by exposing a CityNewsApp rule card to the tested
            # Agent. Ordinary non-cultural public notices remain possible.
            if isinstance(entries, list) and any(
                isinstance(entry, dict) and entry.get("info_type") == "regional_guide"
                for entry in entries
            ):
                errors.append("region static_app_state must not expose regional guidance")
        if sandbox.exists():
            private = read_json(sandbox)
            decision = private.get("region_decision") if isinstance(private, dict) else None
            options = decision.get("accepted_options") if isinstance(decision, dict) else None
            if not isinstance(decision, dict) or decision.get("schema_version") != "geoword.region_contextual_choice.v1":
                errors.append("region task requires contextual-choice region_decision GT")
            elif not isinstance(options, list) or not options:
                errors.append("region contextual-choice GT requires accepted public options")
            elif any(not isinstance(item, dict) or not str(item.get("name") or "").strip()
                     or not isinstance(item.get("location"), dict) for item in options):
                errors.append("region contextual-choice GT options require public name and location")
    if actual_type == "dynamic_replanning":
        static_state_path = task_dir / "static_app_state.json"
        proof_request_path = task_dir / "counterfactual_proof_request.json"
        proof_path = task_dir / "counterfactual_proof.json"
        if not static_state_path.is_file():
            errors.append("dynamic task missing static_app_state.json")
        else:
            static_state = read_json(static_state_path)
            apps = static_state.get("apps", {}) if isinstance(static_state, dict) else {}
            if static_state.get("schema_version") != "geoword.static_app_state.v1":
                errors.append("dynamic static_app_state schema_version missing or unsupported")
            if static_state.get("lifecycle") != "episode_start_frozen":
                errors.append("dynamic static_app_state must be episode_start_frozen")
            if not isinstance(apps, dict) or "MapApp" in apps:
                errors.append("dynamic static_app_state must not override MapApp")
        if not proof_request_path.is_file() or not proof_path.is_file():
            errors.append("dynamic task missing counterfactual proof artifacts")
        else:
            try:
                from test_runner.verification.dynamic_replanning.counterfactual_proof import validate_proof
                proof_report = validate_proof(read_json(proof_request_path), read_json(proof_path))
                if not proof_report.get("valid"):
                    errors.extend(f"dynamic counterfactual proof: {item}" for item in proof_report.get("errors", []))
            except Exception as exc:
                errors.append(f"dynamic counterfactual proof unreadable: {exc}")
    return {"task_dir": str(task_dir), "valid": not errors, "errors": errors, "warnings": warnings}


def _validate_legacy_safety_contract(static_state: dict, private: dict | None) -> list[str]:
    errors = []
    apps = static_state.get("apps", {})
    risk_case = static_state.get("risk_case", {}) if isinstance(static_state, dict) else {}
    supported_families = {
        "weather", "mobility", "infrastructure", "environmental", "public_safety",
        "weather_exposure", "traffic_access_risk", "regional_environment_risk",
    }
    supported_apps = {"WeatherApp", "CityNewsApp"}
    if not isinstance(risk_case, dict) or risk_case.get("family") not in supported_families:
        errors.append("safety static_app_state requires a supported environmental risk case")
    elif risk_case.get("app") not in supported_apps:
        errors.append("safety static risk references unsupported App")
    elif risk_case.get("app") not in apps:
        errors.append("safety static risk App has no snapshot section")
    if private is not None:
        private_case = private.get("safety_risk_case", {}) if isinstance(private, dict) else {}
        counterfactual = private.get("counterfactual", {}) if isinstance(private, dict) else {}
        if not _matching_safety_risk_cases(risk_case, private_case):
            errors.append("safety private/static risk cases do not match")
        evidence = private.get("risk_evidence", []) if isinstance(private, dict) else []
        if not _matching_safety_observation_evidence(risk_case, evidence):
            errors.append("safety private risk evidence does not match its observation contract")
        if not isinstance(counterfactual, dict) or not counterfactual.get("decision_changed"):
            errors.append("safety risk must change the counterfactual decision")
    return errors


def _matching_safety_risk_cases(static_case: Any, private_case: Any) -> bool:
    """Keep static App state and private gold bound to one exact risk case."""
    if not isinstance(static_case, dict) or not isinstance(private_case, dict):
        return False
    fields = ("risk_case_id", "family", "variant", "app", "hazard", "mitigation")
    if any(not static_case.get(field) or static_case.get(field) != private_case.get(field) for field in fields):
        return False
    static_contract = static_case.get("observation_contract")
    private_contract = private_case.get("observation_contract")
    if not isinstance(static_contract, dict) or static_contract != private_contract:
        return False
    match = static_contract.get("response_match")
    return bool(
        static_contract.get("tool")
        and isinstance(static_contract.get("arguments"), dict)
        and isinstance(match, dict)
        and match.get("container")
        and match.get("field")
        and match.get("value")
    )


def _matching_safety_observation_evidence(risk_case: Any, evidence: Any) -> bool:
    """Require the Judge's private evidence to describe the same public lookup."""
    if not isinstance(risk_case, dict) or not isinstance(evidence, list):
        return False
    contract = risk_case.get("observation_contract")
    if not isinstance(contract, dict):
        return False
    expected = contract.get("response_match")
    if not isinstance(expected, dict):
        return False
    for card in evidence:
        if not isinstance(card, dict):
            continue
        if (card.get("risk_case_id") == risk_case.get("risk_case_id")
                and card.get("observation_contract") == contract
                and card.get("field") == expected.get("field")
                and card.get("value") == expected.get("value")):
            return True
    return False


def validate_task_language_contract(task: dict[str, Any], *, public_names: set[str] | None = None,
                                    warnings: list[str] | None = None) -> list[str]:
    """Validate agent-visible prose while allowing source-backed proper names.

    ``prompt_language`` governs narrative language and generic place-type
    display. Real names of public anchors and distractors may retain their
    observed spelling, including a different script.
    """
    summary = task.get("summary") if isinstance(task.get("summary"), dict) else {}
    language = _resolved_prompt_language(task)
    prompt = str(task.get("user_prompt") or "")
    if not prompt:
        return []
    if warnings is None:
        warnings = []
    if not language:
        warnings.append("task.json prompt_language missing and narrative locale cannot be inferred")

    visible_prose = _mask_known_public_names(prompt, summary, public_names or set())
    normalized_language = language.lower()
    if normalized_language.startswith("en") and _CJK_RE.search(visible_prose):
        warnings.append("English task user_prompt contains CJK text (possibly a proper name); review only")
    if normalized_language.startswith("zh") and not _CJK_RE.search(visible_prose):
        warnings.append("Chinese task user_prompt does not contain Chinese natural language")

    raw_types = set(_COMMON_RAW_PLACE_TYPES)
    scope = summary.get("candidate_scope") if isinstance(summary.get("candidate_scope"), dict) else {}
    raw_types.update(
        str(value).strip().lower()
        for value in scope.get("allowed_place_types", []) or []
        if isinstance(value, str) and value.strip()
    )
    for raw_type in sorted((value for value in raw_types if "_" in value), key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(raw_type)}(?![A-Za-z0-9_])", visible_prose, re.I):
            return ["Task user_prompt contains a raw English place-type enum"]
    return []


def _ensure_prompt_language_metadata(task: dict[str, Any]) -> None:
    """Persist an explicit locale for legacy family generators.

    New generators should set ``prompt_language`` while writing their task
    specification.  Several established task families predate that field,
    however, and all of them finalize through this shared release boundary.
    Inferring the dominant *narrative* script here makes the release contract
    explicit before validation without treating an arbitrary foreign token as
    the requested locale. A later mixed-language check records a review warning
    for ``Please find a 餐厅`` instead of silently labelling it Chinese.
    """
    summary = task.get("summary")
    if not isinstance(summary, dict):
        summary = {}
        task["summary"] = summary
    language = _resolved_prompt_language(task)
    if not language:
        language = _infer_prompt_language(str(task.get("user_prompt") or ""))
    if language:
        task["prompt_language"] = language
        summary["prompt_language"] = language


def _resolved_prompt_language(task: dict[str, Any]) -> str:
    """Read an explicit locale, or infer one for a read-only legacy audit."""
    summary = task.get("summary") if isinstance(task.get("summary"), dict) else {}
    language = str(summary.get("prompt_language") or task.get("prompt_language") or "").strip()
    return language or _infer_prompt_language(str(task.get("user_prompt") or ""))


def _infer_prompt_language(prompt: str) -> str:
    """Infer only a dominant narrative locale for legacy task writers."""
    cjk_count = len(_CJK_RE.findall(prompt))
    japanese_count = len(_HIRAGANA_KATAKANA_RE.findall(prompt))
    ascii_count = len(_ASCII_LETTER_RE.findall(prompt))
    if japanese_count:
        return "ja-JP"
    # Place names can contain a handful of Latin characters in Chinese prose;
    # require a meaningful English majority before declaring an English task.
    if cjk_count and cjk_count * 3 >= ascii_count:
        return "zh-CN"
    if ascii_count:
        return "en"
    if cjk_count:
        return "zh-CN"
    return ""


def _mask_known_public_names(prompt: str, summary: dict[str, Any], public_names: set[str]) -> str:
    """Mask only source-backed public names before checking narrative locale."""
    names: set[str] = set()
    scope = summary.get("candidate_scope") if isinstance(summary.get("candidate_scope"), dict) else {}
    for value in scope.get("nearby_anchors", []) or []:
        if isinstance(value, str) and len(value.strip()) > 2:
            names.add(value.strip())
    landscape = summary.get("ambiguity_landscape") if isinstance(summary.get("ambiguity_landscape"), dict) else {}
    for candidate in landscape.get("confusable_candidates", []) or []:
        if isinstance(candidate, dict):
            value = str(candidate.get("name") or "").strip()
            if len(value) > 2:
                names.add(value)
    names.update(value.strip() for value in public_names if len(value.strip()) > 2)
    masked = prompt
    for name in sorted(names, key=len, reverse=True):
        masked = re.sub(re.escape(name), " ", masked, flags=re.IGNORECASE)
    return masked


def _world_slice_place_names(task_dir: Path) -> set[str]:
    """Return source-backed public place and district names in the local slice."""
    path = task_dir / "world_slice.json"
    if not path.exists():
        return set()
    try:
        world_slice = read_json(path)
    except Exception:
        return set()
    if not isinstance(world_slice, dict):
        return set()
    names: set[str] = set()
    for collection in ("places", "districts"):
        for entity in world_slice.get(collection, []) or []:
            if not isinstance(entity, dict):
                continue
            name = str(entity.get("name") or "").strip()
            if len(name) > 2:
                names.add(name)
    return names


def assigned_time_context(task_id: str, city_id: str, timezone_name: str) -> dict[str, str]:
    """Deterministically assign a build-time instruction context for legacy generators."""
    digest = hashlib.sha256(f"{city_id}:{task_id}".encode("utf-8")).digest()
    day_offset = int.from_bytes(digest[:2], "big") % 330
    hour = 8 + digest[2] % 10
    minute = (digest[3] % 4) * 15
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
        timezone_name = "UTC"
    start = datetime(2026, 1, 1, tzinfo=zone) + timedelta(
        days=day_offset, hours=hour, minutes=minute,
    )
    end = start + timedelta(hours=4)
    return {
        "timezone": timezone_name,
        "instruction_time": start.isoformat(),
        "episode_start": start.isoformat(),
        "episode_end": end.isoformat(),
        "source": "deterministic_build_assignment_v1",
    }


def _validate_dynamic_event_envelope(envelope: dict[str, Any]) -> list[str]:
    """Validate the replayable Dynamic Replanning event contract."""
    errors: list[str] = []
    if envelope.get("schema_version") != "geoword.dynamic_events.v2":
        return ["dynamic env_events.json must use geoword.dynamic_events.v2"]
    events = envelope.get("env_events", [])
    if not isinstance(events, list):
        return ["dynamic env_events.json must contain an env_events list"]
    if not events:
        return ["dynamic task requires at least one env event"]
    ids = [str(event.get("event_id", "")) for event in events if isinstance(event, dict)]
    if len(ids) != len(events) or not all(ids) or len(set(ids)) != len(ids):
        errors.append("dynamic env event IDs must be present and unique")
    id_set = set(ids)
    graph = {event_id: set() for event_id in id_set}
    for event in events:
        if not isinstance(event, dict):
            errors.append("dynamic env event must be an object")
            continue
        scope = event.get("scope", {})
        activation = event.get("activation", {})
        app = str(event.get("app") or "")
        if not app:
            errors.append(f"dynamic event missing app:{event.get('event_id', '')}")
        elif app == "MapApp":
            errors.append(f"dynamic event cannot modify MapApp:{event.get('event_id', '')}")
        if not isinstance(scope, dict) or not scope.get("type") or not scope.get("id"):
            errors.append(f"dynamic event missing scope:{event.get('event_id', '')}")
        public_scope = event.get("public_scope")
        if not isinstance(public_scope, dict):
            errors.append(f"dynamic event missing public_scope:{event.get('event_id', '')}")
        elif app == "LocalServiceApp":
            location = public_scope.get("location")
            if not public_scope.get("name") or not isinstance(location, dict):
                errors.append(
                    f"LocalServiceApp dynamic event missing public name/location:{event.get('event_id', '')}"
                )
            else:
                try:
                    float(location.get("lat"))
                    float(location.get("lng"))
                except (TypeError, ValueError):
                    errors.append(
                        f"LocalServiceApp dynamic event has invalid public location:{event.get('event_id', '')}"
                    )
        if not isinstance(event.get("state_patch"), dict) or not event.get("state_patch"):
            errors.append(f"dynamic event missing state_patch:{event.get('event_id', '')}")
        mode = activation.get("mode") if isinstance(activation, dict) else ""
        if mode != "absolute":
            errors.append(f"dynamic event must use absolute activation:{event.get('event_id', '')}")
        if not event.get("activation_time", activation.get("time", "")):
            errors.append(f"dynamic absolute event missing activation_time:{event.get('event_id', '')}")
        if not event.get("state_field"):
            errors.append(f"dynamic event missing state_field:{event.get('event_id', '')}")
        if event.get("trigger_step") is not None or event.get("trigger_condition"):
            errors.append(f"dynamic event contains legacy runtime trigger:{event.get('event_id', '')}")
        for parent in event.get("causal_parents", []) or []:
            if parent not in id_set:
                errors.append(f"dynamic event unknown causal parent:{parent}")
            else:
                graph.setdefault(parent, set()).add(event.get("event_id", ""))
    if _graph_has_cycle(graph):
        errors.append("dynamic event causal graph contains cycle")
    return errors


def _graph_has_cycle(graph: dict[str, set[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        cyclic = any(visit(child) for child in graph.get(node, set()))
        visiting.remove(node)
        visited.add(node)
        return cyclic

    return any(visit(node) for node in graph)


# Private-name compatibility for callers introduced before the public helper.
_assigned_time_context = assigned_time_context
