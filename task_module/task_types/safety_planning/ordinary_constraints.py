"""Target-first ordinary Safety constraints over source-backed activity matches."""

from __future__ import annotations

import copy
import math
from itertools import combinations
from typing import Any, Iterator

from task_module.task_types.region_aware.generator import (
    _region_atomic_constraints, _region_constraint_matches_place,
    _region_district_names, _region_weekday_index, _stage3_route_for,
)


def ordinary_constraint_atoms(
    *, target_id: str, places: list[dict[str, Any]], activity_place_ids: list[str],
    character: dict[str, Any], route_index: dict, world: dict[str, Any],
    time_context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Enumerate C without choosing events, changing GT, or hiding competitors.

Activity membership comes from matching frozen service capabilities, not
from model-inferred activities or a place-type-to-activity lookup table.
"""
    by_id = {str(place["place_id"]): place for place in places}
    if len(by_id) != len(places) or not set(activity_place_ids) <= set(by_id):
        raise ValueError("Ordinary candidates must be unique frozen places")
    if target_id not in activity_place_ids:
        raise ValueError("Fixed target must satisfy the activity requirement")
    eligible = [by_id[pid] for pid in activity_place_ids]
    if len(eligible) < 2:
        return []
    target = by_id[target_id]
    districts = _region_district_names(world)
    weekday = _region_weekday_index(time_context)
    context = dict(character=character, route_index=route_index,
                   district_names=districts, weekday_index=weekday)
    # Share the already-established frozen fact readers. Route atoms below
    # must preserve ordinary alternatives, unlike Region's R+ separator.
    atoms = [atom for atom in _region_atomic_constraints(
        target, [p for p in eligible if p["place_id"] != target_id], **context,
    ) if atom["type"] not in {"route_duration_range", "walking_distance_range"}]
    for mode in ("WALK", "TRANSIT", "DRIVE"):
        target_entry = _stage3_route_for(route_index, character, target, mode)
        if target_entry is None:
            continue
        for kind, metric, unit in (
            ("route_duration_range", "duration_s", "minute"),
            ("walking_distance_range", "distance_m" if mode == "WALK" else "walking_distance_m", "meter"),
        ):
            value = (target_entry.get("route") or {}).get(metric)
            if value is None or (kind == "walking_distance_range" and value <= 0):
                continue
            target_value = max(1, math.ceil(float(value) / 60)) if unit == "minute" else int(value)
            values = {target_value}
            for place in eligible:
                entry = _stage3_route_for(route_index, character, place, mode)
                raw = ((entry or {}).get("route") or {}).get(metric)
                if raw is not None:
                    values.add(max(1, math.ceil(float(raw) / 60)) if unit == "minute" else int(raw))
            for upper in sorted(value for value in values if value >= target_value):
                atoms.append({
                    "constraint_id": f"c_{kind}_{mode.lower()}_{upper}", "type": kind,
                    "operator": "between", "minimum": 0, "maximum": upper,
                    "unit": unit, "actual_value": target_value,
                    "transportation_mode": mode, "origin_relation": "starting_place_to_target",
                })

    checked = []
    for atom in atoms:
        retained = frozenset(str(place["place_id"]) for place in eligible
                             if _region_constraint_matches_place(place, atom, **context))
        if target_id in retained and len(retained) >= 2:
            checked.append({"constraint": copy.deepcopy(atom),
                            "ordinary_place_ids": [pid for pid in activity_place_ids if pid in retained]})
    return checked


def ordinary_constraint_options(**kwargs) -> Iterator[dict[str, Any]]:
    """Legacy enumerator; formal construction samples options in option_selection."""
    checked = [(row["constraint"], frozenset(row["ordinary_place_ids"]))
               for row in ordinary_constraint_atoms(**kwargs)]
    activity_place_ids = kwargs["activity_place_ids"]
    target_id = kwargs["target_id"]
    # Activity itself is a requirement; additional dimensions are optional.
    for size in range(len(checked) + 1):
        for chosen in combinations(checked, size):
            modes = {atom.get("transportation_mode") for atom, _ in chosen if atom.get("transportation_mode")}
            if len(modes) > 1:
                continue
            retained = set(activity_place_ids)
            for _, members in chosen:
                retained.intersection_update(members)
            if target_id not in retained or len(retained) < 2:
                continue
            yield {"target_place_id": target_id,
                   "constraints": [copy.deepcopy(atom) for atom, _ in chosen],
                   "ordinary_place_ids": [pid for pid in activity_place_ids if pid in retained]}
