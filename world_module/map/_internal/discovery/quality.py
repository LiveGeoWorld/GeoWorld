"""Local quality checks for v7 discovery session shards."""

from __future__ import annotations

from typing import Any


ALLOWED_CORRIDOR_TYPES = {
    "rail_corridor",
    "metro_corridor",
    "road_corridor",
    "bus_corridor",
    "ferry_corridor",
    "pedestrian_corridor",
    "commercial_axis",
    "pedestrian_axis",
    "retail_service_axis",
    "waterfront_axis",
    "airport_access",
    "intermodal_corridor",
    "interchange_corridor",
    "east_west_trunk",
    "north_south_corridor",
    "road_tunnel",
    "river_crossing",
}


def valid_district(candidate: dict[str, Any]) -> bool:
    return district_validation_reason(candidate) == ""


def district_validation_reason(candidate: dict[str, Any]) -> str:
    if not candidate.get("name"):
        return "missing_name"
    if not candidate.get("role_hints"):
        return "missing_role_hints"
    if not candidate.get("task_affordances"):
        return "missing_task_affordances"
    if not candidate.get("failure_modes"):
        return "missing_failure_modes"
    if not candidate.get("keywords"):
        return "missing_keywords"
    return ""


ALLOWED_CORRIDOR_CLASSES = {"transport_corridor", "service_axis"}
ALLOWED_CORRIDOR_MODES = {"metro", "rail", "walk", "bus", "drive", "ferry", "mixed"}


def valid_corridor(candidate: dict[str, Any]) -> bool:
    return corridor_validation_reason(candidate) == ""


def corridor_validation_reason(candidate: dict[str, Any]) -> str:
    corridor_class = (candidate.get("corridor_class") or "transport_corridor").strip()
    if corridor_class not in ALLOWED_CORRIDOR_CLASSES:
        return "invalid_corridor_class"
    mode = (candidate.get("mode") or "mixed").strip()
    if mode not in ALLOWED_CORRIDOR_MODES:
        return "invalid_mode"
    min_nodes = 3 if corridor_class == "transport_corridor" else 2
    if len(candidate.get("node_names") or []) < min_nodes:
        return "insufficient_node_names"
    if len(candidate.get("served_areas") or []) < 2:
        return "insufficient_served_areas"
    corridor_type = str(candidate.get("corridor_type") or "").strip()
    # Be strict about the corridor being named and typed, but allow new
    # city-specific transport/service labels as long as they follow the v2
    # suffix convention. Discovery models often produce useful refinements
    # such as "metro_corridor", "river_crossing", or "retail_service_axis".
    type_is_allowed = (
        corridor_type in ALLOWED_CORRIDOR_TYPES
        or corridor_type.endswith("_corridor")
        or corridor_type.endswith("_axis")
        or corridor_type.endswith("_trunk")
        or corridor_type.endswith("_tunnel")
        or corridor_type.endswith("_crossing")
    )
    if not (candidate.get("name") and type_is_allowed):
        return "invalid_name_or_corridor_type"
    bands = candidate.get("corridor_service_bands") or []
    if corridor_class == "service_axis" and not bands:
        return "missing_service_bands"
    return ""


def valid_place_plan(plan: dict[str, Any], district_names: set[str]) -> bool:
    return bool(
        plan.get("district_name") in district_names
        and plan.get("included_types")
        and plan.get("text_queries")
    )


def clean_urls(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if isinstance(v, str) and v.startswith("http")][:2]


def dedupe_named(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for item in items:
        name = str(item.get("name", "")).strip().lower()
        if not name or name in seen:
            continue
        item["evidence_urls"] = clean_urls(item.get("evidence_urls", []))
        seen.add(name)
        out.append(item)
    return out
