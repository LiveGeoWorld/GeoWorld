"""Frozen map and route helpers used by Safety construction."""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from itertools import combinations
from typing import Any

from shared.person_route_identity import person_route_ids, route_endpoint_matches
from test_runner.verification.region_aware.generator import (
    _stage3_map_details,
    _stage3_price_fact,
    _stage3_route_index,
    _regular_opening_hours,
    _region_district_names,
    _region_place_district,
    _region_weekday_index,
    _region_opening_intervals,
    _region_price_contains,
)


def _stage3_route_modes() -> list[str]:
    """Return the complete set of modes that Region may expose."""
    return ["WALK", "TRANSIT", "DRIVE"]

def _stage3_route_for(
    route_index: dict[tuple[str, str, str], dict[str, Any]],
    character: dict[str, Any], place: dict[str, Any], mode: str,
) -> dict[str, Any] | None:
    key = (
        f"character:{character.get('character_id')}",
        str(place.get("place_id") or ""),
        mode,
    )
    entry = route_index.get(key)
    if entry is not None:
        return entry
    bare, _ = person_route_ids(str(character.get("character_id") or ""))
    alternate = route_index.get((bare, key[1], mode))
    home = {"lat": character.get("home_lat"), "lng": character.get("home_lng")}
    if alternate and route_endpoint_matches(alternate, "origin", home, character.get("name")):
        return alternate
    return None

def _character_route_endpoint(character: dict[str, Any]) -> dict[str, Any]:
    mobility = character.get("mobility") if isinstance(character.get("mobility"), dict) else {}
    return {
        "place_id": f"character:{character.get('character_id')}",
        "name": str(character.get("name") or ""),
        "location": {"lat": mobility.get("lat"), "lng": mobility.get("lng")},
    }

def _character_public_starting_place(
    character: dict[str, Any], world: dict[str, Any], district_names: dict[str, str],
) -> dict[str, Any]:
    mobility = character.get("mobility") if isinstance(character.get("mobility"), dict) else {}
    current = mobility.get("current_place") if isinstance(mobility.get("current_place"), dict) else {}
    if current.get("name") and isinstance(current.get("location"), dict):
        return {
            "name": str(current["name"]),
            "district": str(
                current.get("district_name")
                or district_names.get(str(current.get("district_id") or ""))
                or ""
            ),
        }
    endpoint = _character_route_endpoint(character)
    location = endpoint.get("location") or {}
    for place in world.get("places") or []:
        place_location = place.get("location") if isinstance(place.get("location"), dict) else {}
        try:
            same = (
                round(float(place_location.get("lat")), 6) == round(float(location.get("lat")), 6)
                and round(float(place_location.get("lng")), 6) == round(float(location.get("lng")), 6)
            )
        except (TypeError, ValueError):
            same = False
        if same and place.get("name"):
            return {
                "name": str(place["name"]),
                "district": _region_place_district(place, district_names),
            }
    # Older character snapshots may retain only a district anchor's coordinates.
    from task_module.task_types.place_discovery.batch_generation.sampler import _resolve_legacy_character_place

    restored = _resolve_legacy_character_place(world, mobility)
    if restored.get("name") and restored.get("source") == "district_anchor":
        return {
            "name": str(restored["name"]),
            "district": str(district_names.get(str(restored.get("district_id") or "")) or ""),
        }
    raise ValueError("Region character has no public real-place starting anchor")

def _haversine_m(a: dict[str, Any], b: dict[str, Any]) -> float:
    from math import asin, cos, radians, sin, sqrt
    la, lb = a.get("location") or {}, b.get("location") or {}
    lat1, lon1, lat2, lon2 = map(radians, [float(la["lat"]), float(la["lng"]), float(lb["lat"]), float(lb["lng"])])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    return 6371000 * 2 * asin(sqrt(sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2))

def _stage3_local_candidate_pool(
    target: dict[str, Any], related_places: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return all related POIs within the rank-5 straight-line radius."""
    target_id = str(target.get("place_id") or "")
    ordered = sorted(
        (place for place in related_places if str(place.get("place_id") or "") != target_id),
        key=lambda place: (_haversine_m(target, place), str(place.get("place_id") or "")),
    )
    if not ordered:
        return []
    radius_m = _haversine_m(target, ordered[min(4, len(ordered) - 1)])
    return [place for place in ordered if _haversine_m(target, place) <= radius_m]


_ORDINARY_SERVICE_FIELDS = (
    "reservable", "dine_in", "takeout", "delivery", "curbside_pickup",
    "outdoor_seating", "live_music", "serves_breakfast", "serves_lunch",
    "serves_dinner", "serves_brunch", "serves_vegetarian_food",
    "wheelchair_accessible_entrance", "wheelchair_accessible_seating",
)


def _region_constraint_matches_place(
    place: dict[str, Any], constraint: dict[str, Any], *, character: dict[str, Any],
    route_index: dict[tuple[str, str, str], dict[str, Any]],
    district_names: dict[str, str], weekday_index: int,
) -> bool:
    kind = str(constraint.get("type") or "")
    if kind == "primary_type":
        return str(place.get("primary_type") or "") == str(constraint.get("value") or "")
    if kind == "district_name":
        return _region_place_district(place, district_names) == str(constraint.get("value") or "")
    if kind == "service_feature":
        return _stage3_map_details(place).get(str(constraint.get("value") or "")) is True
    if kind == "price_maximum":
        _minimum, maximum, currency = _stage3_price_fact(place)
        return (
            currency == str(constraint.get("unit") or "").upper()
            and maximum is not None and maximum <= Decimal(str(constraint["value"]))
        )
    if kind == "price_target_amount":
        return _region_price_contains(
            place, Decimal(str(constraint["value"])),
            str(constraint.get("unit") or "").upper(),
        )
    if kind in {"opens_by", "closes_at_or_after"}:
        intervals = _region_opening_intervals(place, weekday_index)
        if not intervals:
            return False
        value = int(constraint["value"])
        return min(start for start, _end in intervals) <= value if kind == "opens_by" else max(
            end for _start, end in intervals
        ) >= value
    if kind in {"route_duration_range", "walking_distance_range"}:
        mode = str(constraint.get("transportation_mode") or "").upper()
        entry = _stage3_route_for(route_index, character, place, mode)
        if entry is None:
            return False
        route = entry.get("route") or {}
        if kind == "route_duration_range":
            raw = route.get("duration_s")
            value = max(1, math.ceil(float(raw) / 60)) if raw is not None else None
        else:
            raw = route.get("distance_m") if mode == "WALK" else route.get("walking_distance_m")
            value = int(raw) if raw is not None else None
        return value is not None and int(constraint["minimum"]) <= value <= int(constraint["maximum"])
    return False

def _region_scalar_range_constraint(
    *, constraint_id: str, kind: str, target_value: int,
    other_values: list[int], unit: str, mode: str,
) -> dict[str, Any]:
    lower_values = [value for value in other_values if value < target_value]
    upper_values = [value for value in other_values if value > target_value]
    minimum = max(lower_values) + 1 if lower_values else 0
    maximum = min(upper_values) - 1 if upper_values else target_value + max(
        1, math.ceil(target_value * 0.25),
    )
    return {
        "constraint_id": constraint_id,
        "type": kind,
        "operator": "between",
        "minimum": minimum,
        "maximum": maximum,
        "unit": unit,
        "actual_value": target_value,
        "transportation_mode": mode,
        "origin_relation": "starting_place_to_target",
    }

def _region_atomic_constraints(
    target: dict[str, Any], comparison_places: list[dict[str, Any]], *,
    character: dict[str, Any], route_index: dict[tuple[str, str, str], dict[str, Any]],
    district_names: dict[str, str], weekday_index: int,
) -> list[dict[str, Any]]:
    """Enumerate source-backed public facts after the Region target is fixed."""
    atoms: list[dict[str, Any]] = []
    primary_type = str(target.get("primary_type") or "")
    if primary_type:
        atoms.append({
            "constraint_id": "c_primary_type", "type": "primary_type",
            "operator": "equals", "value": primary_type,
        })
    district = _region_place_district(target, district_names)
    if district:
        atoms.append({
            "constraint_id": "c_district_name", "type": "district_name",
            "operator": "equals", "value": district,
        })
    target_details = _stage3_map_details(target)
    for field in _ORDINARY_SERVICE_FIELDS:
        if target_details.get(field) is True:
            atoms.append({
                "constraint_id": f"c_service_{field}", "type": "service_feature",
                "operator": "includes", "value": field,
            })

    target_minimum, target_maximum, target_currency = _stage3_price_fact(target)
    if target_minimum is not None and target_maximum is not None and target_currency:
        atoms.append({
            "constraint_id": "c_price_maximum", "type": "price_maximum",
            "operator": "at_most", "value": str(target_maximum), "unit": target_currency,
        })
        price_points = {(target_minimum + target_maximum) / Decimal(2)}
        for place in comparison_places:
            other_minimum, other_maximum, other_currency = _stage3_price_fact(place)
            if (
                other_minimum is None or other_maximum is None
                or other_currency != target_currency
            ):
                continue
            if other_maximum < target_maximum:
                price_points.add((max(target_minimum, other_maximum) + target_maximum) / Decimal(2))
            if other_minimum > target_minimum:
                price_points.add((target_minimum + min(target_maximum, other_minimum)) / Decimal(2))
        for index, value in enumerate(sorted(price_points), start=1):
            if target_minimum <= value <= target_maximum:
                atoms.append({
                    "constraint_id": f"c_price_target_{index}", "type": "price_target_amount",
                    "operator": "supports_spend", "value": str(value.normalize()),
                    "unit": target_currency,
                    "source_minimum": str(target_minimum), "source_maximum": str(target_maximum),
                })

    intervals = _region_opening_intervals(target, weekday_index)
    if intervals:
        atoms.extend([
            {
                "constraint_id": "c_opens_by", "type": "opens_by", "operator": "at_most",
                "value": min(start for start, _end in intervals), "unit": "minute_of_day",
                "weekday_index": weekday_index,
            },
            {
                "constraint_id": "c_closes_at_or_after", "type": "closes_at_or_after",
                "operator": "at_least", "value": max(end for _start, end in intervals),
                "unit": "minute_of_day", "weekday_index": weekday_index,
            },
        ])

    all_places = [target, *comparison_places]
    for mode in _stage3_route_modes():
        entries = [_stage3_route_for(route_index, character, place, mode) for place in all_places]
        if entries[0] is None:
            continue
        duration_values = [
            max(1, math.ceil(float((entry.get("route") or {})["duration_s"]) / 60))
            for entry in entries[1:] if entry is not None and (entry.get("route") or {}).get("duration_s")
        ]
        target_duration = max(1, math.ceil(float((entries[0].get("route") or {})["duration_s"]) / 60))
        atoms.append(_region_scalar_range_constraint(
            constraint_id=f"c_route_duration_{mode.casefold()}", kind="route_duration_range",
            target_value=target_duration, other_values=duration_values, unit="minute", mode=mode,
        ))
        metric_key = "distance_m" if mode == "WALK" else "walking_distance_m"
        target_distance = (entries[0].get("route") or {}).get(metric_key)
        if target_distance is not None:
            other_distances = [
                int((entry.get("route") or {})[metric_key])
                for entry in entries[1:] if entry is not None and (entry.get("route") or {}).get(metric_key) is not None
            ]
            atoms.append(_region_scalar_range_constraint(
                constraint_id=f"c_walking_distance_{mode.casefold()}", kind="walking_distance_range",
                target_value=int(target_distance), other_values=other_distances, unit="meter", mode=mode,
            ))
    return atoms
