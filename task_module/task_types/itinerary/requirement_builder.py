"""Compile public itinerary requirements from already selected DAG POIs."""

from __future__ import annotations

from itertools import combinations
import math
from typing import Any
from task_module.task_types.itinerary.clock_precision import clock_seconds
from task_module.task_types.itinerary.time_repair_binding import repair_incoming_time

class RequirementBuildError(RuntimeError):
    """A selected POI is indistinguishable in the allowed public fact space."""


def build_public_requirements(
    construction_dag: dict[str, Any], city_candidates: list[dict[str, Any]],
    *, route_cache: Any | None = None, place_lookup: dict[str, dict[str, Any]] | None = None,
    time_repair_client: Any | None = None, audit: Any | None = None,
    route_resolver: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build natural, full-city-validated requirements after GT selection."""
    candidates = [item for item in city_candidates if item.get("place_id")]
    public: list[dict[str, Any]] = []
    private: list[dict[str, Any]] = []
    for day_payload in construction_dag.get("days", []):
        day = int(day_payload.get("day") or 0)
        weekday_index = int(day_payload.get("weekday_index") or 0)
        timeline = list(day_payload.get("timeline") or [])
        for node_index, node in enumerate(timeline):
            if node.get("block_kind") != "poi_activity":
                continue
            place_id = str(node.get("place_id") or "")
            selected_type = str(node.get("selected_service_type") or node.get("event_type") or "")
            target = next((
                item for item in candidates
                if str(item.get("place_id")) == place_id
                and str(item.get("service_type") or "") == selected_type
            ), None) or {"place_id": place_id, "name": node.get("place_name", ""), "service_type": selected_type}
            required_service_types = list(
                target.get("required_service_types")
                or node.get("required_service_types")
                or _required_service_types(target, selected_type)
            )
            requirement_id = f"day{day}_requirement_{len(public) + 1}"
            facts, matches = _natural_unique_facts(
                target, candidates, weekday_index,
                timeline=timeline, node_index=node_index,
                route_cache=route_cache, place_lookup=place_lookup or {},
                route_resolver=route_resolver,
            )
            if len(matches) != 1:
                raise RequirementBuildError(
                    f"{place_id} is observationally indistinguishable from "
                    f"{[str(item.get('place_id') or '') for item in matches if item.get('place_id')]}"
                )
            for constraint in facts:
                evidence = constraint.pop('_time_repair', None)
                if evidence:
                    repair_incoming_time(node, timeline, constraint, evidence, time_repair_client, audit,
                                         _weekday_intervals(target, weekday_index))
            activity_group = _activity_group_context(
                timeline, node_index, lodging=construction_dag.get("lodging") or {},
            )
            route_context = activity_group.get("route_context", {})
            item = {
                "requirement_id": requirement_id,
                "level": "required_category",
                "day": day,
                "weekday": day_payload.get("weekday", ""),
                "activity_direction": selected_type,
                "required": True,
                "constraints": facts,
                "route_context": route_context,
                "activity_group": activity_group,
            }
            primary_service_type = str(target.get("primary_service_type") or selected_type)
            if required_service_types == [primary_service_type]:
                item["primary_service_type"] = primary_service_type
            else:
                item["service_types"] = required_service_types
            public.append(item)
            private.append({
                "requirement_id": requirement_id,
                "gt_place_id": place_id,
                "gt_place_name": str(target.get("name") or node.get("place_name") or ""),
                "selected_service_type": selected_type,
                "required_service_types": required_service_types,
                "matching_place_ids": [str(match.get("place_id")) for match in matches],
                "fact_evidence": _fact_evidence(target),
                "route_context": route_context,
                "activity_group": activity_group,
                "candidate_comparison": compare_selected_constraints(
                    target, candidates, facts, weekday_index, timeline, node_index,
                    route_cache, place_lookup or {},
                ),
            })
            node["requirement_id"] = requirement_id
            node["requirement_level"] = "required_category"
    return public, private


def compare_selected_constraints(target, candidates, constraints, weekday_index,
                                 timeline, node_index, route_cache=None, place_lookup=None):
    """Observe the existing final C only; never select C, fetch routes or infer semantics."""
    required = target.get('required_service_types') or _required_service_types(target, target.get('service_type', ''))
    primary = str(target.get('primary_service_type') or target.get('service_type') or '')
    pool = [p for p in candidates if p.get('service_type') == target.get('service_type') and
            (str(p.get('primary_service_type') or '') == primary if required == [primary]
             else set(required).issubset(set(p.get('service_types') or [])))]
    rows = []
    for p in pool:
        checks = []
        for c in constraints:
            kind = c.get('type'); actual = p.get(kind); matched = None
            if kind == 'route_duration_range':
                incoming = node_index - 1
                while incoming >= 0 and timeline[incoming].get('local_transition'):
                    incoming -= 1
                edge = timeline[incoming] if c.get('relation') == 'start_anchor_to_gt' and incoming >= 0 else (timeline[node_index + 1] if node_index + 1 < len(timeline) else {})
                reverse = c.get('relation') == 'gt_to_end_anchor'
                anchor = (place_lookup or {}).get(edge.get('to_place_id') if reverse else edge.get('from_place_id'))
                entry = route_cache.get(p, anchor, c.get('mode')) if reverse and anchor and route_cache else (route_cache.get(anchor, p, c.get('mode')) if anchor and route_cache else None)
                route = (entry or {}).get('route') or {}; seconds = route.get('duration_s')
                actual = seconds / 60 if isinstance(seconds, (float, int)) and seconds > 0 and route.get('available') is not False else None
                if actual is not None:
                    matched = c['minimum'] <= actual <= c['maximum']
            else:
                if kind in ('opens_by', 'closes_at_or_after'):
                    actual = _weekday_intervals(p, weekday_index)
                elif kind == 'service_feature':
                    actual = p.get('service_features') or []
                elif kind in ('price_maximum', 'price_target_amount'):
                    actual = p.get('price_range')
                matched = _matches(p, [c], weekday_index)
            checks.append({'constraint': dict(c), 'actual': actual, 'matches': matched})
        rows.append({'place_id': p['place_id'], 'name': p.get('name', p['place_id']),
                     'location': p.get('location'), 'checks': checks})
    return {'schema_version': 'itinerary.selected_C_comparison.v1', 'candidates': rows}


def _natural_unique_facts(
    target: dict[str, Any], candidates: list[dict[str, Any]], weekday_index: int,
    *, timeline: list[dict[str, Any]] | None = None, node_index: int = -1,
    route_cache: Any | None = None, place_lookup: dict[str, dict[str, Any]] | None = None,
    route_resolver: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    service_type = str(target.get("service_type") or "")
    required_service_types = list(
        target.get("required_service_types") or _required_service_types(target, service_type)
    )
    primary_service_type = str(target.get("primary_service_type") or service_type)
    if required_service_types == [primary_service_type]:
        same_type = [
            item for item in candidates
            if str(item.get("service_type") or "") == service_type
            and str(item.get("primary_service_type") or "") == primary_service_type
        ]
    else:
        same_type = [
            item for item in candidates
            if str(item.get("service_type") or "") == service_type
            and set(required_service_types).issubset(set(item.get("service_types") or []))
        ]
    target_id = str(target.get("place_id") or "")
    options: list[dict[str, Any]] = []
    for constraint in _natural_candidate_constraints(target, same_type, weekday_index):
        matching_ids = {
            str(item.get("place_id") or "")
            for item in same_type
            if _matches(item, [constraint], weekday_index)
        }
        if target_id in matching_ids:
            options.append({"constraint": constraint, "matching_ids": matching_ids})
    options.extend(_route_constraint_options(
        target, same_type, timeline or [], node_index,
        route_cache, place_lookup or {},
    ))

    # Prefer the smallest natural combination. Failure is not a reason to
    # resample merely because one fixed template was insufficient: only a POI
    # that remains equivalent after every allowed public fact is ambiguous.
    best_ids = {str(item.get("place_id") or "") for item in same_type}
    for size in range(1, len(options) + 1):
        for selected in combinations(options, size):
            matching_ids = set.intersection(*(set(item["matching_ids"]) for item in selected))
            if target_id not in matching_ids:
                continue
            if len(matching_ids) < len(best_ids):
                best_ids = matching_ids
            if matching_ids == {target_id}:
                facts = []
                for option in selected:
                    constraint = dict(option['constraint'])
                    evidence = constraint.get('_time_repair')
                    if evidence:
                        residual = set.intersection(*(set(other['matching_ids']) for other in selected if other is not option)) if len(selected) > 1 else {str(p['place_id']) for p in same_type}
                        evidence = dict(evidence)
                        evidence['competitor_seconds'] = sorted({v for p, v in evidence['durations'].items() if p in residual and p != target_id})
                        constraint['_time_repair'] = evidence
                        lower = [v for v in evidence['competitor_seconds'] if v < evidence['target_seconds']]
                        rounded_low = math.floor(evidence['target_seconds'] / 60) * 60 if lower else 0
                        rounded_high = math.ceil(evidence['target_seconds'] / 60) * 60
                        if not any(rounded_low <= v <= rounded_high for v in evidence['competitor_seconds']):
                            constraint.pop('_time_repair')
                            constraint['minimum'], constraint['maximum'] = rounded_low / 60, rounded_high / 60
                    facts.append(constraint)
                return facts, [target]
    if route_resolver is not None:
        factual = [option for option in options if option['constraint']['type'] != 'route_duration_range']
        residual = (set.intersection(*(set(option['matching_ids']) for option in factual)) if factual
                    else {str(p['place_id']) for p in same_type})
        _route_constraint_options(target, [p for p in same_type if str(p['place_id']) in residual],
                                  timeline or [], node_index, route_cache, place_lookup or {}, route_resolver)
        return _natural_unique_facts(target, candidates, weekday_index, timeline=timeline,
                                     node_index=node_index, route_cache=route_cache, place_lookup=place_lookup)
    return [], [item for item in same_type if str(item.get("place_id") or "") in best_ids]


def _required_service_types(target: dict[str, Any], selected_type: str) -> list[str]:
    primary = str(target.get("primary_service_type") or selected_type or "")
    selected = str(selected_type or "")
    if not primary:
        return [selected] if selected else []
    if not selected or selected == primary:
        return [primary]
    return [primary, selected]


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


def _weekday_intervals(place: dict[str, Any], weekday_index: int) -> list[list[int]]:
    return list((place.get("weekly_opening_intervals") or {}).get(str(weekday_index), []))


def _activity_group_context(
    timeline: list[dict[str, Any]], node_index: int, *, lodging: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project detailed execution nodes into one public activity-group context."""
    node = timeline[node_index]
    context: dict[str, Any] = {}
    windows = list(node.get("occupied_time_boundaries") or [])
    group_start = str(node.get("start") or "")
    group_end = str(node.get("end") or "")
    start_anchor = {"kind": "current_location"}
    end_anchor: dict[str, Any] | None = None
    transition_activities = []
    scan = node_index - 1
    while scan >= 0 and (timeline[scan].get('local_transition') or timeline[scan].get('block_kind') == 'transit'):
        if timeline[scan].get('local_transition'):
            transition_activities.insert(0, dict(timeline[scan]))
        scan -= 1

    incoming_index = node_index - 1
    while incoming_index >= 0 and timeline[incoming_index].get('local_transition'):
        incoming_index -= 1
    if incoming_index >= 0 and timeline[incoming_index].get("block_kind") == "transit":
        transit = timeline[incoming_index]
        group_start = str(transit.get("start") or group_start)
        predecessor_index = incoming_index - 1
        while predecessor_index >= 0 and timeline[predecessor_index].get('local_transition'):
            predecessor_index -= 1
        predecessor = timeline[predecessor_index] if predecessor_index >= 0 else {}
        if (
            not predecessor
            and str(transit.get("from_place_id") or "")
            == str((lodging or {}).get("place_id") or "")
        ):
            start_anchor = {
                "kind": "hotel", "relation": "start",
                "place_name": (lodging or {}).get("name", ""),
            }
        else:
            start_anchor = _public_anchor(
                predecessor, relation="start", lodging=lodging,
            )
        context["incoming"] = {
            "mode": transit.get("mode", ""), "duration_min": _duration_minutes(transit),
            "relation": "start_anchor_to_gt",
        }

    cursor = node_index + 1
    internal_activities: list[dict[str, Any]] = []
    while cursor < len(timeline) and timeline[cursor].get("block_kind") == "personal_activity":
        personal = timeline[cursor]
        if personal.get('local_transition'):
            break
        if str(personal.get("place_id") or "") != str(node.get("place_id") or ""):
            break
        internal_activities.append({
            "activity_name": personal.get("activity_name", ""),
            "start": personal.get("start", ""),
            "end": personal.get("end", ""),
        })
        for boundary in personal.get("occupied_time_boundaries") or []:
            if boundary not in windows:
                windows.append(boundary)
        group_end = str(personal.get("end") or group_end)
        cursor += 1

    if cursor < len(timeline) and timeline[cursor].get("block_kind") == "transit":
        transit = timeline[cursor]
        successor = timeline[cursor + 1] if cursor + 1 < len(timeline) else {}
        successor_kind = str(successor.get("block_kind") or "")
        successor_event = str(successor.get("event_type") or "")
        returns_to_boundary = successor_kind == "fixed_event" or successor_event == "overnight_personal_activity"
        if returns_to_boundary:
            group_end = str(transit.get("end") or group_end)
            end_anchor = _public_anchor(successor, relation="end", lodging=lodging)
            context["outgoing"] = {
                "mode": transit.get("mode", ""), "duration_min": _duration_minutes(transit),
                "relation": "gt_to_end_anchor",
            }

    deadline = None
    if end_anchor and end_anchor.get("kind") == "fixed_event":
        deadline = end_anchor.get("time")

    return {
        "time_windows": windows,
        "group_start": group_start,
        "group_end": group_end,
        "start_anchor": start_anchor,
        "end_anchor": end_anchor,
        "deadline": deadline,
        "route_context": context,
        "private_internal_activities": internal_activities,
        "private_transition_activities": transition_activities,
    }


def _public_anchor(
    node: dict[str, Any], *, relation: str, lodging: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kind = str(node.get("block_kind") or "")
    event_type = str(node.get("event_type") or "")
    if kind == "fixed_event":
        return {
            "kind": "fixed_event",
            "event_type": event_type,
            "place_name": node.get("place_name", ""),
            "time": node.get("start", ""),
        }
    if (
        lodging
        and str(node.get("place_id") or "")
        and str(node.get("place_id") or "") == str(lodging.get("place_id") or "")
    ):
        return {
            "kind": "hotel", "relation": relation,
            "place_name": lodging.get("name", ""),
        }
    if event_type == "overnight_personal_activity":
        return {"kind": "hotel", "relation": relation}
    if kind == "poi_activity":
        return {"kind": "previous_activity" if relation == "start" else "next_activity"}
    return {"kind": "current_location", "relation": relation}


def _route_context(timeline: list[dict[str, Any]], node_index: int) -> dict[str, Any]:
    """Compatibility view of the public activity group's owned route legs."""
    return _activity_group_context(timeline, node_index).get("route_context", {})


def _duration_minutes(node: dict[str, Any]) -> int:
    return max(0, (clock_seconds(node.get('end') or '00:00') - clock_seconds(node.get('start') or '00:00')) / 60)


def _fact_evidence(place: dict[str, Any]) -> dict[str, Any]:
    return {
        key: place.get(key)
        for key in (
            "service_type", "selected_service_type", "primary_service_type", "service_types",
            "primary_type", "district_id", "district_name", "rating", "location",
            "price_level", "price_range", "service_features", "weekly_opening_intervals",
        )
        if place.get(key) not in (None, "", [], {})
    }
