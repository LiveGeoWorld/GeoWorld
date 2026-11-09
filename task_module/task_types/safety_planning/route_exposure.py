"""Source-bound private route membership supplied by the event authoring Agent."""

from __future__ import annotations

import copy
from typing import Any


def route_evidence_input(
    geography: dict[str, Any], candidate_routes: dict[str, str],
) -> list[dict[str, Any]]:
    """Project only selected routes, preserving original cache source references."""
    routes = {row["source_ref"]: row for row in geography["routes"]}
    result = []
    for candidate, source_ref in candidate_routes.items():
        if source_ref not in routes:
            raise ValueError("Candidate route is absent from frozen geographic evidence")
        source = routes[source_ref]
        result.append({
            "candidate_ref": candidate, "source_ref": source_ref,
            "mode": source["mode"], "route": copy.deepcopy(source["route"]),
            "steps": copy.deepcopy(source["steps"]),
        })
    return result


def resolve_route_exposure(event: dict[str, Any], context: dict[str, Any]) -> set[str]:
    """Validate coverage and citations, not the LLM's geographic interpretation.

Both positive and negative membership need route evidence. Unknown membership
must never silently become an unaffected candidate in the unique-GT proof.
"""
    evidence = context["route_evidence"]
    routes = {row["candidate_ref"]: row for row in evidence}
    candidates = {row["candidate_ref"] for row in context["candidate_profiles"]}
    if set(routes) != candidates or len(routes) != len(evidence):
        raise ValueError("Route evidence must cover every ordinary candidate exactly once")
    rows = event.get("route_exposure")
    if not isinstance(rows, list):
        raise ValueError("Event Agent must return private route exposure")
    seen = set()
    members = set()
    delay_modes = {effect["transportation_mode"] for effect in event["effects"]
                   if effect["kind"] == "delay"}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Route exposure must be an object")
        ref = row.get("candidate_ref")
        if not isinstance(ref, str) or ref not in routes or ref in seen:
            raise ValueError("Unknown or duplicate route exposure candidate")
        seen.add(ref)
        if row.get("scope_ref") != event["scope_ref"]:
            raise ValueError("Route exposure must refer to the generated event scope")
        route = routes[ref]
        if delay_modes and delay_modes != {route["mode"]}:
            raise ValueError("Route exposure evidence must use the affected transportation mode")
        status = row.get("status")
        if status not in {"intersects", "disjoint", "unknown"}:
            raise ValueError("Route exposure status is invalid")
        if status == "unknown":
            raise ValueError("Unknown route exposure cannot prove the fixed GT")
        allowed = {route["source_ref"], *(step["source_ref"] for step in route["steps"])}
        refs = row.get("source_refs")
        if not isinstance(refs, list) or not refs or any(
            not isinstance(item, str) or item not in allowed for item in refs
        ):
            raise ValueError("Route exposure requires the candidate's own frozen route citations")
        if not isinstance(row.get("reason"), str) or not row["reason"].strip():
            raise ValueError("Route exposure requires a geographic explanation")
        if status == "intersects":
            members.add(ref)
    if seen != candidates:
        raise ValueError("Route exposure must cover every ordinary candidate")
    return members
