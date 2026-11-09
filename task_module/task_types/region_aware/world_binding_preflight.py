"""Conservative author-side fact audit before Region task construction.

This module intentionally does *not* construct a user objective, candidate set,
``A_base``, ``A_region``, Gold answer, or benchmark task.  It records only
facts already present in the immutable Wikivoyage rule asset and the selected
world/city/version asset.  The later decision-binding algorithm is therefore a
separate, reviewable step rather than an implicit heuristic in source loading.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "geoword.region_world_binding_preflight.v1"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _rule_source_text(rule: dict[str, Any]) -> str:
    parts: list[str] = [str(rule.get("summary") or "")]
    scope = rule.get("scope") or {}
    for key in ("activity", "place_context", "time_context"):
        value = scope.get(key)
        if value:
            parts.append(str(value))
    for clause in rule.get("clauses") or []:
        if clause.get("statement"):
            parts.append(str(clause["statement"]))
    return "\n".join(parts)


def _active_services(world: dict[str, Any], city_id: str) -> list[dict[str, Any]]:
    places = {
        str(place.get("place_id")): place
        for place in world.get("places") or []
        if place.get("city_id") == city_id
    }
    services: list[dict[str, Any]] = []
    for service in world.get("services") or []:
        place = places.get(str(service.get("place_id")))
        if place is None:
            continue
        services.append({
            "service_id": service.get("service_id"),
            "place_id": place.get("place_id"),
            "name": place.get("name"),
            "location": place.get("location"),
            "address": place.get("address"),
            "service_type": service.get("service_type"),
            "google_types": (service.get("app_projection") or {}).get("google_types") or [],
        })
    return services


def _lexical_name_overlaps(source_text: str, services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return deterministic text overlaps without resolving entity identity.

    A service is not inferred from a category, review, address fragment, or
    semantic similarity.  Short names are excluded because they cause broad
    accidental matches (for example, words such as "bar" or "vue").
    """
    normalized_source = f" {_normalize(source_text)} "
    overlaps: list[dict[str, Any]] = []
    for service in services:
        name = str(service.get("name") or "")
        normalized_name = _normalize(name)
        if len(normalized_name) < 6:
            continue
        if f" {normalized_name} " not in normalized_source:
            continue
        overlaps.append({
            "service_id": service["service_id"],
            "place_id": service["place_id"],
            "name": name,
            "location": service["location"],
            "service_type": service["service_type"],
        })
    return overlaps


def build_world_binding_preflight(
    *, rules_asset: dict[str, Any], screening_asset: dict[str, Any], world: dict[str, Any],
    world_id: str, city_id: str, city_active_version: str,
) -> dict[str, Any]:
    """Create a non-task diagnostic for a single frozen city version."""
    if rules_asset.get("city_id") != city_id or screening_asset.get("city_id") != city_id:
        raise ValueError("Rule and screening assets must be bound to the requested city")

    rule_by_id = {str(rule["rule_id"]): rule for rule in rules_asset.get("rules") or []}
    eligible = [
        row for row in screening_asset.get("screening") or []
        if row.get("status") == "task_source_eligible"
    ]
    services = _active_services(world, city_id)
    type_counts = Counter(str(service.get("service_type") or "unknown") for service in services)
    records: list[dict[str, Any]] = []
    for screening in eligible:
        rule_id = str(screening["rule_id"])
        rule = rule_by_id.get(rule_id)
        if rule is None:
            raise ValueError(f"Screening row references an unknown rule: {rule_id}")
        overlaps = _lexical_name_overlaps(_rule_source_text(rule), services)
        records.append({
            "rule_id": rule_id,
            "rule_category": rule.get("rule_category"),
            "decision_domains": screening.get("decision_domains") or [],
            "source_evidence_refs": rule.get("evidence_refs") or [],
            "lexical_active_service_overlaps": overlaps,
            "binding_status": "requires_decision_binding",
            "reason": (
                "This record proves only source-to-frozen-world facts. No user objective, "
                "candidate set, A_base, A_region, or Gold has been constructed in this stage."
            ),
        })

    overlap_count = sum(bool(record["lexical_active_service_overlaps"]) for record in records)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "author_side_non_task_diagnostic",
        "world_binding": {
            "world_id": world_id,
            "city_id": city_id,
            "city_active_version": city_active_version,
            "world_sha256": _sha256_json(world),
            "rules_sha256": _sha256_json(rules_asset),
            "screening_sha256": _sha256_json(screening_asset),
        },
        "frozen_service_inventory": {
            "service_count": len(services),
            "service_type_counts": dict(sorted(type_counts.items())),
            "represented_fields": ["name", "location", "address", "service_type", "google_types"],
            "not_represented_as_structured_service_facts": [
                "product_inventory", "dish_or_menu", "festival_participation", "local_representativeness",
                "time_specific_opening_exception", "payment_instrument_sale",
            ],
        },
        "summary": {
            "task_source_eligible_rule_count": len(records),
            "rules_with_lexical_active_service_overlap": overlap_count,
            "rules_without_lexical_active_service_overlap": len(records) - overlap_count,
            "world_binding_ready_count": 0,
            "formal_task_count": 0,
        },
        "records": records,
    }


def read_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_atomic(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)
