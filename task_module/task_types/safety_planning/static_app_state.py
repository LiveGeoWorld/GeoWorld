"""Static, evidence-grounded risk cases for Safety Planning.

Safety is not a generic event task.  Each case models a pre-existing hazard,
the traveller's exposure and vulnerability, and a mitigated decision.  The
construction LLM can enrich an allow-listed case with public wording, but the
compiler owns facts, scope, App projection, and the private evidence contract.
"""

from __future__ import annotations

from typing import Any


STATIC_APP_STATE_SCHEMA_VERSION = "geoword.static_app_state.v1"

SAFETY_RISK_FAMILIES: dict[str, dict[str, Any]] = {
    "weather_exposure": {
        "app": "WeatherApp",
        "variants": ("heavy_rain", "high_wind"),
        "tool": "get_alerts",
        "decision_effect": "change_to_sheltered_place_or_postpone",
        "hazard": "adverse_weather",
    },
    "mobility": {
        "app": "CityNewsApp",
        "variants": ("traffic_congestion", "road_closure", "transit_disruption", "bridge_access_restriction"),
        "tool": "get_info",
        "decision_effect": "avoid_affected_access_or_change_place",
        "hazard": "unsafe_travel_conditions",
    },
    "infrastructure": {
        "app": "CityNewsApp",
        "variants": ("power_outage", "water_service_disruption", "communications_outage", "facility_failure"),
        "tool": "get_info",
        "decision_effect": "choose_service_available_place_or_postpone",
        "hazard": "infrastructure_failure",
    },
    "environmental": {
        "app": "CityNewsApp",
        "variants": ("flooding", "air_pollution", "smoke_haze", "wildfire_smoke", "pollution_release"),
        "tool": "get_info",
        "decision_effect": "avoid_exposed_area_or_change_activity",
        "hazard": "environmental_exposure",
    },
    "public_safety": {
        "app": "CityNewsApp",
        "variants": ("fire_incident", "danger_area_advisory", "public_safety_incident", "event_security_restriction", "earthquake", "landslide"),
        "tool": "get_info",
        "decision_effect": "avoid_affected_area_or_postpone",
        "hazard": "public_safety_hazard",
    },
}

SAFETY_RISK_FAMILY_ALIASES = {
    "traffic_access_risk": "mobility",
    "regional_environment_risk": "public_safety",
    "geophysical": "public_safety",
}

# Terrain/gradient/accessibility risk needs factual, queryable terrain and
# accessibility data.  It is intentionally not eligible until that layer is
# built; an LLM must never invent it from a POI category or coordinates.
UNAVAILABLE_SAFETY_RISK_FAMILIES = {
    "terrain_access_risk": "missing_factual_terrain_accessibility_layer",
}


def eligible_risk_families(risky_target: dict[str, Any], character: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return risk seeds only when hazard, exposure, and scope are factual."""
    character = character or {}
    district_id = str(risky_target.get("district_id") or "")
    home_district_id = str(character.get("home_district_id") or "")
    outdoor_exposure = bool(risky_target.get("weather_sensitive")) or not bool(risky_target.get("indoor"))
    vulnerability = _traveller_vulnerability(character)
    candidates: list[dict[str, Any]] = []
    if outdoor_exposure and district_id:
        candidates.append({
            "family": "weather_exposure", "anchor": "target_outdoor_suitability",
            "exposure": {"target_place_id": risky_target.get("place_id", ""), "district_id": district_id,
                         "outdoor_exposure": True},
            "vulnerability": vulnerability,
        })
    # A travel risk is meaningful only when the journey crosses a factual area
    # boundary; MapApp's baseline route itself remains unchanged.
    if district_id and home_district_id and district_id != home_district_id:
        candidates.append({
            "family": "mobility", "anchor": "home_to_target_district_trip",
            "exposure": {"origin_district_id": home_district_id, "district_id": district_id,
                         "target_place_id": risky_target.get("place_id", "")},
            "vulnerability": vulnerability,
        })
    if district_id:
        candidates.append({
            "family": "public_safety", "anchor": "target_district",
            "exposure": {"district_id": district_id, "target_place_id": risky_target.get("place_id", "")},
            "vulnerability": vulnerability,
        })
    return candidates


def compile_static_safety_snapshot(
    *, task_id: str, risky_target: dict[str, Any], risk_seed: dict[str, Any],
    proposal: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Compile a bounded risk proposal into one private SafetyRiskCase."""
    family = SAFETY_RISK_FAMILY_ALIASES.get(str(risk_seed.get("family") or ""), str(risk_seed.get("family") or ""))
    spec = SAFETY_RISK_FAMILIES.get(family)
    if not spec:
        raise ValueError(f"unsupported safety risk family: {family}")
    variant = str((proposal or {}).get("variant") or "")
    if variant not in spec["variants"]:
        variant = spec["variants"][0]
    district_id = str(risky_target.get("district_id") or "")
    place_id = str(risky_target.get("place_id") or "")
    narrative = _bounded_text((proposal or {}).get("public_notice_zh"), _default_notice(family, variant))
    severity = "high"
    apps: dict[str, dict[str, Any]] = {
        "LocalServiceApp": {"services": []},
        "WeatherApp": {"forecast": [], "alerts": [], "current": {}},
        "CityNewsApp": {"entries": []},
        "CalendarApp": {"availability": []},
        "MessagingApp": {"messages": []},
    }
    if family == "weather_exposure":
        apps["WeatherApp"]["alerts"].append({
            "type": variant, "severity": severity, "scope": {"type": "district", "id": district_id},
            "description": narrative,
        })
        apps["WeatherApp"]["forecast"].append({
            "district_id": district_id, "condition": variant, "severity": severity, "description": narrative,
        })
        apps["WeatherApp"]["current"] = {"district_id": district_id, "condition": variant, "severity": severity}
        response_match = {"container": "alerts", "field": "type", "value": variant}
    else:
        category = {"mobility": "mobility", "infrastructure": "infrastructure",
                    "environmental": "environmental", "public_safety": "public_safety"}.get(
                        family, "public_safety")
        apps["CityNewsApp"]["entries"].append({
            "info_type": "notice", "title": "Safety travel notice", "category": category,
            "risk_variant": variant, "description": narrative, "severity": severity,
            "scope": {"type": "district", "id": district_id},
        })
        response_match = {"container": "items", "field": "risk_variant", "value": variant}

    observation_contract = {
        "app": spec["app"], "tool": spec["tool"],
        "arguments": {"district_id": district_id},
        "response_match": response_match,
    }
    risk_case = {
        "risk_case_id": f"safety_{task_id}", "family": family, "variant": variant,
        "app": spec["app"], "hazard": spec["hazard"], "severity": severity,
        "fact_anchors": {
            "place_id": place_id, "district_id": district_id,
            "service_type": risky_target.get("service_type", ""),
            "indoor": bool(risky_target.get("indoor")),
            "weather_sensitive": bool(risky_target.get("weather_sensitive")),
        },
        "exposure": dict(risk_seed.get("exposure") or {}),
        "vulnerability": dict(risk_seed.get("vulnerability") or {}),
        "mitigation": spec["decision_effect"],
        "observation_contract": observation_contract,
        "simulation_source": "task_construction_safety_risk_case.v2",
    }
    evidence = [{
        "evidence_id": f"{risk_case['risk_case_id']}:observation",
        "risk_case_id": risk_case["risk_case_id"], "dimension": family,
        "source": f"static_app_state.{spec['app']}", "field": response_match["field"],
        "value": variant, "severity": severity, "consequence": spec["decision_effect"],
        "observable_via": {"app": spec["app"], "function": spec["tool"]},
        "observation_contract": observation_contract,
    }]
    snapshot = {
        "schema_version": STATIC_APP_STATE_SCHEMA_VERSION,
        "snapshot_id": f"static_{task_id}", "lifecycle": "episode_start_frozen",
        "risk_case": risk_case, "apps": apps,
    }
    return snapshot, evidence, risk_case


def app_state_for(snapshot: dict[str, Any], app_name: str) -> dict[str, Any]:
    apps = snapshot.get("apps", {}) if isinstance(snapshot, dict) else {}
    value = apps.get(app_name, {}) if isinstance(apps, dict) else {}
    return value if isinstance(value, dict) else {}


def _traveller_vulnerability(character: dict[str, Any]) -> dict[str, Any]:
    mobility = character.get("mobility", {}) if isinstance(character.get("mobility"), dict) else {}
    return {
        "age": character.get("age", 0),
        "max_walk_m": mobility.get("max_walk_m", character.get("max_walk_m", 0)),
        "preferred_transit": character.get("preferred_transit", ""),
    }


def _default_notice(family: str, variant: str) -> str:
    return {
        "weather_exposure": "目标区域当前存在不利天气安全提示。",
        "mobility": "前往目标区域的出行条件存在安全提示。",
        "infrastructure": "目标地点或区域的基础设施服务存在提示。",
        "environmental": "目标区域当前存在环境暴露提示。",
        "public_safety": "目标区域当前存在公共安全提示。",
    }.get(family, variant)


def _bounded_text(value: Any, fallback: str) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:240] if text else fallback
