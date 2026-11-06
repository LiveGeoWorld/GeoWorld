"""Demo (non-LLM) character generator — validation use only.

Produces fixed-template characters from templates.py.
Coordinates are picked from real anchor_places within each assigned district,
NOT the district center — so characters in the same district get distinct
real-building locations.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.io import read_json, write_json

from world_module.character.character_schema import (
    Character, CharacterPreferences, CharacterMobility,
    CharacterAvailability, CharacterCommunication, CharacterConstraint,
    CharacterSet, Relationship, RelationshipGraph, RelationshipGraphEdge,
    RelationshipGraphNode,
)
from world_module.character.config import CharacterGeneratorConfig
from world_module.character.demo_generator.templates import DEMO_TEMPLATES


class DemoGenerator:
    """Generate fixed-template characters for pipeline validation."""

    def __init__(self, config: CharacterGeneratorConfig | None = None):
        self.config = config or CharacterGeneratorConfig()

    def generate(self) -> CharacterSet:
        """Generate demo characters with embedded cross-relationships."""
        cfg = self.config
        world_path = cfg.world_path
        if not world_path:
            raise ValueError(
                "--world is required. Generate a world first:\n"
                "  python -m world_module.map.cli --seed seeds/tokyo_complex_seed.json"
            )

        print(f"[DemoGenerator] Loading world: {world_path}")
        world = read_json(world_path)

        city = (world.get("cities") or [{}])[0]
        city_id = city.get("city_id", "unknown")
        city_name = city.get("name", city_id)
        districts = world.get("districts", [])
        district_ids = [d["district_id"] for d in districts]
        district_names = [d["name"] for d in districts]
        # Precompute anchor coordinates per district
        district_anchors = _build_anchor_map(districts)
        district_centers = _build_center_map(districts)

        if not districts:
            raise ValueError("world.json has no districts.")

        rng = random.Random(cfg.seed)

        count = min(cfg.count, len(DEMO_TEMPLATES))

        # Step 1: create Character objects
        char_objects: list[Character] = []
        for i in range(count):
            tpl = DEMO_TEMPLATES[i]
            cid = f"char_{city_id}_demo_{i + 1:03d}"
            home_idx = i % len(district_ids)
            work_idx = (i + 3) % len(district_ids)
            home_did = district_ids[home_idx]
            home_lat, home_lng, current_place = _pick_anchor_location(
                home_did, district_anchors, district_centers, rng
            )

            character = Character(
                character_id=cid,
                name=tpl.get("name", f"Demo {i + 1}"),
                name_romanized=tpl.get("name_romanized", ""),
                age=tpl.get("age", 30),
                gender=tpl.get("gender", "unspecified"),
                occupation=tpl.get("occupation", ""),
                personality_traits=tpl.get("personality_traits", []),
                preferences=CharacterPreferences(
                    prefer=tpl.get("prefer", []),
                    avoid=tpl.get("avoid", []),
                    dietary=tpl.get("dietary", ""),
                    budget=tpl.get("budget", "medium"),
                ),
                mobility=CharacterMobility(
                    home_district_id=home_did,
                    home_district_name=district_names[home_idx],
                    work_district_id=district_ids[work_idx],
                    work_district_name=district_names[work_idx],
                    lat=home_lat,
                    lng=home_lng,
                    current_place=current_place,
                    preferred_modes=tpl.get("preferred_modes", ["TRANSIT"]),
                    max_walk_m=tpl.get("max_walk_m", 800),
                ),
                availability=CharacterAvailability(
                    typical_free_windows=tpl.get("typical_free_windows", []),
                    punctuality=tpl.get("punctuality", "medium"),
                ),
                communication=CharacterCommunication(
                    style=tpl.get("communication_style", ""),
                    response_latency=tpl.get("response_latency", "medium"),
                ),
                constraints=[
                    CharacterConstraint(
                        constraint_type="hidden",
                        content=tpl.get("hidden_constraint", ""),
                        reveal_trigger="after_first_plan_received",
                    ),
                ],
                source="demo_template",
            )
            char_objects.append(character)

        # Step 2: build relationships from template relations arrays
        # index → character_id mapping
        id_by_index = {i: c.character_id for i, c in enumerate(char_objects)}
        name_by_id = {c.character_id: c.name for c in char_objects}

        relationships: list[Relationship] = []
        seen_pairs: set[tuple[str, str]] = set()

        for i, c in enumerate(char_objects):
            tpl = DEMO_TEMPLATES[i]
            for rel in tpl.get("relations", []):
                to_idx = rel.get("to_index")
                if to_idx is None or to_idx >= len(char_objects):
                    continue
                target_id = id_by_index[to_idx]
                pair = (c.character_id, target_id)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                relationships.append(Relationship(
                    character_id=c.character_id,
                    related_id=target_id,
                    type=rel.get("type", ""),
                    subtype=rel.get("subtype", ""),
                    strength="strong" if rel.get("type") in ("spouse", "parent", "child", "sibling") else "medium",
                    trust_level="very_high" if rel.get("type") in ("spouse", "parent", "child") else "high",
                    communication_frequency="daily" if rel.get("type") in ("spouse", "parent", "child") else "weekly",
                    context=rel.get("context", ""),
                ))

                # Also add reverse edge for spouse/sibling symmetry
                reverse_type = _reverse_type(rel.get("type", ""))
                if reverse_type != rel.get("type", ""):
                    reverse_pair = (target_id, c.character_id)
                    if reverse_pair not in seen_pairs:
                        seen_pairs.add(reverse_pair)
                        relationships.append(Relationship(
                            character_id=target_id,
                            related_id=c.character_id,
                            type=reverse_type,
                            subtype=_reverse_subtype(rel.get("type", ""), rel.get("subtype", "")),
                            strength="strong" if rel.get("type") in ("spouse", "parent", "child", "sibling") else "medium",
                            trust_level="very_high" if rel.get("type") in ("spouse", "parent", "child") else "high",
                            communication_frequency="daily" if rel.get("type") in ("spouse", "parent", "child") else "weekly",
                            context=rel.get("context", ""),
                        ))

        # Step 3: build relationship graph
        graph_nodes = [
            RelationshipGraphNode(id=c.character_id, name=c.name, type="character")
            for c in char_objects
        ]
        graph_edges = [
            RelationshipGraphEdge(from_id=r.character_id, to_id=r.related_id, type=r.type)
            for r in relationships
        ]
        graph = RelationshipGraph(nodes=graph_nodes, edges=graph_edges)

        # Step 4: assemble result
        generated_at = datetime.now(timezone.utc).isoformat()
        result = CharacterSet(
            city_id=city_id,
            city_name=city_name,
            generated_at=generated_at,
            generation_config={
                "count": count,
                "seed": cfg.seed,
                "world_path": world_path,
                "mode": "demo",
            },
            characters=char_objects,
            relationships=relationships,
            relationship_graph=graph,
        )

        # Write output
        out_path = Path(cfg.output_root) / city_id / "demo" / "characters_validation.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(out_path, result.to_dict())

        print(
            f"[DemoGenerator] Generated {len(char_objects)} DEMO characters, "
            f"{len(relationships)} relationships → {out_path}\n"
            f"  ⚠  These are validation-only templates, NOT production data."
        )
        return result


def _reverse_type(t: str) -> str:
    """Return the reciprocal relationship type."""
    mapping = {
        "parent": "child",
        "child": "parent",
        "spouse": "spouse",
        "sibling": "sibling",
        "friend": "friend",
        "coworker": "coworker",
        "neighbor": "neighbor",
        "classmate": "classmate",
    }
    return mapping.get(t, t)


def _reverse_subtype(t: str, subtype: str) -> str:
    """Return the reciprocal subtype."""
    if t == "parent":
        if "father" in subtype:
            return subtype.replace("father", "son" if "長男" in subtype else "child")
        if "mother" in subtype:
            return subtype.replace("mother", "son" if "長男" in subtype else "child")
        return "child"
    if t == "child":
        if "son" in subtype:
            return "parent"
        return "parent"
    if t == "sibling":
        if "older_brother" in subtype:
            return "younger_brother"
        if "younger_brother" in subtype:
            return "older_brother"
    return subtype


def _build_anchor_map(districts: list[dict]) -> dict[str, list[dict[str, Any]]]:
    """Build district_id → factual anchor places."""
    result = {}
    for d in districts:
        did = d.get("district_id", "")
        anchors = d.get("director_notes", {}).get("anchor_places", [])
        valid = [
            a for a in anchors
            if a.get("location", {}).get("lat") is not None
            and a.get("location", {}).get("lng") is not None
        ]
        if valid:
            result[did] = valid
    return result


def _build_center_map(districts: list[dict]) -> dict[str, tuple[float, float]]:
    """Build district_id → (center_lat, center_lng) for fallback."""
    result = {}
    for d in districts:
        did = d.get("district_id", "")
        geom = d.get("geometry", {})
        gdata = geom.get("data", {}) if isinstance(geom, dict) else {}
        lat = float(gdata.get("lat", 0.0))
        lng = float(gdata.get("lng", 0.0))
        result[did] = (lat, lng)
    return result


def _pick_anchor_location(
    did: str,
    anchor_map: dict[str, list[dict[str, Any]]],
    center_map: dict[str, tuple[float, float]],
    rng: random.Random,
) -> tuple[float, float, dict[str, Any]]:
    """Pick a factual anchor and retain its identity; fallback to district center."""
    anchors = anchor_map.get(did, [])
    if anchors:
        anchor = rng.choice(anchors)
        location = anchor.get("location", {})
        current_place = {
            "source_place_id": str(
                anchor.get("source_place_id")
                or anchor.get("google_place_id")
                or anchor.get("osm_place_id")
                or anchor.get("place_id")
                or ""
            ),
            "name": str(anchor.get("name", "")),
            "address": str(anchor.get("formatted_address") or anchor.get("address") or ""),
            "location": {"lat": float(location["lat"]), "lng": float(location["lng"])},
            "place_type": str(anchor.get("primary_type") or anchor.get("type") or ""),
            "source": "district_anchor",
        }
        return current_place["location"]["lat"], current_place["location"]["lng"], current_place
    lat, lng = center_map.get(did, (0.0, 0.0))
    return lat, lng, {}
