"""Task-local event sets and simulated, POI-centred impact geometry."""

import copy
import math


def same_circle_anchor(stored, computed):
    """Compare geodesic materialization, allowing only coordinate roundoff.

    Parameters/references and geometry structure remain exact. Never replace
    frozen coordinates; the absolute angular bound is not a spatial buffer.
    """
    if not isinstance(stored, dict) or not isinstance(computed, dict):
        return False
    if {k: v for k, v in stored.items() if k != 'geometry'} != {
            k: v for k, v in computed.items() if k != 'geometry'}:
        return False
    left, right = stored.get('geometry'), computed.get('geometry')
    if not isinstance(left, dict) or not isinstance(right, dict) or set(left) != set(right):
        return False
    if {k: v for k, v in left.items() if k != 'coordinates'} != {
            k: v for k, v in right.items() if k != 'coordinates'}:
        return False

    def coordinates_equal(a, b):
        if isinstance(a, list) or isinstance(b, list):
            return (isinstance(a, list) and isinstance(b, list) and len(a) == len(b)
                    and all(coordinates_equal(x, y) for x, y in zip(a, b)))
        return (type(a) in (int, float) and type(b) in (int, float)
                and math.isfinite(a) and math.isfinite(b)
                and math.isclose(a, b, rel_tol=0, abs_tol=1e-12))

    return coordinates_equal(left.get('coordinates'), right.get('coordinates'))


def events(value):
    return value["events"] if "events" in value else [value]


def records(entries):
    for entry in entries:
        for number, event in enumerate(events(entry["event"])):
            yield {**entry, "event": event,
                   **({"event_index": number} if "events" in entry["event"] else {})}


def materialize(value, context):
    """Resolve author parameters without inventing a factual building boundary."""
    result = copy.deepcopy(context)
    dependency_contract = context.get("protection_contract") in {"safety_occupancy.v5", "safety_occupancy.v6"}
    exact_count = dependency_contract or context.get("protection_contract") == "safety_occupancy.v4"
    if exact_count:
        profiles = context["candidate_profiles"]
        expected = len({p["candidate_ref"] for p in profiles}) - 1
        if expected < 1 or len(profiles) != expected + 1:
            raise ValueError("Event count requires distinct GT and distractor candidates")
        fields = {"events", "gt_protection"} if dependency_contract else {"events"}
        if set(value) != fields or not isinstance(value.get("events"), list) or len(value["events"]) != expected:
            raise ValueError(f"Event count must equal distractor count: expected {expected}")
    if "events" not in value:
        return result
    if not exact_count and (set(value) != {"events"} or not isinstance(value["events"], list) or not 1 <= len(value["events"]) <= 2):
        raise ValueError("Safety requires one or two events")
    anchors = {a["scope_ref"]: a for a in result["geographic_anchors"]}
    for number, event in enumerate(value["events"], 1):
        if not isinstance(event, dict):
            raise ValueError("Each event must be an object")
        definition = event.get("scope")
        if not isinstance(definition, dict):
            raise ValueError("Each new event requires an explicit scope")
        if definition.get("kind") == "route_segment":
            ref = definition.get("source_ref")
            if ref not in anchors or anchors[ref].get("exposure_basis") != "route":
                raise ValueError("Route scope must reference a supplied navigation segment")
            if not any(s["source_ref"] == ref for r in result.get("route_evidence", []) for s in r.get("steps", [])):
                raise ValueError("Route scope must not expand to a complete route")
            if event.get("scope_ref") != ref:
                raise ValueError("Route scope reference differs from its segment")
            continue
        if definition.get("kind") != "poi_circle":
            raise ValueError("Unsupported simulated scope")
        ref = f"simulated_scope_{number}"
        if event.get("scope_ref") != ref:
            raise ValueError("Circle scope_ref must match its event ordinal")
        anchor = circle_anchor(definition, ref, result, anchors)
        if ref in anchors and not same_circle_anchor(anchors[ref], anchor):
            raise ValueError("Stored simulated scope differs from its author parameters")
        if ref not in anchors:
            result["geographic_anchors"].append(anchor)
        anchors[ref] = anchor
    if dependency_contract:
        from .gt_protection import compile_protection
        result = compile_protection(value, result)
    return result


def circle_anchor(definition, ref, context, anchors):
    from geographiclib.geodesic import Geodesic
    if set(definition) != {"kind", "center_ref", "radius_m", "reason"} or definition.get("kind") != "poi_circle":
        raise ValueError("Circle requires center_ref, radius_m and reason only")
    source = definition["center_ref"]
    place = next((p for p in context.get("place_evidence", []) if p["source_refs"][0] == source), None)
    radius = definition["radius_m"]
    if place is None or type(radius) not in (int, float) or not math.isfinite(radius) or radius <= 0:
        raise ValueError("Circle requires an existing POI and a positive finite radius")
    if not isinstance(definition["reason"], str) or not definition["reason"].strip():
        raise ValueError("Circle requires a scenario-based radius explanation")
    location = place["location"]
    # A deterministic geodesic ring is the same geometry used for display,
    # overlap checks and replay; it is never represented as surveyed data.
    ring = []
    for bearing in range(0, 360, 2):
        point = Geodesic.WGS84.Direct(location["lat"], location["lng"], bearing, radius)
        ring.append([point["lon2"], point["lat2"]])
    ring.append(ring[0])
    anchor = {"scope_ref": ref, "source_refs": [source], "exposure_basis": "place",
              "geometry": {"type": "Polygon", "coordinates": [ring]},
              "simulation": copy.deepcopy(definition),
              "public_scope": {**anchors[source]["public_scope"],
                               "approximate": True, "center": copy.deepcopy(location), "radius_m": radius}}
    return anchor
