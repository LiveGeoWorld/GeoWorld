"""World object sampler — selects characters, places, services for a task.

Given world.json + characters.json + a scenario type, samples a
coherent subset of world objects to build a task world_slice.
"""

from __future__ import annotations

import random
from typing import Any

from shared.place_type_catalog import category_matches


class TaskSampler:
    """Sample world objects for task assembly."""

    def __init__(self, world: dict[str, Any], characters: dict[str, Any], seed: int = 42):
        self.world = world
        self.characters = characters
        self.rng = random.Random(seed)

    def sample_characters(
        self,
        scenario_type: dict[str, Any],
        count: int | None = None,
    ) -> list[dict[str, Any]]:
        """Sample characters, preferring those with relevant relationships.

        When count=1: pick randomly from all characters (no relationship bias).
        """
        chars = list(self.characters.get("characters", []))
        if not chars:
            return []

        min_c = scenario_type.get("min_chars", 2)
        max_c = scenario_type.get("max_chars", 5)
        preferred_roles = scenario_type.get("preferred_roles", [])

        if count is None:
            count = self.rng.randint(min_c, min(max_c, len(chars)))

        # Single character: random pick, no relationship bias
        if count == 1:
            return [self.rng.choice(chars)]

        # Build relationship graph for connected character selection
        relationships = self.characters.get("relationships", [])
        rel_graph: dict[str, set[str]] = {}
        for r in relationships:
            src = r.get("character_id", "")
            tgt = r.get("related_id", "")
            if src and tgt:
                rel_graph.setdefault(src, set()).add(tgt)
                rel_graph.setdefault(tgt, set()).add(src)

        # If we have relationships, prefer connected characters
        if rel_graph and len(chars) >= count:
            # Find a character with many connections
            start_chars = sorted(chars, key=lambda c: len(rel_graph.get(c.get("character_id", ""), set())), reverse=True)
            if start_chars:
                used_ids: set[str] = set()
                selected: list[dict[str, Any]] = []

                # Start with a randomly chosen character (not always the most connected)
                selected.append(self.rng.choice(chars))
                used_ids.add(start_chars[0].get("character_id", ""))

                # Add connected characters
                while len(selected) < count:
                    candidates = []
                    for sc in selected:
                        for neighbor_id in rel_graph.get(sc.get("character_id", ""), set()):
                            if neighbor_id not in used_ids:
                                neighbor = _find_char(chars, neighbor_id)
                                if neighbor:
                                    candidates.append(neighbor)
                                    break
                        if candidates:
                            break
                    if candidates:
                        picked = self.rng.choice(candidates)
                        selected.append(picked)
                        used_ids.add(picked.get("character_id", ""))
                    else:
                        # Fall back to random selection
                        remaining = [c for c in chars if c.get("character_id") not in used_ids]
                        if not remaining:
                            break
                        picked = self.rng.choice(remaining)
                        selected.append(picked)
                        used_ids.add(picked.get("character_id", ""))

                return selected[:count]

        # No relationships: random sampling
        return self.rng.sample(chars, min(count, len(chars)))

    def sample_places(
        self,
        characters: list[dict[str, Any]],
        service_types: list[str],
        count: int = 4,
    ) -> list[dict[str, Any]]:
        """Sample places near characters' home districts, matching service types."""
        places = self.world.get("places", [])
        services = self.world.get("services", [])

        if not places:
            return []

        # Get character district IDs
        char_districts: set[str] = set()
        for c in characters:
            did = c.get("mobility", {}).get("home_district_id", "")
            if did:
                char_districts.add(did)

        # Find services matching required types
        matching_service_ids: set[str] = set()
        for svc in services:
            if any(category_matches(requested, str(svc.get("service_type", ""))) for requested in service_types):
                matching_service_ids.add(svc.get("place_id", ""))

        # Find places in character districts with matching services
        candidates: list[dict[str, Any]] = []
        other_places: list[dict[str, Any]] = []
        for p in places:
            pid = p.get("place_id", "")
            if pid in matching_service_ids:
                if p.get("district_id") in char_districts:
                    candidates.append(p)
                else:
                    other_places.append(p)

        # Prefer character district places, fall back to others
        pool = candidates if candidates else other_places
        if not pool:
            return places[:count]

        result = self.rng.sample(pool, min(count, len(pool)))
        return result

    def sample_env_events(
        self,
        all_env_events: list[Any],
        scenario_type: dict[str, Any],
        characters: list[dict[str, Any]],
        places: list[dict[str, Any]],
        count: int = 4,
    ) -> list[Any]:
        """Sample environment events relevant to the selected characters and places."""

        # From task_module.task_schema import is too verbose at runtime,
        # so we work with dict-like objects using getattr/get
        if not all_env_events:
            return []

        char_ids = {c.get("character_id", "") for c in characters}
        district_ids: set[str] = set()
        for p in places:
            did = p.get("district_id", "")
            if did:
                district_ids.add(did)
        place_ids = {p.get("place_id", "") for p in places}

        relevant_cats = scenario_type.get("env_event_categories", [])

        scored: list[tuple[int, Any]] = []
        for evt in all_env_events:
            scope_id = _attr(evt, "scope_id") or ""
            category = _attr(evt, "category") or ""
            scope_type = _attr(evt, "scope_type") or ""

            score = 0
            # Category match
            if category in relevant_cats:
                score += 3
            # Scope matches selected objects
            if scope_id in char_ids or scope_id in place_ids or scope_id in district_ids:
                score += 5
            # Actor scope matches selected characters
            if scope_type == "actor" and scope_id in char_ids:
                score += 5

            if score > 0:
                scored.append((score, evt))

        scored.sort(key=lambda x: -x[0])
        selected = [evt for _, evt in scored[:count]]
        if len(selected) < count:
            # Add random events to reach count
            remaining = [evt for evt in all_env_events
                         if _attr(evt, "node_id") not in {_attr(s, "node_id") for s in selected}]
            if remaining:
                extra = self.rng.sample(remaining, min(count - len(selected), len(remaining)))
                selected.extend(extra)

        return selected

    def build_world_slice(
        self,
        characters: list[dict[str, Any]],
        places: list[dict[str, Any]],
        services: list[dict[str, Any]] | None = None,
    ) -> dict[str, list[str]]:
        """Build a world_slice dict from selected objects."""
        all_services = self.world.get("services", [])

        place_ids = [p.get("place_id", "") for p in places]
        district_ids: set[str] = set()
        corridor_ids: set[str] = set()
        for p in places:
            did = p.get("district_id", "")
            if did:
                district_ids.add(did)
            for cid in p.get("near_corridor_ids", [])[:2]:
                corridor_ids.add(cid)
        for c in characters:
            did = c.get("mobility", {}).get("home_district_id", "")
            if did:
                district_ids.add(did)

        # Find services for the selected places
        service_matches = []
        if services:
            service_matches = services
        else:
            for svc in all_services:
                if svc.get("place_id") in place_ids:
                    service_matches.append(svc)

        service_ids = [s.get("service_id", "") for s in service_matches[:4]]
        character_ids = [c.get("character_id", "") for c in characters]

        return {
            "character_ids": character_ids,
            "place_ids": place_ids[:5],
            "service_ids": service_ids[:5],
            "district_ids": sorted(district_ids)[:5],
            "corridor_ids": sorted(corridor_ids)[:5],
            "info_ids": [],
            "event_template_ids": [],
        }

    def build_world_slice_summary(
        self,
        characters: list[dict[str, Any]],
        places: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build a world_slice summary with human-readable names for vis."""

        def _char_summary(c: dict[str, Any]) -> dict[str, Any]:
            prefs = c.get("preferences", {})
            mob = c.get("mobility", {})
            constraints = c.get("constraints", [])
            return {
                "character_id": c.get("character_id", ""),
                "name": c.get("name", ""),
                "age": c.get("age", 0),
                "occupation": c.get("occupation", ""),
                "home_district": mob.get("home_district_name", ""),
                "preferences": {
                    "prefer": prefs.get("prefer", [])[:5],
                    "avoid": prefs.get("avoid", [])[:5],
                    "dietary": prefs.get("dietary", ""),
                    "budget": prefs.get("budget", ""),
                },
                "constraints": [
                    {
                        "type": ct.get("constraint_type", ct.get("type", "visible")),
                        "content": ct.get("content", ""),
                        "reveal_trigger": ct.get("reveal_trigger", ""),
                    }
                    for ct in constraints[:3]
                ],
                "relationships": [],
            }

        def _place_summary(p: dict[str, Any]) -> dict[str, Any]:
            mp = p.get("map_projection", {})
            return {
                "place_id": p.get("place_id", ""),
                "name": p.get("name", ""),
                "type": p.get("primary_type", ""),
                "district": _find_district_name(p.get("district_id", ""), self.world),
                "rating": mp.get("rating", None),
                "address": p.get("address", ""),
            }

        def _district_summary(did: str) -> dict[str, Any]:
            return {
                "district_id": did,
                "name": _find_district_name(did, self.world),
                "role_tags": _find_district_tags(did, self.world),
            }

        char_summaries = [_char_summary(c) for c in characters]
        place_summaries = [_place_summary(p) for p in places]

        district_ids: set[str] = set()
        for p in places:
            did = p.get("district_id", "")
            if did:
                district_ids.add(did)
        for c in characters:
            did = c.get("mobility", {}).get("home_district_id", "")
            if did:
                district_ids.add(did)

        district_summaries = [_district_summary(d) for d in sorted(district_ids)]

        # Add relationships for selected characters
        relationships = self.characters.get("relationships", [])
        sel_ids = {c.get("character_id", "") for c in characters}
        char_name_map = {c.get("character_id", ""): c.get("name", "") for c in characters}
        for csum in char_summaries:
            cid = csum["character_id"]
            rels = []
            for r in relationships:
                if r.get("character_id") == cid and r.get("related_id") in sel_ids:
                    rels.append(
                        f"{char_name_map.get(r['related_id'], r['related_id'])} "
                        f"({r.get('type', '')}/{r.get('subtype', '')})"
                    )
            csum["relationships"] = rels

        return {
            "characters": char_summaries,
            "places": place_summaries,
            "districts": district_summaries,
            "corridors": [],
        }


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Get attribute from dataclass or dict."""
    if hasattr(obj, name):
        val = getattr(obj, name)
        return val() if callable(val) and not isinstance(val, (dict, list)) else val
    if isinstance(obj, dict):
        return obj.get(name, default)
    return default


def _find_char(chars: list[dict[str, Any]], char_id: str) -> dict[str, Any] | None:
    for c in chars:
        if c.get("character_id") == char_id:
            return c
    return None


def _find_district_name(district_id: str, world: dict[str, Any]) -> str:
    for d in world.get("districts", []):
        if d.get("district_id") == district_id:
            return d.get("name", "")
    return ""


def _find_district_tags(district_id: str, world: dict[str, Any]) -> list[str]:
    for d in world.get("districts", []):
        if d.get("district_id") == district_id:
            return d.get("role_tags", [])[:5]
    return []
