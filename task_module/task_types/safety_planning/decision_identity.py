"""Decision identity for frozen choice tasks, independent of author wording."""

import copy
import json

from shapely.geometry import mapping

from .occupancy import scope_geometry


def _ordered(values):
    return sorted(values, key=lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False))


def _conditions(value):
    if isinstance(value, dict):
        return {key: _conditions(item) for key, item in value.items()
                if key not in {"constraint_id", "source_refs", "reason", "description", "label"}}
    if isinstance(value, list):
        return _ordered([_conditions(item) for item in value])
    return value


def decision_identity(decision, context):
    anchors = {item["scope_ref"]: item for item in context["geographic_anchors"]}
    identities = decision["identities"]
    target = decision["target_place_id"]
    if not identities or target not in identities.values() or not decision["events"]:
        raise ValueError("Safety decision identity requires candidates, GT and events")
    event_values = []
    for record in decision["events"]:
        event = record["event"]
        anchor = anchors[event["scope_ref"]]
        event_values.append({
            "geometry": mapping(scope_geometry(anchor, context)), "exposure_basis": anchor["exposure_basis"],
            "active_window": copy.deepcopy(event["active_window"]), "effects": _conditions(event["effects"]),
            "scope_place_ids": sorted(record["scope_place_ids"]),
        })
    character = decision["character"]
    mobility = character.get("mobility") or {}
    return {
        "contract": "safety_choice_identity.v1",
        "character_id": character["character_id"],
        "origin": {key: mobility.get(key) for key in ("lat", "lng")},
        "task_time": copy.deepcopy(decision["task_time"] if "task_time" in decision else decision["activity_window"]),
        "target_place_id": target,
        "distractor_place_ids": sorted(pid for pid in identities.values() if pid != target),
        "constraints": _conditions(decision["public_place_constraints"]),
        "events": _ordered(event_values),
    }
