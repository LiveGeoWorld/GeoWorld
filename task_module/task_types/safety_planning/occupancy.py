"""Exact, time-filtered occupancy geometry. Display offsets never enter proofs."""

import copy
from itertools import combinations
from datetime import datetime

from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import unary_union

from shared.maps_client import decode_polyline
from .map_scene import coordinates, route_paths
from .spacetime import TimeWindow
from .protection import ProtectionConflict
from .task_time import OCCUPANCY_CONTRACTS, applies, moment, record_applies

CONTRACT = "safety_occupancy.v6"
GT_CONTRACTS = {"safety_occupancy.v5", CONTRACT}


def geometry(value):
    if not isinstance(value, dict):
        raise ValueError("Occupancy requires explicit geometry")
    if "type" in value:
        result = shape(value)
    else:
        kind, data = value.get("kind"), value.get("data", {})
        if kind == "point":
            result = Point(coordinates([data])[0])
        elif kind in {"polygon", "multipolygon"}:
            result = shape({"type": "Polygon" if kind == "polygon" else "MultiPolygon", **data})
        else:
            raise ValueError("Unsupported occupancy geometry")
    if result.is_empty or not result.is_valid:
        raise ValueError("Invalid or empty occupancy geometry")
    if result.geom_type not in {"Point", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}:
        raise ValueError("Unsupported occupancy geometry type")
    x0, y0, x1, y1 = result.bounds
    if not (-180 <= x0 <= x1 <= 180 and -90 <= y0 <= y1 <= 90):
        raise ValueError("Occupancy coordinates outside bounds")
    return result


def scope_geometry(anchor, context):
    raw = anchor.get("geometry")
    if raw:
        result = geometry(raw)
        # Administrative representative points must not substitute for areas.
        if result.geom_type == "Point" and any("/districts/" in r for r in anchor.get("source_refs", [])):
            raise ValueError("Representative district point is not an impact boundary")
        return result
    refs = set(anchor.get("source_refs", []))
    parts = []
    for route in context.get("route_evidence", []):
        if route["source_ref"] in refs:
            parts.extend(LineString(p) for p in route_paths(route["route"]))
        for step in route.get("steps", []):
            encoded = step.get("facts", {}).get("polyline", {}).get("encodedPolyline")
            if step["source_ref"] in refs and encoded:
                points = coordinates(decode_polyline(encoded))
                if len(points) >= 2:
                    parts.append(LineString(points))
    if not parts:
        raise ValueError("Scope lacks a resolvable cached shape")
    return geometry(mapping(unary_union(parts)))


def dependencies(context):
    rows = []
    for place in context.get("place_evidence", []):
        rows.append({"candidate_ref": place["candidate_ref"], "source_ref": place["source_refs"][0],
                     "dependency_kind": "place", "geometry": mapping(Point(coordinates([place["location"]])[0]))})
    for route in context.get("route_evidence", []):
        paths = route_paths(route["route"])
        if not paths:
            raise ValueError("Selected complete route lacks cached geometry")
        rows.append({"candidate_ref": route["candidate_ref"], "source_ref": route["source_ref"],
                     "dependency_kind": "route", "mode": route["mode"],
                     "geometry": mapping(unary_union([LineString(p) for p in paths]))})
    return rows


def occupancy_records(entries):
    from .event_bundle import records
    rows = []
    for entry in records(entries):
        if "event_index" in entry:
            entry = {**entry, "owner": f'{entry["owner"]}:event:{entry["event_index"]}'}
        ctx, event = entry["context"], entry["event"]
        if ctx.get("protection_contract") not in OCCUPANCY_CONTRACTS:
            raise ValueError("Occupancy history cannot import old Safety tasks")
        anchors = {a["scope_ref"]: a for a in ctx["geographic_anchors"]}
        scopes = [(event["scope_ref"], event["active_window"])]
        scopes += [(ref, row["active_window"]) for row in event.get("protected_states", []) for ref in row["scope_refs"]]
        if ctx.get("protection_contract") in GT_CONTRACTS and entry.get("event_index") == 0:
            scopes += [(ref, row["active_window"]) for row in ctx["compiled_gt_protection"] for ref in row["scope_refs"]]
        seen = set()
        for ref, window in scopes:
            key = (ref, window["start"], window["end"])
            if key in seen:
                continue
            seen.add(key)
            rows.append({"record_ref": f'{entry["owner"]}:color:{len(seen)}', "role": "protected_state",
                         "active_window": copy.deepcopy(window), "source_refs": anchors[ref]["source_refs"],
                         "scopes": [{"scope_ref": ref, "geometry": mapping(scope_geometry(anchors[ref], ctx))}]})
        for i, dep in enumerate([] if ctx.get("protection_contract") in GT_CONTRACTS else dependencies(ctx)):
            rows.append({"record_ref": f'{entry["owner"]}:dependency:{i}', "role": "protected_state",
                         **({"task_time": ctx["task_time"]} if "task_time" in ctx else {"active_window": copy.deepcopy(ctx["activity_window"])}),
                         "source_refs": [dep["source_ref"]],
                         "scopes": [{"scope_ref": dep["source_ref"], "geometry": dep["geometry"]}]})
    return rows


def prepare(context, entries, permitted):
    if context.get("protection_contract") in {"safety_occupancy.v2", "safety_occupancy.v3", "safety_occupancy.v4", "safety_occupancy.v5", CONTRACT} and (
            not isinstance(context.get("task_time"), str) or "activity_window" in context):
        raise ValueError("Point-time occupancy requires task_time, not an activity window")
    result = copy.deepcopy(context)
    allowed = TimeWindow(**permitted)
    if not applies(permitted, moment(context)):
        raise ValueError("Event window must overlap activity")
    current = [geometry(row["geometry"]) for row in dependencies(context)]
    if context.get("protection_contract") != CONTRACT:
        for anchor in context.get("geographic_anchors", []):
            current.append(scope_geometry(anchor, context))
    if not current:
        raise ValueError("Local screening requires candidate coordinates or cached routes")
    envelope = unary_union(current).envelope
    if context.get("protection_contract") == CONTRACT:
        x0, y0, x1, y1 = envelope.bounds
        if x0 == x1 and y0 == y1:
            envelope = Point(x0, y0)
        elif x0 == x1 or y0 == y1:
            envelope = LineString([(x0, y0), (x1, y1)])
    rows = [row for row in occupancy_records(entries) if ("task_time" in context or record_applies(row, permitted))
            and any(envelope.intersects(geometry(a["geometry"])) for a in row["scopes"])]
    result["event_generation_window"] = copy.deepcopy(permitted)
    result["prior_spacetime_records"] = ([row for row in rows if record_applies(row, context["task_time"])]
                                         if "task_time" in context else rows)
    if "task_time" in context:
        result["interval_occupancy_records"] = rows
    if context.get("protection_contract") == CONTRACT:
        result["local_search_geometry"] = mapping(envelope)
    return result


def final_spatial_check(event, context, entries):
    """Requery full committed history, never just the pre-authoring local hits."""
    from .event_bundle import events
    anchors = {a["scope_ref"]: a for a in context["geographic_anchors"]}
    requested = [(e["scope_ref"], e["active_window"]) for e in events(event)]
    requested += [(ref, row["active_window"]) for row in context["compiled_gt_protection"]
                  for ref in row["scope_refs"]]
    history = occupancy_records(entries)
    checks, blocked, details = [], [], []
    for ref, window in requested:
        current = scope_geometry(anchors[ref], context)
        hits = []
        for row in history:
            if not record_applies(row, window):
                continue
            for scope in row["scopes"]:
                prior = geometry(scope["geometry"])
                if not current.intersects(prior):
                    continue
                hits.append(row["record_ref"])
                details.append({
                    "scope_ref": ref,
                    "record_ref": row["record_ref"],
                    "current_geometry_type": current.geom_type,
                    "protected_geometry_type": prior.geom_type,
                    "relation": "intersects",
                    "nearest_distance_m": round(current.distance(prior) * 111320, 3),
                    "intersection_length_m": round(
                        current.intersection(prior).length * 111320, 3
                    ) if current.intersection(prior).geom_type in {"LineString", "MultiLineString"} else 0.0,
                })
        hits = sorted(set(hits))
        checks.append({"scope_ref": ref, "active_window": copy.deepcopy(window), "conflicts": hits})
        blocked.extend(hits)
    if blocked:
        raise ProtectionConflict(sorted(set(blocked)), details=details)
    return {"history_owners": [e["owner"] for e in entries], "checks": checks, "conflict_details": details}


def conflicts(context):
    rows = []
    for dep in dependencies(context):
        item = geometry(dep["geometry"])
        for prior in context.get("prior_spacetime_records", []):
            if any(item.intersects(geometry(a["geometry"])) for a in prior["scopes"]):
                rows.append({key: dep[key] for key in ("candidate_ref", "source_ref", "dependency_kind")})
                rows[-1]["record_ref"] = prior["record_ref"]
    return rows


def validate(event, context):
    if "events" in event:
        from .event_bundle import events
        for item in events(event):
            validate(item, context)
        items = events(event)
        for pair in combinations(items, 2):
            anchors = {a["scope_ref"]: a for a in context["geographic_anchors"]}
            first, second = (anchors[e["scope_ref"]] for e in pair)
            if (first.get("exposure_basis") == second.get("exposure_basis") == "route"
                    and scope_geometry(first, context).intersection(scope_geometry(second, context)).length > 0):
                kinds = [{e["kind"] for e in item["effects"]} for item in pair]
                if ("unavailable" in kinds[0] and "delay" in kinds[1]) or ("delay" in kinds[0] and "unavailable" in kinds[1]):
                    raise ValueError("Overlapping route scopes cannot be closed and passable with delay")
        return
    window = TimeWindow(**event["active_window"])
    permitted = TimeWindow(**context["event_generation_window"])
    parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not (parse(permitted.start) <= parse(window.start) and parse(window.end) <= parse(permitted.end)):
        raise ValueError("Event must remain inside its permitted generation window")
    overlap = conflicts(context)
    if overlap:
        raise ProtectionConflict([row["record_ref"] for row in overlap])
    anchors = {a["scope_ref"]: a for a in context["geographic_anchors"]}
    requested = [(event["scope_ref"], event["active_window"])]
    requested += [(ref, state["active_window"]) for state in event.get("protected_states", []) for ref in state["scope_refs"]]
    if context.get("protection_contract") in GT_CONTRACTS:
        requested += [(ref, row["active_window"]) for row in context["compiled_gt_protection"] for ref in row["scope_refs"]]
    blocked = []
    for ref, active in requested:
        current = scope_geometry(anchors[ref], context)
        for prior in context.get("interval_occupancy_records", context["prior_spacetime_records"]):
            if "task_time" in context and not record_applies(prior, active):
                continue
            if any(current.intersects(geometry(a["geometry"])) for a in prior["scopes"]):
                blocked.append(prior["record_ref"])
    if blocked:
        raise ProtectionConflict(blocked)
    # Exposure is recomputed from original geometry, not from display or prose.
    target = scope_geometry(anchors[event["scope_ref"]], context)
    key = "route_exposure" if anchors[event["scope_ref"]].get("exposure_basis") == "route" else "place_exposure"
    kind = "route" if key == "route_exposure" else "place"
    deps = dependencies(context)
    for row in event[key]:
        relevant = [geometry(d["geometry"]) for d in deps if d["candidate_ref"] == row["candidate_ref"] and d["dependency_kind"] == kind]
        actual = "intersects" if any(target.intersects(d) for d in relevant) else "disjoint"
        if not relevant or row["status"] != actual:
            raise ValueError("Event exposure disagrees with cached geometry")
    if event.get("avoidance_assessments", []) != []:
        raise ValueError("Occupancy avoidance is checked by script, not old-state model assessments")
