"""Match frozen service capabilities without model-inferred activity semantics."""

from __future__ import annotations

from typing import Any


def match_activity(*, places: list[dict[str, Any]], services: list[dict[str, Any]],
                   target_id: str, selected_service_type: str) -> dict[str, Any]:
    if not selected_service_type:
        raise ValueError("Safety activity requires the sampled service type")
    identities = {f"candidate_{i}": p["place_id"] for i, p in enumerate(places, 1)}
    if len(set(identities.values())) != len(places) or target_id not in identities.values():
        raise ValueError("Safety activity requires distinct POIs including the fixed target")
    by_place: dict[str, list[dict[str, Any]]] = {}
    for service in services:
        by_place.setdefault(service.get("place_id", ""), []).append(service)
    assessments = []
    for ref, pid in identities.items():
        evidence = []
        # Both POI capabilities and the paired service records are frozen facts.
        place = places[len(assessments)]
        for source, record in [(f"place:{pid}", place)] + [
            (f"service:{s.get('service_id', pid)}", s) for s in by_place.get(pid, [])
        ]:
            for field in ("service_types", "primary_service_type", "service_type"):
                value = record.get(field)
                matches = (selected_service_type in value if isinstance(value, list)
                           else selected_service_type == value)
                if matches:
                    evidence.append({"source_ref": source, "field": field, "value": value})
        assessments.append({"candidate_ref": ref, "supports_activity": bool(evidence),
                            "evidence": evidence})
    activity_ids = [identities[r["candidate_ref"]] for r in assessments if r["supports_activity"]]
    if target_id not in activity_ids:
        raise ValueError("Fixed target does not support the sampled service type")
    return {"method": "frozen_service_type_match", "target_place_id": target_id,
            "activity_intent": {"service_type": selected_service_type},
            "identities": identities, "activity_place_ids": activity_ids,
            "assessments": assessments}
