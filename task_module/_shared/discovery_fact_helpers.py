"""Unchanged factual helpers reused by Discovery construction. No generator entry points."""

from __future__ import annotations

import math

from typing import Any


# Source: task_module/task_types/itinerary/sampler.py

def _google_details(place: dict[str, Any]) -> dict[str, Any]:
    projection = place.get("map_projection") or {}
    details = projection.get("google_place_details") or {}
    return details if isinstance(details, dict) else {}


# Source: task_module/task_types/itinerary/sampler.py

def _money_units(value: dict[str, Any] | None) -> float | None:
    if not isinstance(value, dict):
        return None
    try:
        return float(value.get("units") or 0) + float(value.get("nanos") or 0) / 1_000_000_000
    except (TypeError, ValueError):
        return None


# Source: task_module/task_types/itinerary/sampler.py

def _price_range(details: dict[str, Any]) -> dict[str, Any]:
    raw = details.get("price_range") or {}
    lower = _money_units(raw.get("startPrice"))
    upper = _money_units(raw.get("endPrice"))
    if lower is None and upper is None:
        return {}
    currency = str((raw.get("startPrice") or raw.get("endPrice") or {}).get("currencyCode") or "")
    return {"currency": currency, "minimum": lower, "maximum": upper}


# Source: task_module/task_types/itinerary/sampler.py

def _service_features(details: dict[str, Any]) -> list[str]:
    factual_flags = (
        "reservable", "dine_in", "takeout", "delivery", "curbside_pickup",
        "outdoor_seating", "live_music", "serves_breakfast", "serves_lunch",
        "serves_dinner", "serves_brunch", "serves_vegetarian_food",
        "wheelchair_accessible_entrance", "wheelchair_accessible_seating",
    )
    return [key for key in factual_flags if details.get(key) is True]


# Source: task_module/task_types/itinerary/sampler.py

def _weekly_opening_intervals(details: dict[str, Any]) -> dict[str, list[list[int]]]:
    from shared.opening_hours import effective_hours_from_details
    hours = effective_hours_from_details(details) or {}
    result: dict[str, list[list[int]]] = {}
    for period in hours.get("periods") or []:
        opened = period.get("open") or {}
        closed = period.get("close") or {}
        try:
            open_day = int(opened["day"])
            close_day = int(closed.get("day", open_day))
            start = int(opened.get("hour", 0)) * 60 + int(opened.get("minute", 0))
            close_clock = int(closed.get("hour", 0)) * 60 + int(closed.get("minute", 0))
        except (KeyError, TypeError, ValueError):
            continue
        day_delta = (close_day - open_day) % 7
        end = close_clock + day_delta * 24 * 60
        if end <= start:
            end += 24 * 60
        result.setdefault(str(open_day), []).append([start, end])
    for intervals in result.values():
        intervals.sort()
    return result


# Source: task_module/task_types/itinerary/requirement_builder.py

class RequirementBuildError(RuntimeError):
    """A selected POI is indistinguishable in the allowed public fact space."""


# Source: task_module/task_types/itinerary/requirement_builder.py

def _route_constraint_options(
    target: dict[str, Any], matches: list[dict[str, Any]], timeline: list[dict[str, Any]],
    node_index: int, route_cache: Any | None, place_lookup: dict[str, dict[str, Any]],
    route_resolver: Any | None = None,
) -> list[dict[str, Any]]:
    if route_cache is None or len(matches) < 2 or node_index < 0:
        return []
    legs: list[tuple[str, str, str, bool]] = []
    incoming_index = node_index - 1
    while incoming_index >= 0 and timeline[incoming_index].get('local_transition'):
        incoming_index -= 1
    if incoming_index >= 0 and timeline[incoming_index].get("block_kind") == "transit":
        transit = timeline[incoming_index]
        legs.append((
            "start_anchor_to_gt", str(transit.get("from_place_id") or ""),
            str(transit.get("mode") or ""), False,
        ))
    if node_index + 1 < len(timeline) and timeline[node_index + 1].get("block_kind") == "transit":
        transit = timeline[node_index + 1]
        successor = timeline[node_index + 2] if node_index + 2 < len(timeline) else {}
        if successor.get("block_kind") == "fixed_event":
            legs.append((
                "gt_to_end_anchor", str(transit.get("to_place_id") or ""),
                str(transit.get("mode") or ""), True,
            ))

    options: list[dict[str, Any]] = []
    target_id = str(target.get("place_id") or "")
    for relation, anchor_id, mode, reverse in legs:
        anchor = place_lookup.get(anchor_id)
        if not anchor or not mode or mode == "NONE":
            continue
        durations: dict[str, int] = {}
        unknown_ids: set[str] = set()
        for candidate in matches:
            candidate_id = str(candidate.get("place_id") or "")
            origin, destination = (candidate, anchor) if reverse else (anchor, candidate)
            entry = route_cache.get(origin, destination, mode)
            if not entry and route_resolver:
                entry = route_resolver(origin, destination, mode)
            route = (entry or {}).get("route") or {}
            seconds = route.get("duration_s")
            if route.get("available") is False or not isinstance(seconds, (int, float)) or seconds <= 0:
                if route_resolver and not entry:
                    raise RequirementBuildError(f'route evidence unavailable for {candidate_id}; cannot conclude GT ambiguity')
                # An unknown route cannot be used to exclude a public
                # candidate. It may still be removed by another sourced fact.
                unknown_ids.add(candidate_id)
                continue
            durations[candidate_id] = float(seconds)
        target_minutes = durations.get(target_id)
        if target_minutes is None:
            continue
        lower_values = [value for key, value in durations.items() if key != target_id and value < target_minutes]
        upper_values = [value for key, value in durations.items() if key != target_id and value > target_minutes]
        minimum = math.floor(target_minutes / 60) * 60 if lower_values else 0
        maximum = math.ceil(target_minutes / 60) * 60
        needs_repair = bool((lower_values and minimum <= max(lower_values)) or
                            (upper_values and maximum >= min(upper_values)))
        if lower_values and minimum <= max(lower_values):
            minimum = (max(lower_values) + target_minutes) / 2
        if upper_values and maximum >= min(upper_values):
            maximum = (min(upper_values) + target_minutes) / 2
        if minimum > target_minutes or maximum < target_minutes:
            continue
        matching_ids = unknown_ids | {
            place_id for place_id, minutes in durations.items()
            if minimum <= minutes <= maximum
        }
        options.append({
            "constraint": {
                "type": "route_duration_range",
                "operator": "between",
                "minimum": minimum / 60,
                "maximum": maximum / 60,
                "actual_minutes": target_minutes / 60,
                "mode": mode,
                "relation": relation,
            },
            "matching_ids": matching_ids,
        })
        if needs_repair:
            options[-1]['constraint']['_time_repair'] = {
                'target_seconds': target_minutes, 'durations': durations,
                'competitor_seconds': sorted(set(lower_values + upper_values)),
            }
    return options


# Source: task_module/task_types/itinerary/requirement_builder.py

def _natural_candidate_constraints(
    target: dict[str, Any], candidates: list[dict[str, Any]], weekday_index: int,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    service_type = str(target.get("service_type") or "")
    primary_type = str(target.get("primary_type") or "")
    # primary_service_type was itself derived from the Google primary type, so
    # the concrete primary type remains a valid public description even when a
    # broad catalog has no dedicated entry such as food_court or bakery.
    if primary_type:
        facts.append({"type": "primary_type", "operator": "equals", "value": primary_type})
    district = str(target.get("district_name") or "")
    if district:
        facts.append({"type": "district_name", "operator": "equals", "value": district})
    price_range = target.get("price_range") or {}
    if price_range.get("maximum") is not None and price_range.get("currency"):
        facts.append({
            "type": "price_maximum", "operator": "at_most",
            "value": float(price_range["maximum"]), "currency": price_range["currency"],
        })
        facts.extend(_price_target_constraints(target, candidates))
    for feature in target.get("service_features") or []:
        facts.append({"type": "service_feature", "operator": "includes", "value": feature})
    intervals = _weekday_intervals(target, weekday_index)
    if intervals:
        facts.append({"type": "opens_by", "operator": "at_most", "value": min(int(x[0]) for x in intervals)})
        facts.append({"type": "closes_at_or_after", "operator": "at_least", "value": max(int(x[1]) for x in intervals)})
    return facts


# Source: task_module/task_types/itinerary/requirement_builder.py

def _price_target_constraints(
    target: dict[str, Any], candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Derive supported spend points from differences between sourced ranges."""
    price = target.get("price_range") or {}
    currency = str(price.get("currency") or "")
    try:
        low = float(price["minimum"])
        high = float(price["maximum"])
    except (KeyError, TypeError, ValueError):
        return []
    if not currency or high < low:
        return []
    points = {(low + high) / 2.0}
    for candidate in candidates:
        other = candidate.get("price_range") or {}
        if str(other.get("currency") or "") != currency:
            continue
        try:
            other_low = float(other["minimum"])
            other_high = float(other["maximum"])
        except (KeyError, TypeError, ValueError):
            continue
        if other_high < high:
            point = (max(low, other_high) + high) / 2.0
            if low <= point <= high and point > other_high:
                points.add(point)
        if other_low > low:
            point = (low + min(high, other_low)) / 2.0
            if low <= point <= high and point < other_low:
                points.add(point)
    result: list[dict[str, Any]] = []
    for value in sorted(points, key=lambda item: (abs(item - ((low + high) / 2.0)), item))[:5]:
        result.append({
            "type": "price_target_amount",
            "operator": "supports_spend",
            "value": round(value, 2),
            "currency": currency,
            "source_minimum": low,
            "source_maximum": high,
        })
    return result


# Source: task_module/task_types/itinerary/requirement_builder.py

def _matches(place: dict[str, Any], constraints: list[dict[str, Any]], weekday_index: int) -> bool:
    for constraint in constraints:
        kind = str(constraint.get("type") or "")
        value = constraint.get("value")
        if kind == "price_maximum":
            price = place.get("price_range") or {}
            if str(price.get("currency") or "") != str(constraint.get("currency") or ""):
                return False
            if price.get("maximum") is None or float(price["maximum"]) > float(value):
                return False
        elif kind == "price_target_amount":
            price = place.get("price_range") or {}
            if str(price.get("currency") or "") != str(constraint.get("currency") or ""):
                return False
            if price.get("minimum") is None or price.get("maximum") is None:
                return False
            if not float(price["minimum"]) <= float(value) <= float(price["maximum"]):
                return False
        elif kind == "service_feature":
            if str(value) not in {str(item) for item in place.get("service_features") or []}:
                return False
        elif kind in {"opens_by", "closes_at_or_after"}:
            intervals = _weekday_intervals(place, weekday_index)
            if not intervals:
                return False
            if kind == "opens_by" and min(int(x[0]) for x in intervals) > int(value):
                return False
            if kind == "closes_at_or_after" and max(int(x[1]) for x in intervals) < int(value):
                return False
        elif str(place.get(kind) or "") != str(value or ""):
            return False
    return True


# Source: task_module/task_types/itinerary/requirement_builder.py

def _weekday_intervals(place: dict[str, Any], weekday_index: int) -> list[list[int]]:
    return list((place.get("weekly_opening_intervals") or {}).get(str(weekday_index), []))


# Source: task_module/task_types/region_aware/generator.py

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
