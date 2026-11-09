"""Persist the confirmed choice contract without fabricating legacy risk cases."""

from __future__ import annotations

import copy
import math
from pathlib import Path

from shared.io import read_json, write_json
from task_module.task_types.region_aware.generator import (
    _region_constraint_matches_place, _region_district_names, _region_weekday_index,
    _stage3_route_for, _stage3_route_index,
)
from task_module.task_types.safety_planning.choice_pipeline import prove_choice
from task_module.task_types.safety_planning.evidence_index import SafetyEvidenceIndex
from task_module.task_types.safety_planning.event_contract import public_event
from task_module.task_types.safety_planning.event_view import freeze_event_view, read_event_view
from .task_time import moment, time_fields
from .opening_hours import open_at
from .event_bundle import events


def verify_decision(decision: dict) -> dict:
    """Reproduce current-task C and event arithmetic, never solve prior tasks."""
    places = {p["place_id"]: p for p in decision["world"]["places"]}
    identities = decision["identities"]
    if len(set(identities.values())) != len(identities) or not set(identities.values()) <= places.keys():
        raise ValueError("Safety identity map must bind distinct frozen places")
    activity = decision["activity_place_ids"]
    if len(set(activity)) != len(activity) or not set(activity) <= places.keys():
        raise ValueError("Safety activity candidates must be distinct frozen places")
    constraints = decision["public_place_constraints"]
    context = dict(character=decision["character"], route_index=_stage3_route_index(decision["route_cache"]),
                   district_names=_region_district_names(decision["world"]),
                   weekday_index=_region_weekday_index(decision["time_context"]))
    ordinary = {pid for pid in activity if all(_region_constraint_matches_place(places[pid], c, **context) for c in constraints)}
    if "task_time" in decision:
        if decision["time_context"]["instruction_time"] != decision["task_time"]:
            raise ValueError("Safety task instant differs from its time context")
        ordinary = {pid for pid in ordinary if open_at(places[pid], decision["task_time"]) is True}
    refs = decision["proof"]["ordinary_candidates"]
    if set(refs) != set(identities) or {identities[r] for r in refs} != ordinary:
        raise ValueError("Safety proof omits or adds candidates retained by ordinary C")
    target = next((ref for ref, pid in identities.items() if pid == decision["target_place_id"]), None)
    if target is None:
        raise ValueError("Safety fixed target is missing from ordinary candidates")
    records = decision["events"]
    multi = decision.get("event_contract") in {"safety_events.v1", "safety_events.v2"}
    if decision.get("event_contract") == "safety_events.v2":
        if len(records) != len(identities) - 1 or not records:
            raise ValueError("Event count must equal distractor count")
    elif not 1 <= len(records) <= (2 if multi else 1):
        raise ValueError("Safety choice has an invalid event count")
    record = records[0]
    scope = set(record["scope_place_ids"])
    if any(not set(r["scope_place_ids"]) <= ordinary for r in records):
        raise ValueError("Safety scope mapping references non-candidates")
    routes = {}
    for ref, pid in identities.items():
        routes[ref] = {}
        for c in constraints:
            if c["type"] == "route_duration_range":
                mode = c["transportation_mode"]
                entry = _stage3_route_for(context["route_index"], decision["character"], places[pid], mode)
                raw = ((entry or {}).get("route") or {}).get("duration_s")
                if raw is None:
                    raise ValueError("Safety route constraint has no frozen route")
                routes[ref][mode] = max(1, math.ceil(float(raw) / 60))
    proof = prove_choice(target_ref=target, ordinary_refs=refs, event={"events": [r["event"] for r in records]} if multi else record["event"],
        profiles=[{"candidate_ref": ref, "facts": decision["profiles_by_place_id"][pid]} for ref, pid in identities.items()],
        scope_members=[{ref for ref, pid in identities.items() if pid in r["scope_place_ids"]} for r in records] if multi else {ref for ref, pid in identities.items() if pid in scope},
        activity_window=moment(decision), constraints=constraints, route_minutes=routes)
    if proof != decision["proof"]:
        raise ValueError("Safety stored proof differs from its frozen current-task facts")
    return proof


def build_choice_artifacts(*, task_id: str, city_id: str, generated_at: str, time_context: dict,
                           choice: dict, evidence_index: SafetyEvidenceIndex, identities: dict,
                           world: dict, character: dict, activity_place_ids: list[str],
                           route_cache: dict, metadata: dict) -> dict[str, dict]:
    entry = next((r for r in evidence_index.entries if r["owner"] == task_id), None)
    if entry is None or entry != choice["spacetime_evidence"] or entry["event"] != choice["event"]:
        raise ValueError("Safety task must bind its completed source-evidence record")
    context = entry["context"]
    refs = choice["proof"]["ordinary_candidates"]
    if set(identities) != set(refs):
        raise ValueError("Safety artifact identity map must cover exactly the ordinary candidates")
    target_ref, = choice["proof"]["accepted_candidates"]
    writing = choice["writer_input"]
    multi = "events" in choice["event"]
    event_items = events(choice["event"])
    notices = choice["public_events"] if multi else [choice["public_event"]]
    memberships = choice["scope_candidate_refs"] if multi else [choice["scope_candidate_refs"]]
    app_states = {}
    for item, notice in zip(event_items, notices):
        app_states.setdefault(item["app"], {"entries": []})["entries"].append(notice)
    if writing["public_place_constraints"] != context["public_place_constraints"]:
        raise ValueError("Writer and frozen decision constraints differ")
    decision = {
        "target_place_id": identities[target_ref], "identities": identities,
        "world": world, "character": character, "task_context": writing["task_context"],
        "activity_intent": writing["activity_intent"], "activity_place_ids": activity_place_ids,
        **time_fields(context), "time_context": time_context,
        "public_place_constraints": writing["public_place_constraints"], "proof": choice["proof"],
        "profiles_by_place_id": {identities[row["candidate_ref"]]: row["facts"] for row in context["candidate_profiles"]},
        **({"event_contract": "safety_events.v2" if context.get("protection_contract") in {"safety_occupancy.v4", "safety_occupancy.v5", "safety_occupancy.v6"} else "safety_events.v1"} if multi else {}),
        "events": [{"event": item, "public_event": notice,
                    "scope_place_ids": [identities[r] for r in members]}
                   for item, notice, members in zip(event_items, notices, memberships)],
        "route_cache": route_cache,
    }
    verify_decision(decision)
    view = freeze_event_view(world_binding=evidence_index.world_binding, evidence_index=evidence_index)
    writer_call = next(call for call in reversed(choice["model_calls"]) if call["role"] == "writer")
    artifacts = {
        "task.json": {"schema_version": "geoword.task.v1", "task_id": task_id, "task_type": "safety_planning",
                      "city_id": city_id, "generated_at": generated_at, "difficulty": "medium", "time_context": time_context,
                      "user_prompt": choice["user_prompt"], "prompt_language": "en",
                      "metadata": {**metadata, "generation_origin": "current_flow"},
                      "summary": {"character_count": 1, "place_count": len(refs), "risk_family": ",".join(dict.fromkeys(e["event_kind"] for e in event_items))}},
        "task_sandbox.json": {"schema_version": "geoword.task.v1", "task_id": task_id, "task_type": "safety_planning",
                              "time_context": time_context, "safety_choice": decision,
                              "safety_event_view": {"path": "safety_event_view.json", "world_binding": evidence_index.world_binding}},
        "static_app_state.json": {"schema_version": "geoword.static_app_state.v1", "lifecycle": "episode_start_frozen",
                                  "apps": app_states},
        "safety_event_view.json": view,
        "safety_evidence_history.json": evidence_index.to_dict(),
        "world_slice.json": {"characters": [{"character_id": character["character_id"], "name": character.get("name", "")}],
                             "places": [p for p in world["places"] if p["place_id"] in identities.values()]},
        "env_events.json": {"schema_version": "geoword.dynamic_events.v1", "env_events": []},
        "judge_sequence.json": {"stages": []},
        "generation_log/details.json": {"proof": choice["proof"], "spacetime_evidence": entry,
                                        "model_calls": choice["model_calls"]},
        "generation_log/writer_messages.json": {"messages": writer_call["messages"]},
    }
    return {name: copy.deepcopy(payload) for name, payload in artifacts.items()}


def write_choice_artifacts(task_dir: Path, artifacts: dict[str, dict]) -> None:
    task_dir.mkdir(parents=True, exist_ok=False)
    for relative, payload in artifacts.items():
        path = (task_dir / relative).resolve()
        if not path.is_relative_to(task_dir.resolve()):
            raise ValueError("Safety artifacts must stay inside their task folder")
        write_json(path, payload)


def validate_choice_folder(task_dir: Path, task: dict, private: dict, static_state: dict) -> list[str]:
    try:
        decision = private["safety_choice"]
        verify_decision(decision)
        if decision["time_context"] != task["time_context"]:
            raise ValueError("Safety task time context differs from the frozen decision")
        binding = private["safety_event_view"]
        relative = Path(binding["path"])
        view_path = (task_dir / relative).resolve()
        if relative.is_absolute() or not view_path.is_relative_to(task_dir.resolve()):
            raise ValueError("Safety event view must stay task-local")
        apps = read_event_view(read_json(view_path), world_binding=binding["world_binding"])
        index = SafetyEvidenceIndex.from_dict(read_json(task_dir / "safety_evidence_history.json"), world_binding=binding["world_binding"])
        entry = next(r for r in index.entries if r["owner"] == task["task_id"])
        context = entry["context"]
        if time_fields(context) != time_fields(decision) or context["public_place_constraints"] != decision["public_place_constraints"]:
            raise ValueError("Safety decision differs from its source-bound history")
        for row in context["candidate_profiles"]:
            if decision["profiles_by_place_id"][decision["identities"][row["candidate_ref"]]] != row["facts"]:
                raise ValueError("Safety exposure facts differ from history")
        items = events(entry["event"])
        if len(items) != len(decision["events"]):
            raise ValueError("Decision event count differs from history")
        for item, record in zip(items, decision["events"]):
            anchor = next(a for a in context["geographic_anchors"] if a["scope_ref"] == item["scope_ref"])
            exposure_key = "route_exposure" if anchor.get("exposure_basis") == "route" else "place_exposure"
            if exposure_key in item:
                expected_members = {decision["identities"][r["candidate_ref"]]
                                    for r in item[exposure_key] if r["status"] == "intersects"}
                if set(record["scope_place_ids"]) != expected_members:
                    raise ValueError("Decision scope membership differs from verified event exposure")
            expected = public_event(item, anchor)
            if item != record["event"] or record["public_event"] != expected:
                raise ValueError("Safety public notice differs from the frozen event")
            app = item["app"]
            if expected not in apps[app] or expected not in static_state["apps"][app]["entries"]:
                raise ValueError("Safety task-defining notice is absent from the public App data")
        return []
    except (ValueError, KeyError, TypeError, StopIteration, OSError) as exc:
        return [f"safety choice contract: {exc}"]
