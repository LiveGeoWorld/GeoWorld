"""Source-bound drawing objects and exact wire-field coverage, never exposure judgments."""

import hashlib
import json
import math

from shared.maps_client import decode_polyline


def payload_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def coordinates(values):
    result = []
    for point in values:
        lat, lng = (point.get("lat"), point.get("lng")) if isinstance(point, dict) else point
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (lat, lng)):
            raise ValueError("Invalid map coordinate")
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError("Map coordinate outside bounds")
        result.append((lng, lat))
    return result


def route_paths(route):
    decoded = coordinates(decode_polyline(route["encoded_polyline"])) if route.get("encoded_polyline") else []
    points = coordinates(route["polyline_points"]) if route.get("polyline_points") else []
    # Different representations remain separate evidence, not silently interchangeable.
    paths = [points] if points else []
    if decoded and decoded != points:
        paths.append(decoded)
    return [p for p in paths if len(p) >= 2]


def area_paths(geometry):
    kind = str(geometry.get("type", geometry.get("kind", ""))).lower()
    data = geometry.get("data", geometry)
    if kind not in {"polygon", "multipolygon"} or not isinstance(data, dict):
        return []
    polygons = [data.get("coordinates")] if kind == "polygon" else data.get("coordinates")
    paths = []
    try:
        for polygon in polygons:
            for ring in polygon:
                path = coordinates([(p[1], p[0]) for p in ring])
                if len(path) < 4 or path[0] != path[-1]:
                    return []
                paths.append(path)
    except (TypeError, ValueError, IndexError):
        return []
    return paths


def build_scene(context):
    objects, missing = [], []
    palettes = ["#0072b2", "#d55e00", "#009e73", "#a13d8f", "#555555"]
    gt = context.get("construction_context", {}).get("fixed_target_ref")
    candidates = [p["candidate_ref"] for p in context.get("candidate_profiles", [])]
    candidates.sort(key=lambda ref: ref != gt)
    aliases = {ref: chr(65 + i) if i < 26 else f"C{i + 1}" for i, ref in enumerate(candidates)}
    colors = {ref: palettes[i % len(palettes)] for i, ref in enumerate(candidates)}
    counts = {"route": 0, "step": 0, "scope": 0, "place": 0}

    def add(kind, row, paths, owner, path, keys, **extra):
        ref = row.get("source_ref", row.get("scope_ref"))
        if not paths:
            missing.append({"source_ref": ref, "owner": owner, "reason": "No drawable source geometry; text retained"})
            return None
        counts[kind] += 1
        label = {"route": "R", "step": "S", "scope": "G", "place": "L"}[kind] + str(counts[kind])
        item = {"label": label, "kind": kind, "source_ref": ref, "owner": owner,
                "paths": paths, "coverage": [{"path": path, "keys": keys}] if keys else [], **extra}
        objects.append(item)
        return item

    def routes(rows, owner, prefix):
        for i, row in enumerate(rows):
            path = prefix + [i]
            ref = row.get("candidate_ref")
            color = colors.get(ref, "#666666") if owner == "current" else "#777777"
            route = row.get("route", {})
            geometry_keys = []
            if len(route.get("polyline_points", [])) >= 2:
                geometry_keys.append("polyline_points")
            if route.get("encoded_polyline") and len(decode_polyline(route["encoded_polyline"])) >= 2:
                geometry_keys.append("encoded_polyline")
            paths = route_paths(route)
            route_item = add("route", row, paths, owner, path + ["route"], geometry_keys,
                             candidate_ref=ref, color=color, mode=row.get("mode"),
                             minutes=row.get("baseline_minutes"), candidate_label=aliases.get(ref) if owner == "current" else None)
            for j, step in enumerate(row.get("steps", [])):
                facts = step.get("facts", {})
                encoded = facts.get("polyline", {}).get("encodedPolyline")
                step_paths = [coordinates(decode_polyline(encoded))] if encoded else []
                step_paths = [p for p in step_paths if len(p) >= 2]
                # Endpoints alone must never become an invented straight road section.
                item = add("step", step, step_paths, owner, path + ["steps", j, "facts"],
                           ["polyline"] if encoded and set(facts["polyline"]) == {"encodedPolyline"} else [],
                           color=color, route_label=route_item["label"] if route_item else None,
                           candidate_ref=ref, instruction=facts.get("instruction", ""))
                if item:
                    item["endpoints"] = []
                    for key in ("startLocation", "endLocation"):
                        value = facts.get(key, {})
                        loc = value.get("latLng", {})
                        if set(value) == {"latLng"} and set(loc) == {"latitude", "longitude"}:
                            point = coordinates([(loc["latitude"], loc["longitude"])])[0]
                            item["endpoints"].append(point)
                            item["coverage"].append({"path": path + ["steps", j, "facts"], "keys": [key]})

    def places(rows, owner):
        for row in rows:
            location = row.get("location")
            if location:
                ref = row.get("candidate_ref", row.get("name"))
                item = add("place", row, [coordinates([location])], owner, [], [],
                           candidate_ref=ref, color=colors.get(ref, "#555555"),
                           candidate_label=aliases.get(ref) if owner == "current" else None,
                           is_gt=owner == "current" and ref == gt)
                if item and item["candidate_label"]:
                    item["label"] = item["candidate_label"]

    def scopes(rows, owner, prefix):
        for i, row in enumerate(rows):
            geometry = row.get("geometry") or {}
            if geometry:
                pure_geometry = set(geometry) <= {"type", "coordinates"} or (
                    set(geometry) <= {"kind", "data"} and isinstance(geometry.get("data"), dict)
                    and set(geometry["data"]) <= {"coordinates"})
                add("scope", row, area_paths(geometry), owner, prefix + [i], ["geometry"] if pure_geometry else [],
                    color="#7950a3" if owner != "current" else "#555555")
            elif not any(set(row.get("source_refs", [])) & {obj["source_ref"]} for obj in objects):
                missing.append({"source_ref": row["scope_ref"], "owner": owner,
                                "reason": "Scope not drawn; no boundary inferred"})

    routes(context.get("route_evidence", []), "current", ["route_evidence"])
    rows = context.get("places", [])
    if not rows:
        rows = [{**r, "source_ref": r["source_refs"][0]} for r in context.get("place_evidence", []) if r.get("source_refs")]
    places(rows, "current")
    scopes(context.get("geographic_anchors", []), "current", ["geographic_anchors"])
    if context.get("protection_contract") in {"safety_occupancy.v1", "safety_occupancy.v2", "safety_occupancy.v3", "safety_occupancy.v4", "safety_occupancy.v5", "safety_occupancy.v6"}:
        from .occupancy import geometry
        def paths_of(g):
            if g.geom_type == "Polygon":
                return [list(g.exterior.coords)] + [list(r.coords) for r in g.interiors]
            if hasattr(g, "geoms"):
                return [p for child in g.geoms for p in paths_of(child)]
            return [list(g.coords)]
        for i, anchor in enumerate(a for r in context.get("prior_spacetime_records", []) for a in r["scopes"]):
            objects.append({"label": f"P{i + 1}", "kind": "coloring", "owner": "coloring",
                            "source_ref": f"P{i + 1}", "color": "#84778f", "coverage": [],
                            "geometry": anchor["geometry"],
                            "paths": paths_of(geometry(anchor["geometry"]))})
        return [{**obj, "owners": [obj["owner"]]} for obj in objects], missing
    for i, record in enumerate(context.get("prior_spacetime_records", [])):
        owner = record["record_ref"]
        prefix = ["prior_spacetime_records", i]
        routes(record.get("routes", []), owner, prefix + ["routes"])
        places(record.get("places", []), owner)
        scopes(record.get("scopes", []), owner, prefix + ["scopes"])
    # Same frozen source repeated by event/protection records is drawn once, with all owners retained.
    unique = {}
    for obj in objects:
        key = (obj["kind"], obj["source_ref"], json.dumps(obj["paths"]),
               json.dumps(obj.get("endpoints", [])), obj.get("candidate_ref"), obj["owner"])
        if key in unique:
            prior = unique[key]
            prior["owners"].append(obj["owner"])
            prior["coverage"].extend(obj["coverage"])
        else:
            obj["owners"] = [obj["owner"]]
            unique[key] = obj
    return list(unique.values()), missing
