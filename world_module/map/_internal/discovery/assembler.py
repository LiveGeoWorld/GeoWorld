"""Assemble discovery session checkpoints into the legacy discovery dict."""

from __future__ import annotations

from typing import Any

from world_module.map._internal.discovery.quality import (
    dedupe_named,
    valid_corridor,
    valid_district,
    valid_place_plan,
)


def assemble_discovery(
    *,
    city_outline: dict[str, Any],
    district_docs: list[dict[str, Any]],
    corridor_group_doc: dict[str, Any],
    corridor_docs: list[dict[str, Any]],
    place_docs: list[dict[str, Any]],
    dynamic_storyline_doc: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    caps = policy.get("caps", {})
    max_districts = int(caps.get("max_districts") or 0)
    max_corridors = int(caps.get("max_corridors") or 0)

    districts = dedupe_named(
        [
            district
            for doc in district_docs
            for district in doc.get("district_candidates", [])
            if isinstance(district, dict) and valid_district(district)
        ]
    )
    raw_district_count = len(districts)
    if max_districts > 0:
        districts = districts[:max_districts]

    corridors = dedupe_named(
        [
            corridor
            for doc in corridor_docs
            for corridor in doc.get("corridor_candidates", [])
            if isinstance(corridor, dict) and valid_corridor(corridor)
        ]
    )
    raw_corridor_count = len(corridors)
    if max_corridors > 0:
        corridors = corridors[:max_corridors]

    district_names = {d.get("name") for d in districts}
    place_search_plan = [
        plan
        for doc in place_docs
        for plan in doc.get("place_search_plan", [])
        if isinstance(plan, dict) and valid_place_plan(plan, district_names)
    ]

    source_notes: list[str] = []
    for doc in [city_outline, *district_docs, corridor_group_doc, *corridor_docs, *place_docs, dynamic_storyline_doc]:
        source_notes.extend(str(note) for note in doc.get("source_notes", []) if note)

    return {
        "city_summary": city_outline.get("city_summary", {}),
        "district_candidates": districts,
        "corridor_candidates": corridors,
        "place_search_plan": place_search_plan,
        "dynamic_storylines": dynamic_storyline_doc.get("dynamic_storylines", []),
        "source_notes": source_notes,
        "discovery_policy": {
            **policy,
            "raw_counts": {
                "district_candidates": raw_district_count,
                "corridor_candidates": raw_corridor_count,
            },
            "final_counts": {
                "district_candidates": len(districts),
                "corridor_candidates": len(corridors),
                "place_search_plan": len(place_search_plan),
            },
        },
    }
