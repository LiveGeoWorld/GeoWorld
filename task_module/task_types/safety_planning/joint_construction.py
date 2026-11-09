"""Event and protection authoring for already selected ordinary requirements."""

import copy
import math

from task_module.task_types.safety_planning.option_selection import public_constraint
from task_module.task_types.safety_planning.protection import validate_protections
from task_module.task_types.safety_planning.event_system import EVENT_AUTHOR_SYSTEM
from task_module.task_types.safety_planning.occupancy_system import OCCUPANCY_EVENT_SYSTEM, POINT_EVENT_SYSTEM
from .task_time import OCCUPANCY_CONTRACTS
from .event_bundle import events, materialize
from .multi_event_system import MULTI_EVENT_SYSTEM
from .construction_errors import ConstructionRejected


JOINT_SYSTEM = EVENT_AUTHOR_SYSTEM


def construct_pair(agents, *, option, identities, target_id, context, evidence_index,
                   event_generation_window, map_renderer, failures):
    option = copy.deepcopy(option)
    if (option["target_place_id"] != target_id or target_id not in option["ordinary_place_ids"]
            or len(option["ordinary_place_ids"]) < 2):
        raise ValueError("Selected C must retain fixed GT and at least one competitor")
    if set(identities.values()) != set(option["ordinary_place_ids"]):
        raise ValueError("Event input must contain exactly the C-retained candidates")
    payload = copy.deepcopy(context)
    if payload.get("protection_contract") in {"safety_occupancy.v5", "safety_occupancy.v6"}:
        expected_target = next(ref for ref, pid in identities.items() if pid == target_id)
        if payload.get("construction_context", {}).get("fixed_target_ref") != expected_target:
            raise ValueError("Event protection context differs from the fixed GT")
    if {p["candidate_ref"] for p in payload["candidate_profiles"]} != set(identities):
        raise ValueError("Event profiles must match the C-retained candidates")
    payload["public_place_constraints"] = [public_constraint(c) for c in option["constraints"]]
    occupancy = payload.get("protection_contract") in OCCUPANCY_CONTRACTS
    payload.setdefault("protection_contract", "safety_state_protection.v1")
    payload = evidence_index.prepare(payload, event_generation_window=event_generation_window)
    payload.pop("time_allocation", None)
    payload["available_route_modes"] = sorted({r["mode"] for r in payload.get("route_evidence", [])})
    payload["previous_attempts"] = [
        {key: copy.deepcopy(row[key]) for key in ("reason", "failure_kind", "cluster_id") if key in row}
        for row in failures
    ]
    feedback = [copy.deepcopy(row["conflict_details"])
                for row in failures if row.get("conflict_details")]
    if feedback:
        payload["spatial_conflict_feedback"] = feedback[-1]
    for route in payload.get("route_evidence", []):
        route["baseline_minutes"] = max(1, math.ceil(route["route"]["duration_s"] / 60))
    if occupancy:
        from .occupancy import conflicts
        blocked = conflicts(payload)
        if blocked:
            raise ValueError("Overlapping candidate reached event gate")
        # Keep only minimal geometry feedback for the next event repair call.
        payload.pop("previous_attempts", None)
    images = map_renderer(payload)
    if payload.get("protection_contract") in {"safety_occupancy.v5", "safety_occupancy.v6"}:
        from .gt_protection import required_routes
        required_routes(payload)
    system = POINT_EVENT_SYSTEM if "task_time" in payload else OCCUPANCY_EVENT_SYSTEM
    if payload.get("protection_contract") in {"safety_occupancy.v3", "safety_occupancy.v4", "safety_occupancy.v5", "safety_occupancy.v6"}:
        system = MULTI_EVENT_SYSTEM
    answer = agents.call("event", system if occupancy else JOINT_SYSTEM, payload, images=images)
    if answer.get("cannot_construct") is True:
        raise ConstructionRejected("Safety pairing unavailable: " + str(answer.get("reason", "")))
    if payload.get("protection_contract") in {"safety_occupancy.v3", "safety_occupancy.v4", "safety_occupancy.v5", "safety_occupancy.v6"}:
        selected = materialize(answer, payload)
        validate_protections(answer, selected)
        option["protection_contract"] = payload["protection_contract"]
        return option, answer
    if set(answer) != {"event"}:
        raise ValueError("Event Agent must return only event, without overriding selected C")
    authored = answer.get("event")
    if not isinstance(authored, dict):
        raise ValueError("Joint pairing requires a complete event")
    if authored.get("scope_ref") not in {a["scope_ref"] for a in context["geographic_anchors"]}:
        raise ValueError("Pairing requires a supplied scope reference")
    selected = pair_context(payload, option, authored, identities)
    validate_protections(authored, selected)
    option["protection_contract"] = payload["protection_contract"]
    option["pairing_scope_ref"] = authored["scope_ref"]
    return option, authored


def pair_context(context, option, event, identities, *, event_generation_window=None):
    """Retain the selected pair's dependencies with their original source refs."""
    prepared = copy.deepcopy(context)
    if event_generation_window is not None:
        if "event_generation_window" in prepared and prepared["event_generation_window"] != event_generation_window:
            raise ValueError("Pair context event window differs from the allocated window")
        prepared["event_generation_window"] = copy.deepcopy(event_generation_window)
    result = materialize(event, prepared)
    if option.get("protection_contract"):
        result["protection_contract"] = option["protection_contract"]
    if result.get("protection_contract"):
        # Keep the original screening envelope stable after selecting C and on replay.
        result.setdefault("screening_anchors", copy.deepcopy(context["geographic_anchors"]))
        result.setdefault("screening_has_routes", bool(context.get("route_evidence")))
    selected_ids = set(option["ordinary_place_ids"])
    selected_refs = {ref for ref, pid in identities.items() if pid in selected_ids}
    modes = {c["transportation_mode"] for c in option["constraints"] if c.get("transportation_mode")}
    for key in ("candidate_profiles", "place_evidence"):
        result[key] = [r for r in result.get(key, []) if r["candidate_ref"] in selected_refs]
    result["places"] = [r for r in result.get("places", []) if r["place_id"] in selected_ids]
    used_sources = {ref for item in events(event) for key in ("protected_states", "route_exposure")
                    for row in item.get(key, []) for ref in row.get("source_refs", [])}
    result["route_evidence"] = [r for r in result.get("route_evidence", [])
                                if r["candidate_ref"] in selected_refs and (r["mode"] in modes or
                                    used_sources.intersection({r["source_ref"], *(s["source_ref"] for s in r.get("steps", []))}))]
    refs = {ref for row in result["place_evidence"] for ref in row["source_refs"]}
    for route in result["route_evidence"]:
        refs.add(route["source_ref"])
        refs.update(step["source_ref"] for step in route["steps"])
    protected_scopes = {ref for item in events(event) for row in item.get("protected_states", []) for ref in row.get("scope_refs", [])}
    protected_scopes.update(ref for row in result.get("compiled_gt_protection", []) for ref in row["scope_refs"])
    event_scopes = {item.get("scope_ref") for item in events(event)}
    result["geographic_anchors"] = [a for a in result["geographic_anchors"]
                                    if a["scope_ref"] in event_scopes
                                    or a["scope_ref"] in protected_scopes
                                    or set(a["source_refs"]) & refs]
    result["constrained_modes"] = sorted(modes)
    result["public_place_constraints"] = copy.deepcopy(option["constraints"])
    return result
