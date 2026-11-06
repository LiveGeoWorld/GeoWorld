"""Parse and validate LLM JSON responses for the 4-step pipeline.

Step 1: parse_region_result  → dict of district_name → count
Step 2: parse_profile_result → list of profile dicts
Step 4: parse_relationship_result → list of Relationship objects

Also: legacy parse_llm_response for backward compat (Yunwu single-pass).
"""

from __future__ import annotations

import re
from typing import Any

from world_module.character.character_schema import (
    Character, Relationship, RelationshipGraph, RelationshipGraphEdge, RelationshipGraphNode,
)


def parse_region_result(raw_text: str) -> dict[str, int]:
    """Parse Step 1 LLM output: {"district_name": N, ...}"""
    data = _extract_json(raw_text)
    result = {}
    for name, count in data.items():
        if isinstance(count, (int, float)):
            result[str(name)] = int(count)
    return result


def parse_profile_result(raw_text: str) -> list[dict[str, Any]]:
    """Parse Step 2 LLM output: list of character profile dicts."""
    data = _extract_json(raw_text)
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict) and "characters" in data:
        return [d for d in data["characters"] if isinstance(d, dict)]
    return []


def parse_relationship_result(
    raw_text: str,
    char_ids: set[str],
    id_remap: dict[str, str],
) -> tuple[list[Relationship], RelationshipGraph]:
    """Parse Step 4 LLM output: relationships with ID remapping."""
    data = _extract_json(raw_text)
    raw_rels = data.get("relationships", []) if isinstance(data, dict) else []

    relationships = []
    for rr in raw_rels:
        if not isinstance(rr, dict):
            continue
        raw_cid = str(rr.get("character_id", ""))
        raw_rid = str(rr.get("related_id", ""))
        cid = id_remap.get(raw_cid, raw_cid)
        rid = id_remap.get(raw_rid, raw_rid)
        subtype = str(rr.get("subtype", ""))
        if subtype.lower() in ("none", "null", ""):
            subtype = ""

        rel = Relationship(
            character_id=cid,
            related_id=rid,
            type=str(rr.get("type", "")),
            subtype=subtype,
            strength=str(rr.get("strength", "medium")),
            trust_level=str(rr.get("trust_level", "medium")),
            communication_frequency=str(rr.get("communication_frequency", "")),
            context=str(rr.get("context", "")),
        )
        if rel.character_id in char_ids and rel.related_id in char_ids:
            relationships.append(rel)

    graph_nodes = []
    graph_edges = [
        RelationshipGraphEdge(from_id=r.character_id, to_id=r.related_id, type=r.type)
        for r in relationships
    ]
    graph = RelationshipGraph(nodes=graph_nodes, edges=graph_edges)
    return relationships, graph


# ── Legacy parser (for Yunwu single-pass, kept for backward compat) ──

def parse_llm_response(
    raw_data: dict[str, Any],
    city_id: str,
    districts: list[dict[str, Any]],
) -> tuple[list[Character], list[Relationship], RelationshipGraph]:
    """Legacy single-pass parser. Kept for non-Azure providers."""
    return [], [], RelationshipGraph()


def _extract_json(text: str) -> Any:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
    import json
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r"[\{\[].*[\}\]]", clean, re.DOTALL)
        if not match:
            raise ValueError("LLM response did not contain JSON:\n" + text[:500])
        return json.loads(match.group(0))
