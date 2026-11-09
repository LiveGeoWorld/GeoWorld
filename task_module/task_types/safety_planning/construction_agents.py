"""Auditable Safety construction-model calls."""

from __future__ import annotations

import copy
import json
import base64
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .task_time import OCCUPANCY_CONTRACTS


def project_event_message(payload: dict[str, Any], images=None) -> dict[str, Any]:
    """Remove unknown fact placeholders and verified duplicate geometry only."""
    from shared.maps_client import decode_polyline

    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {key: visit(item) for key, item in value.items()}
        if isinstance(result.get("facts"), dict):
            result["facts"] = {key: item for key, item in result["facts"].items() if item is not None}
        encoded, points = result.get("encoded_polyline"), result.get("polyline_points")
        if isinstance(encoded, str) and encoded and isinstance(points, list) and points:
            try:
                if decode_polyline(encoded) == points:
                    del result["encoded_polyline"]
            except (ValueError, IndexError, TypeError):
                pass
        return result

    result = visit(payload)
    steps = {}
    for route in result.get("route_evidence", []):
        for step in route.get("steps", []):
            steps.setdefault(step.get("source_ref"), []).append(step.get("facts"))
    for anchor in result.get("geographic_anchors", []):
        navigation = anchor.get("navigation")
        matches = [facts for ref in anchor.get("source_refs", []) for facts in steps.get(ref, [])]
        if navigation is not None and matches and all(facts == navigation for facts in matches):
            del anchor["navigation"]
    profiles = {p["candidate_ref"]: p.get("facts", {}) for p in result.get("candidate_profiles", [])
                if "candidate_ref" in p}
    locations = result.get("place_evidence", [])
    # Internal place IDs and duplicate coordinates are not model evidence when
    # the same source and coordinates already have a candidate reference.
    places = result.get("places", [])
    if places and all(any(p.get("source_ref") in loc.get("source_refs", [])
                          and p.get("location") == loc.get("location")
                          and p.get("name") == loc.get("candidate_ref") for loc in locations)
                      and set(p) <= {"source_ref", "place_id", "name", "location", "district_id"}
                      for p in places):
        del result["places"]
    for loc in locations:
        if "district_name" in loc and loc["district_name"] == profiles.get(loc.get("candidate_ref"), {}).get("district_name"):
            del loc["district_name"]
    context = result.get("construction_context", {})
    if context.get("requirement") == "Preserve the fixed target; C plus this event must exclude every ordinary competitor.":
        del context["requirement"]
    modes = sorted({c["transportation_mode"] for c in result.get("public_place_constraints", [])
                    if c.get("transportation_mode")})
    if result.get("constrained_modes") == modes:
        result.pop("constrained_modes", None)
    available = sorted({r["mode"] for r in result.get("route_evidence", []) if "mode" in r})
    if result.get("available_route_modes") == available:
        result.pop("available_route_modes", None)
    if images:
        from .map_scene import payload_hash
        digest = payload_hash(payload)
        for image in images:
            if image.get("payload_sha256") != digest:
                continue
            if hashlib.sha256(Path(image["path"]).read_bytes()).hexdigest() != image.get("image_sha256"):
                continue
            for entry in image.get("geometry_coverage", []):
                node = result
                try:
                    for key in entry["path"]:
                        node = node[key]
                except (KeyError, IndexError, TypeError):
                    continue
                if not isinstance(node, dict):
                    continue
                removed = False
                for key in entry["keys"]:
                    if key in {"polyline_points", "encoded_polyline", "polyline", "startLocation", "endLocation", "geometry"} and key in node:
                        del node[key]
                        removed = True
                if removed:
                    ref = {"image_ref": image["image_ref"], "label": entry["label"]}
                    refs = node.setdefault("map_refs", [])
                    if ref not in refs:
                        refs.append(ref)
    if payload.get("protection_contract") in OCCUPANCY_CONTRACTS:
        result.pop("interval_occupancy_records", None)
        result.pop("prior_spacetime_records", None)
        result.pop("previous_attempts", None)
        result.pop("protection_contract", None)
        result.pop("time_allocation", None)
        result.pop("local_search_geometry", None)
        result.pop("final_spatial_check", None)
        result["coloring"] = [{"label": f"P{i + 1}"} for i, _ in enumerate(
            a for row in payload.get("prior_spacetime_records", []) for a in row["scopes"])]
    return result


def project_map_legend(legend):
    """Group identical omission explanations without dropping their sources."""
    result = copy.deepcopy(legend)
    rows = result.get("not_drawn", [])
    if rows and all(set(row) == {"source_ref", "reason"} for row in rows):
        groups = {}
        for row in rows:
            groups.setdefault(row["reason"], []).append(row["source_ref"])
        result["not_drawn"] = [{"reason": reason, "source_refs": refs} for reason, refs in groups.items()]
    return result


def project_writer_message(payload):
    """Remove authoring IDs and duplicate times from the wire copy only."""
    from .option_selection import public_constraint
    result = copy.deepcopy(payload)
    if "public_place_constraints" in result:
        result["public_place_constraints"] = [public_constraint(c) for c in result["public_place_constraints"]]
    context = result.get("task_context", {})
    date_time = context.get("date_time")
    if isinstance(date_time, dict):
        date_time.pop("source", None)
        if context.get("task_time"):
            for key in ("instruction_time", "episode_start"):
                if date_time.get(key) == context["task_time"]:
                    date_time.pop(key)
        if not date_time:
            context.pop("date_time")
    return result


class SafetyConstructionAgents:
    def __init__(self, client: Any) -> None:
        if client is None:
            raise ValueError("Safety authoring requires a configured construction model")
        self.client = client
        self.calls: list[dict[str, Any]] = []
        self.audit_context: dict[str, Any] = {}
        self.on_audit_update = None

    def _checkpoint(self):
        if self.on_audit_update is not None:
            self.on_audit_update()

    def call(self, role: str, system: str, payload: dict[str, Any], *,
             images: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if images and role != "event":
            raise ValueError("Safety construction maps belong only to the event Agent")
        if role == "event" and payload.get("protection_contract") in OCCUPANCY_CONTRACTS:
            from .map_scene import payload_hash
            required = {f"P{i + 1}" for i, _ in enumerate(
                a for row in payload.get("prior_spacetime_records", []) for a in row["scopes"])}
            represented = set()
            for item in images or []:
                if item.get("payload_sha256") != payload_hash(payload):
                    raise ValueError("Safety map does not match the current event context")
                if hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() != item.get("image_sha256"):
                    raise ValueError("Safety map changed after rendering")
                represented.update(row["label"] for row in item["legend"].get("layers", [])
                                   if row.get("kind") == "coloring" and not row.get("geometry_in_text"))
            if not required <= represented:
                raise ValueError("Safety coloring requires readable, bound maps before the model call")
        wire_payload = project_event_message(payload) if role == "event" else payload
        if role == "writer":
            wire_payload = project_writer_message(payload)
        content: Any = json.dumps(wire_payload, ensure_ascii=False, separators=(",", ":"))
        media_audit = []
        if images:
            from PIL import Image
            content = [{"type": "text", "text": content}]
            for item in images:
                path = Path(item["path"])
                raw = path.read_bytes()
                with Image.open(path) as bitmap:
                    if bitmap.format not in {"PNG", "JPEG"}:
                        raise ValueError("Safety map must be PNG or JPEG")
                    mime = Image.MIME[bitmap.format]
                    size = list(bitmap.size)
                    bitmap.verify()
                image_context = {"image_ref": item["image_ref"], "legend": project_map_legend(item["legend"])}
                covered = {layer.get("source_ref") for layer in item["legend"].get("layers", [])}
                if not set(item["source_refs"]) <= covered:
                    image_context["source_refs"] = item["source_refs"]
                content.append({"type": "text", "text": json.dumps(image_context, ensure_ascii=False)})
                content.append({"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"),
                    "detail": "high",
                }})
                media_audit.append({"image_ref": item["image_ref"], "path": str(path),
                                    "sha256": hashlib.sha256(raw).hexdigest(), "size": size})
            # Only validated, attached images may replace geometry in the wire copy.
            wire_payload = project_event_message(payload, images)
            content[0]["text"] = json.dumps(wire_payload, ensure_ascii=False, separators=(",", ":"))
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": content},
        ]
        started = time.monotonic()
        model = getattr(self.client, "model", "")
        endpoint = getattr(self.client, "endpoint_name", "")
        audit: dict[str, Any] = {
            "role": role, "messages": copy.deepcopy(messages),
            "call_index": len(self.calls) + 1, "context": copy.deepcopy(self.audit_context),
            "started_at": datetime.now(timezone.utc).isoformat(), "status": "requesting",
            "model": model if isinstance(model, str) else "",
            "endpoint_name": endpoint if isinstance(endpoint, str) else "",
            "usage": None, "usage_status": "not_reported",
        }
        if media_audit:
            audit["images"] = media_audit
            audit["input_projection"] = {
                "user_json_bytes": len(content[0]["text"].encode("utf-8")),
                "geometry_bindings": [{"image_ref": item["image_ref"],
                                       "payload_sha256": item.get("payload_sha256"),
                                       "coverage": copy.deepcopy(item.get("geometry_coverage", []))}
                                      for item in images],
            }
        self.calls.append(audit)
        # Persist the exact request before waiting on the provider.
        self._checkpoint()
        try:
            response = self.client.call_chat(messages, extra={"response_format": {"type": "json_object"}})
            audit["response_received_at"] = datetime.now(timezone.utc).isoformat()
            audit["response"] = copy.deepcopy(response)
            audit["usage"] = copy.deepcopy(response.get("usage"))
            audit["usage_status"] = "reported" if isinstance(audit["usage"], dict) and audit["usage"] else "not_reported"
            audit["model"] = response.get("model") or audit["model"]
            audit["status"] = "response_received"
            self._checkpoint()
            assistant = copy.deepcopy(response["choices"][0]["message"])
            audit["messages"].append(assistant)
            result = json.loads(assistant["content"])
            if not isinstance(result, dict):
                raise ValueError("Safety Agent response must be a JSON object")
            audit["status"] = "completed"
            return result
        except BaseException as exc:
            audit["status"] = "failed" if isinstance(exc, Exception) else "interrupted"
            audit["error_class"] = type(exc).__name__
            error = str(exc)
            secret = getattr(self.client, "api_key", None)
            if isinstance(secret, str) and secret:
                error = error.replace(secret, "[REDACTED]")
            audit["error_message"] = error
            raise
        finally:
            audit["finished_at"] = datetime.now(timezone.utc).isoformat()
            audit["elapsed_seconds"] = round(time.monotonic() - started, 6)
            self._checkpoint()
