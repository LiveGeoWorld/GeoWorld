"""Freeze a public Safety event view from private source-bound event history."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from task_module.task_types.safety_planning.event_contract import public_event
from task_module.task_types.safety_planning.evidence_index import SafetyEvidenceIndex
from .event_bundle import records


def freeze_event_view(*, world_binding: str, evidence_index: SafetyEvidenceIndex) -> dict[str, Any]:
    """Consume completed author records; do not choose or infer event content."""
    if evidence_index.world_binding != world_binding:
        raise ValueError("Event view cannot mix frozen world versions")
    apps: dict[str, list[dict[str, Any]]] = {"WeatherApp": [], "CityNewsApp": []}
    for record in records(evidence_index.entries):
        event = record["event"]
        if event["app"] not in apps:
            raise ValueError("Unsupported Safety event App")
        anchor = next(row for row in record["context"]["geographic_anchors"] if row["scope_ref"] == event["scope_ref"])
        apps[event["app"]].append(public_event(event, anchor))
    payload = {"schema_version": "safety_public_event_view.v1", "world_binding": world_binding,
               "apps": apps}
    payload["content_hash"] = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    return copy.deepcopy(payload)


def read_event_view(payload: dict[str, Any], *, world_binding: str) -> dict[str, list[dict]]:
    value = copy.deepcopy(payload)
    supplied_hash = value.pop("content_hash", None)
    actual = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()
    if (supplied_hash != actual or value.get("schema_version") != "safety_public_event_view.v1"
            or value.get("world_binding") != world_binding):
        raise ValueError("Invalid or mismatched frozen Safety event view")
    return value["apps"]
