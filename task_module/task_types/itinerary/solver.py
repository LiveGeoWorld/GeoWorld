"""Deterministic private oracle for long-horizon itinerary tasks.

The public task retains six familiar daily slot labels.  Internally, the
oracle treats fixed events and selectable slot candidates as a temporal DAG:
nodes carry a time window, while an edge exists only when its cached real
route fits between the two selected events.  Travel is therefore an edge,
never a seventh kind of slot.
"""

from __future__ import annotations

from typing import Any
from task_module.task_types.itinerary.clock_precision import clock_minutes, clock_seconds, format_minutes


ROUTE_BUFFER_MIN = 5


def solve_itinerary(
    menus: list[dict[str, Any]],
    character: dict[str, Any],
    route_data: dict[str, dict[str, int]] | None = None,
    scenario: dict[str, Any] | None = None,
) -> dict[str, Any]:
    route_data = route_data or {}
    scenario = scenario or {}
    construction_dag = scenario.get("construction_dag") or {}
    if construction_dag:
        return _solve_incremental_dag(construction_dag, character, scenario)
    days: list[dict[str, Any]] = []
    selected_ids: list[str] = []
    graph = {"nodes": [], "edges": [], "hard_dependencies": [], "candidate_nodes": [], "candidate_edges": []}
    candidate_graph = _candidate_graph(menus, route_data)
    graph["candidate_nodes"] = candidate_graph["nodes"]
    graph["candidate_edges"] = candidate_graph["edges"]
    failures: list[dict[str, Any]] = []

    unavailable_place_ids = {str(place_id) for place_id in scenario.get("unavailable_place_ids", []) or []}
    for menu in menus:
        events, day_graph, day_failures = _solve_day(menu, route_data, unavailable_place_ids)
        graph["nodes"].extend(day_graph["nodes"])
        graph["edges"].extend(day_graph["edges"])
        graph["hard_dependencies"].extend(day_graph["hard_dependencies"])
        failures.extend(day_failures)
        selected_ids.extend(event["place_id"] for event in events if event.get("place_id"))
        days.append({"day": menu.get("day", len(days) + 1), "events": events})

    target = scenario.get("feasibility_target", "feasible")
    expected_feasible = not failures and bool(days) and all(day["events"] for day in days)
    certificate = _certificate(failures, scenario)
    if target == "infeasible" and not certificate:
        # A generator is never allowed to claim an infeasible instance without
        # an observed graph conflict. The caller rejects this candidate rather
        # than emitting an unverifiable negative task.
        expected_feasible = True

    return {
        "expected_feasible": expected_feasible,
        "constraints": {
            "one_primary_event_per_slot": True,
            "slots_are_optional_unless_fixed_or_explicitly_required": True,
            "travel_is_between_events_not_a_slot": True,
            "route_buffer_min": ROUTE_BUFFER_MIN,
            "departure_ends_task": bool(scenario.get("departure_ends_task", False)),
            "daily_scenarios": list(scenario.get("daily_scenarios", [])),
            "continuous_timeline_required": True,
            "lodging_is_stateful": True,
        },
        "correct_itinerary": {
            "days": days,
            "continuous_timeline": list(construction_dag.get("days", [])),
        },
        "acceptable_itineraries": ([{"selected_place_ids": selected_ids}] if expected_feasible else []),
        "temporal_graph": graph,
        "infeasibility_certificate": certificate,
        "oracle_source": "deterministic_temporal_graph_solver.v2",
    }


def _solve_incremental_dag(
    construction_dag: dict[str, Any],
    character: dict[str, Any],
    scenario: dict[str, Any],
) -> dict[str, Any]:
    """Validate and publish a DAG already built one factual node at a time."""
    days = list(construction_dag.get("days", []))
    failures: list[dict[str, Any]] = []
    selected_place_ids: list[str] = []
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    correct_days: list[dict[str, Any]] = []
    for day_payload in days:
        timeline = list(day_payload.get("timeline", []))
        previous_end = ""
        events: list[dict[str, Any]] = []
        for node in timeline:
            start = str(node.get("start") or "")
            end = str(node.get("end") or "")
            if previous_end and start and clock_seconds(start) != clock_seconds(previous_end):
                failures.append({
                    "type": "timeline_gap",
                    "day": day_payload.get("day"),
                    "from": previous_end,
                    "to": start,
                    "node_id": node.get("node_id", ""),
                })
            if not start or not end:
                failures.append({"type": "missing_time", "node_id": node.get("node_id", "")})
            previous_end = end
            place_id = str(node.get("place_id") or node.get("to_place_id") or "")
            if place_id:
                selected_place_ids.append(place_id)
            nodes.append(dict(node))
            if node.get("block_kind") == "transit":
                edges.append({
                    "from_place_id": node.get("from_place_id", ""),
                    "to_place_id": node.get("to_place_id", ""),
                    "mode": node.get("mode", ""),
                    "start": start,
                    "end": end,
                })
            events.append(dict(node))
        correct_days.append({
            "day": day_payload.get("day"),
            "scenario_family": day_payload.get("scenario_family"),
            "events": events,
        })

    expected_feasible = bool(days) and not failures
    certificate = ({"type": failures[0]["type"], **failures[0]} if failures else None)
    return {
        "expected_feasible": expected_feasible,
        "constraints": {
            "continuous_timeline_required": True,
            "travel_is_explicit_timeline_block": True,
            "departure_ends_task": bool(scenario.get("departure_ends_task", False)),
            "daily_scenarios": list(construction_dag.get("daily_scenarios", [])),
            "lodging_is_stateful": True,
        },
        "correct_itinerary": {"days": correct_days},
        "acceptable_itineraries": ([{"selected_place_ids": selected_place_ids}] if expected_feasible else []),
        "temporal_graph": {
            "schema_version": construction_dag.get("schema_version", ""),
            "nodes": nodes,
            "edges": edges,
            "construction_dag": construction_dag,
        },
        "infeasibility_certificate": certificate,
        "oracle_source": "incremental_construction_dag_solver.v1",
    }


def _solve_day(menu: dict[str, Any], route_data: dict[str, dict[str, int]],
               unavailable_place_ids: set[str] | None = None) -> tuple[list[dict], dict, list[dict]]:
    fixed = list(menu.get("fixed_events", []))
    slots = list(menu.get("required_slots", [])) + _selected_optional_slots(menu)
    nodes = [_fixed_node(event) for event in fixed] + [
        _slot_node(slot, unavailable_place_ids or set()) for slot in slots
    ]
    nodes.sort(key=lambda node: (node["window_start"], 0 if node["is_fixed"] else 1))

    graph = {"nodes": [], "edges": [], "hard_dependencies": []}
    failures: list[dict[str, Any]] = []
    for node in nodes:
        graph["nodes"].append(_graph_node(node))
        for predecessor in node.get("depends_on", []):
            graph["hard_dependencies"].append({"from": predecessor, "to": node["node_id"], "type": "hard_precedence"})

    def try_option(node: dict, option: dict, scheduled: list[dict]) -> tuple[dict, dict | None] | None:
        start = node["window_start"]
        predecessor = _latest_predecessor(scheduled, node.get("depends_on", []))
        if predecessor is None and scheduled:
            predecessor = scheduled[-1]
        edge = None
        if predecessor is not None:
            route = _best_route(predecessor["place_id"], option["place_id"], route_data)
            if route is None and predecessor["place_id"] != option["place_id"]:
                return None
            commute = route["minutes"] if route else 0
            start = max(start, predecessor["end_min"] + commute + ROUTE_BUFFER_MIN)
            edge = {
                "from": predecessor["event_id"], "to": node["node_id"],
                "route_key": route["key"] if route else "same_place",
                "mode": route["mode"] if route else "NONE", "travel_min": commute,
            }
        end = start + option["duration_min"]
        if end > node["window_end"]:
            return None
        event = {
            "event_id": node["node_id"], "event_type": node["event_type"],
            "place_id": option["place_id"], "place_name": option["place_name"],
            "start": _format_time(start), "end": _format_time(end),
            "is_fixed": node["is_fixed"], "slot_id": node.get("slot_id"),
            "start_min": start, "end_min": end,
        }
        return event, edge

    def search(index: int, scheduled: list[dict], edges: list[dict]) -> tuple[list[dict] | None, list[dict] | None, list[dict]]:
        if index == len(nodes):
            return scheduled, edges, []

        node = nodes[index]
        if not node["options"]:
            if node["required"]:
                return None, None, [{"type": "empty_candidate_pool", "node_id": node["node_id"]}]
            return search(index + 1, scheduled, edges)

        # Explore all factual candidates. A later required event may make the
        # first locally valid candidate unsuitable, so this deliberately
        # backtracks instead of committing greedily.
        first_failure: list[dict] = []
        for option in node["options"]:
            placed = try_option(node, option, scheduled)
            if placed is None:
                continue
            event, edge = placed
            result, result_edges, child_failures = search(
                index + 1, [*scheduled, event], [*edges, *([edge] if edge else [])],
            )
            if result is not None:
                return result, result_edges, []
            if not first_failure:
                first_failure = child_failures

        # Optional slots enrich a valid itinerary but never become hidden hard
        # constraints. Try omitting one only after all of its real candidates.
        if not node["required"]:
            result, result_edges, child_failures = search(index + 1, scheduled, edges)
            if result is not None:
                return result, result_edges, []
            return None, None, first_failure or child_failures

        return None, None, first_failure or [_failed_node(node, scheduled, route_data)]

    scheduled, selected_edges, failures = search(0, [], [])
    if scheduled is None:
        scheduled = []
    if selected_edges:
        graph["edges"].extend(selected_edges)

    for event in scheduled:
        event.pop("start_min", None)
        event.pop("end_min", None)
    return scheduled, graph, failures


def _selected_optional_slots(menu: dict[str, Any]) -> list[dict]:
    """Select a modest, deterministic subset; no meal is implicitly required."""
    marked = [slot for slot in menu.get("optional_slots", []) if "route_selected" in slot]
    if marked:
        return [slot for slot in marked if slot.get("route_selected")]

    # Backward compatibility for historical task sandboxes created before the
    # construction-time selected-timeline marker existed.
    selected: list[dict] = []
    fixed_intervals = []
    departure_start: int | None = None
    for event in menu.get("fixed_events", []):
        start, _ = _window(event)
        fixed_intervals.append((start, start + int(event.get("duration_min", 30))))
        if event.get("event_type") == "departure":
            departure_start = start
    for slot in menu.get("optional_slots", []):
        event_type = slot.get("event_type", "")
        slot_start, slot_end = _window(slot)
        if departure_start is not None and slot_start >= departure_start:
            continue
        shortest_duration = min(
            (int(candidate.get("duration_typical_min", 60)) for candidate in slot.get("candidates", [])),
            default=60,
        )
        if any(
            not (slot_start + shortest_duration <= fixed_start or slot_start >= fixed_end)
            for fixed_start, fixed_end in fixed_intervals
        ):
            continue
        if event_type in {"morning_activity", "afternoon_activity"}:
            selected.append(slot)
        elif event_type in {"lunch", "dinner"} and menu.get("day", 1) % 2 == (0 if event_type == "lunch" else 1):
            selected.append(slot)
    return selected


def _fixed_node(event: dict[str, Any]) -> dict:
    start, end = _window(event)
    return {
        "node_id": event.get("event_id", event.get("event_type", "fixed")),
        "slot_id": None, "event_type": event.get("event_type", "fixed"), "is_fixed": True,
        "required": True, "window_start": start, "window_end": end + int(event.get("duration_min", 30)),
        "depends_on": list(event.get("depends_on", [])),
        "options": [{"place_id": event.get("place_id", ""), "place_name": event.get("place_name", ""),
                     "duration_min": int(event.get("duration_min", 30))}],
    }


def _slot_node(slot: dict[str, Any], unavailable_place_ids: set[str] | None = None) -> dict:
    start, end = _window(slot)
    forced = slot.get("forced_candidate_place_id", "")
    unavailable_place_ids = unavailable_place_ids or set()
    candidates = [
        candidate for candidate in slot.get("candidates", [])
        if (not forced or candidate.get("place_id") == forced)
        and str(candidate.get("place_id") or "") not in unavailable_place_ids
    ]
    return {
        "node_id": slot.get("slot_id", slot.get("event_type", "slot")),
        "slot_id": slot.get("slot_id"), "event_type": slot.get("event_type", "activity"), "is_fixed": False,
        "required": slot.get("requirement") == "required", "window_start": start, "window_end": end,
        "depends_on": list(slot.get("depends_on", [])),
        "options": [{"place_id": candidate.get("place_id", ""), "place_name": candidate.get("name", ""),
                     "duration_min": int(candidate.get("duration_typical_min", 60))} for candidate in candidates],
    }


def _latest_predecessor(scheduled: list[dict], dependency_ids: list[str]) -> dict | None:
    matches = [event for event in scheduled if event["event_id"] in dependency_ids]
    return max(matches, key=lambda event: event["end_min"]) if matches else None


def _best_route(origin: str, destination: str, route_data: dict[str, dict[str, int]]) -> dict | None:
    key = f"{origin}__TO__{destination}"
    modes = route_data.get(key, {})
    valid = [(mode, int(minutes)) for mode, minutes in modes.items() if isinstance(minutes, (int, float)) and minutes >= 0]
    if not valid:
        return None
    mode, minutes = min(valid, key=lambda item: item[1])
    return {"key": key, "mode": mode, "minutes": minutes}


def _failed_node(node: dict, scheduled: list[dict], route_data: dict[str, dict[str, int]]) -> dict:
    predecessor = scheduled[-1] if scheduled else None
    if predecessor is None:
        return {"type": "time_window", "node_id": node["node_id"], "reason": "no option fits its own window"}
    option = node["options"][0] if node["options"] else {}
    route = _best_route(predecessor["place_id"], option.get("place_id", ""), route_data)
    available = max(0, node["window_end"] - predecessor["end_min"])
    return {
        "type": "temporal_route_conflict", "from_event_id": predecessor["event_id"], "to_node_id": node["node_id"],
        "from_place_id": predecessor["place_id"], "to_place_id": option.get("place_id", ""),
        "available_min": available, "required_min": (route["minutes"] + ROUTE_BUFFER_MIN if route else None),
        "route_key": route["key"] if route else None,
    }


def _certificate(failures: list[dict], scenario: dict[str, Any]) -> dict | None:
    if not failures:
        return None
    seed = scenario.get("infeasibility_certificate_seed")
    if seed:
        return {"type": "minimal_temporal_route_conflict", "conflict_count": 1, **seed}
    return {"type": failures[0].get("type", "constraint_conflict"), "conflict_count": 1, **failures[0]}


def _window(item: dict[str, Any]) -> tuple[int, int]:
    if item.get("time_range"):
        start, end = item["time_range"]
    else:
        start = item.get("earliest", "00:00")
        end = item.get("latest", start)
    return _minutes(start), _minutes(end)


def _minutes(value: str) -> int:
    return clock_minutes(value)


def _format_time(value: int) -> str:
    return format_minutes(value)


def _graph_node(node: dict) -> dict:
    return {key: node[key] for key in ("node_id", "slot_id", "event_type", "is_fixed", "required", "window_start", "window_end", "depends_on")}


def _candidate_graph(menus: list[dict[str, Any]], route_data: dict[str, dict[str, int]]) -> dict[str, list[dict]]:
    """Serialize the private graph before the solver selects one itinerary."""
    graph: dict[str, list[dict]] = {"nodes": [], "edges": []}
    for menu in menus:
        items = list(menu.get("fixed_events", []))
        items.extend(menu.get("required_slots", []))
        items.extend(menu.get("optional_slots", []))
        for item in items:
            item_id = item.get("event_id", item.get("slot_id", item.get("event_type", "event")))
            options = item.get("candidates") or [{"place_id": item.get("place_id", ""), "name": item.get("place_name", "")}]
            start, end = _window(item)
            for option in options:
                graph["nodes"].append({
                    "node_id": f"{item_id}:{option.get('place_id', '')}", "event_ref": item_id,
                    "place_id": option.get("place_id", ""), "window_start": start, "window_end": end,
                })
        for source in items:
            source_id = source.get("event_id", source.get("slot_id", source.get("event_type", "event")))
            source_options = source.get("candidates") or [{"place_id": source.get("place_id", "")}]
            source_start, _ = _window(source)
            for target in items:
                target_id = target.get("event_id", target.get("slot_id", target.get("event_type", "event")))
                target_options = target.get("candidates") or [{"place_id": target.get("place_id", "")}]
                target_start, _ = _window(target)
                if source is target or source_start >= target_start:
                    continue
                for source_option in source_options:
                    for target_option in target_options:
                        route = _best_route(source_option.get("place_id", ""), target_option.get("place_id", ""), route_data)
                        if route:
                            graph["edges"].append({
                                "from": f"{source_id}:{source_option.get('place_id', '')}",
                                "to": f"{target_id}:{target_option.get('place_id', '')}",
                                "route_key": route["key"], "mode": route["mode"], "travel_min": route["minutes"],
                            })
    return graph
