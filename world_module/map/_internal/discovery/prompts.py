"""Prompt builders for recoverable v7 discovery sessions."""

from __future__ import annotations

import json
from typing import Any

from world_module.map.seed import CitySeed, seed_to_prompt_payload


def base_context(seed: CitySeed, *, allow_web_search: bool) -> str:
    search_clause = "Use web search for real public facts." if allow_web_search else "Do not use web search; reason only from provided inputs."
    payload = json.dumps(seed_to_prompt_payload(seed), ensure_ascii=False, separators=(",", ":"))
    return f"""
You are a discovery worker for GeoAgentBench v7. The goal is to discover real,
geocodable city substrate for dynamic city-service tasks.

{search_clause}

First principles:
- A district matters if it creates service choices, constraints, substitutions,
  local context, or realistic failure modes.
- A corridor is a REAL CITY MOBILITY BACKBONE that connects districts:
  metro/rail line, road artery, transfer spine, river/bay crossing, airport
  access, or pedestrian/commercial axis with strong city identity. A corridor
  must be identifiable on a real map (named line, named road, named bridge,
  etc.) and must connect at least 2 of the discovered districts.
- Each corridor has a transport mode (metro / rail / walk / bus / drive /
  ferry / mixed) and a corridor_class (transport_corridor for true mobility
  spines, service_axis for commercial/pedestrian density bands that are not
  primarily mobility lines).
- Output is not the final world. It is a candidate substrate for Google Maps
  enrichment, real Routes API verification, and later executable task generation.

City seed JSON: {payload}

Return strict compact JSON only. No markdown. Keep strings short. Use real,
geocodable names. Use at most 2 evidence URLs per object.
""".strip()


def city_outline_prompt(seed: CitySeed) -> str:
    return f"""
{base_context(seed, allow_web_search=True)}

Task: create a small city discovery outline. Do not list all districts or all
corridors. Define functional groups that later workers can discover separately.

Output schema:
{{
  "city_summary": {{
    "name": "...",
    "country": "...",
    "urban_structure_notes": ["short facts"],
    "benchmark_design_intent": ["why this city supports dynamic service tasks"],
    "evidence_urls": ["https://..."]
  }},
  "district_group_specs": [
    {{
      "group_name": "station_business_core",
      "focus": "what this group should discover",
      "target_count": 4,
      "role_hints": ["commercial","rail_hub"],
      "task_value": "why this group matters"
    }}
  ],
  "corridor_group_specs": [
    {{
      "group_name": "rail_intermodal",
      "corridor_types": ["rail_corridor","intermodal_corridor"],
      "focus": "what this group should discover",
      "target_count": 5,
      "task_value": "why this group matters"
    }}
  ],
  "source_notes": ["uncertainty or source notes"]
}}

Return 5 to 8 district groups and 4 to 6 corridor groups. Each target_count
should be small enough for a later worker to return compact JSON.
""".strip()


def districts_by_group_prompt(seed: CitySeed, city_outline: dict[str, Any], group: dict[str, Any]) -> str:
    target = int(group.get("target_count") or 5)
    target = max(3, min(target, 8))
    return f"""
{base_context(seed, allow_web_search=True)}

City summary:
{json.dumps(city_outline.get("city_summary", {}), ensure_ascii=False, separators=(",", ":"))}

District group:
{json.dumps(group, ensure_ascii=False, separators=(",", ":"))}

Task: discover {target} real district/area/station-area candidates for this one
group only.

Output schema:
{{
  "group_name": "{group.get("group_name", "group")}",
  "district_candidates": [
    {{
      "name": "real area/neighborhood/station-area name",
      "role_hints": ["commercial","nightlife","hotel_cluster"],
      "task_affordances": ["restaurant_reservation","retail_pickup"],
      "failure_modes": ["crowding","transfer_complexity"],
      "keywords": ["Google Maps search terms"],
      "approx_center": {{"lat": null, "lng": null}},
      "evidence_summary": "short factual basis",
      "evidence_urls": ["https://..."]
    }}
  ],
  "source_notes": ["uncertainty or source notes"]
}}

Return only this JSON object. Do not include corridors, place plans, actors, or storylines.
""".strip()


def corridor_groups_refine_prompt(seed: CitySeed, city_outline: dict[str, Any], districts: list[dict[str, Any]]) -> str:
    district_names = [d.get("name", "") for d in districts if d.get("name")]
    return f"""
{base_context(seed, allow_web_search=False)}

City outline:
{json.dumps(city_outline, ensure_ascii=False, separators=(",", ":"))}

Discovered district names:
{json.dumps(district_names, ensure_ascii=False, separators=(",", ":"))}

Task: refine corridor group specs so later workers discover real mobility
backbones (transport_corridor) and at most a small number of service axes
(service_axis). Each group should target one identifiable city mobility
structure: metro line, rail trunk, road artery, river/bay crossing, airport
access, transfer spine, etc. Service axes (commercial/pedestrian density
bands) should be a clearly separate group, not mixed with transport groups.

Output schema:
{{
  "corridor_group_specs": [
    {{
      "group_name": "rail_intermodal",
      "corridor_class": "transport_corridor",
      "mode": "metro",
      "corridor_types": ["rail_corridor","intermodal_corridor"],
      "focus": "what this group should discover",
      "served_district_hints": ["district names"],
      "target_count": 5,
      "task_value": "why this group matters for Bench tasks"
    }}
  ],
  "source_notes": ["uncertainty or source notes"]
}}

Return 4 to 6 groups. At most 1 group should have corridor_class=service_axis;
the rest must be transport_corridor. Do not include actual corridor candidates.
""".strip()


def corridors_by_group_prompt(
    seed: CitySeed,
    city_outline: dict[str, Any],
    districts: list[dict[str, Any]],
    group: dict[str, Any],
) -> str:
    target = int(group.get("target_count") or 5)
    target = max(3, min(target, 8))
    district_names = [d.get("name", "") for d in districts if d.get("name")]
    allowed_types = group.get("corridor_types") or [
        "rail_corridor",
        "road_corridor",
        "bus_corridor",
        "pedestrian_corridor",
        "commercial_axis",
        "waterfront_axis",
        "airport_access",
        "intermodal_corridor",
    ]
    group_class = group.get("corridor_class", "transport_corridor")
    group_mode = group.get("mode", "mixed")
    return f"""
{base_context(seed, allow_web_search=True)}

Known districts:
{json.dumps(district_names, ensure_ascii=False, separators=(",", ":"))}

Corridor group:
{json.dumps(group, ensure_ascii=False, separators=(",", ":"))}

Task: discover {target} real corridors for this one group.

Hard requirements:
- Each corridor must be a REAL, named city structure verifiable on a public
  map: a metro/rail line segment, a major road, a named bridge or tunnel,
  an airport access route, a transfer spine, a famous commercial/pedestrian
  axis. Generic "service bands" are NOT corridors.
- ``corridor_class`` must be either ``transport_corridor`` (for mobility
  lines) or ``service_axis`` (for commercial/pedestrian density bands).
  Default to the group's class: ``{group_class}``.
- ``mode`` describes the dominant travel mode used to traverse it
  (metro, rail, walk, bus, drive, ferry, mixed). Default for this group:
  ``{group_mode}``.
- ``served_areas`` must list at least 2 district names from the discovered
  district set above. Corridors that cannot connect at least 2 districts
  should be dropped.
- ``node_names`` must be real, geocodable, ordered points along the
  corridor (stations, intersections, named landmarks). At least 3 nodes.
- Service axes may have only 1 service band; transport corridors must have
  at least 2 if dense places exist, but a transport corridor with weak
  surrounding services is still acceptable.

Allowed corridor_type values:
{json.dumps(allowed_types, ensure_ascii=False, separators=(",", ":"))}

Output schema:
{{
  "group_name": "{group.get("group_name", "group")}",
  "corridor_candidates": [
    {{
      "name": "real road, rail line, named bridge/tunnel, transfer path, named axis",
      "corridor_class": "transport_corridor or service_axis",
      "mode": "metro|rail|walk|bus|drive|ferry|mixed",
      "corridor_type": "one allowed value",
      "role_hints": ["commuter_spine","service_band"],
      "served_areas": ["at least 2 district names"],
      "node_names": ["ordered station/intersection/landmark names suitable for geocoding"],
      "corridor_service_bands": [
        {{
          "band_role": "restaurant/nightlife/retail/hotel/clinic/tourism/commuter-support",
          "place_types": ["Google Places types"],
          "text_queries": ["specific searches near corridor nodes or streets"],
          "density_expectation": "sparse|medium|dense",
          "task_value": "why this service band helps executable tasks"
        }}
      ],
      "time_varying_risk_hints": [
        {{"time_window": "window", "risk": "crowding|traffic|closure|weather_sensitive|last_train|transfer_complexity|tourist_surge", "segment_hint": "...", "task_consequence": "..."}}
      ],
      "impact_hints": {{
        "peak_sensitivity": "low|medium|high",
        "transfer_complexity": "low|medium|high",
        "disruption_scope": ["arrival_delay","reroute_required","district_isolation"]
      }},
      "evidence_summary": "short factual basis",
      "evidence_urls": ["https://..."]
    }}
  ],
  "source_notes": ["uncertainty or source notes"]
}}

Transport corridors must have at least 3 node_names. Service axes need at
least 2 node_names. Both must list at least 2 served_areas.
""".strip()


def structural_top_up_prompt(
    seed: CitySeed,
    city_outline: dict[str, Any],
    districts: list[dict[str, Any]],
    corridors: list[dict[str, Any]],
    *,
    kind: str,
    requested_count: int,
) -> str:
    """Request only new structural candidates after local validation found a gap."""
    existing = districts if kind == "districts" else corridors
    existing_names = [str(item.get("name", "")) for item in existing if item.get("name")]
    district_names = [str(item.get("name", "")) for item in districts if item.get("name")]
    if kind == "districts":
        schema = '''
  "district_candidates": [{
    "name": "real area/neighborhood/station-area name",
    "role_hints": ["commercial"],
    "task_affordances": ["service_choice"],
    "failure_modes": ["crowding"],
    "keywords": ["Google Maps search terms"],
    "evidence_summary": "short factual basis",
    "evidence_urls": ["https://..."]
  }]'''
        requirements = "Every district must include every schema field."
    else:
        schema = '''
  "corridor_candidates": [{
    "name": "real named mobility backbone or axis",
    "corridor_class": "transport_corridor or service_axis",
    "mode": "metro|rail|walk|bus|drive|ferry|mixed",
    "corridor_type": "*_corridor or *_axis",
    "role_hints": ["commuter_spine"],
    "served_areas": ["two exact district names"],
    "node_names": ["three ordered real geocodable nodes"],
    "corridor_service_bands": [{"band_role": "commuter-support", "place_types": ["cafe"], "text_queries": ["cafe near node"], "density_expectation": "medium", "task_value": "short"}],
    "evidence_summary": "short factual basis",
    "evidence_urls": ["https://..."]
  }]'''
        requirements = "Each corridor must have at least 3 nodes and serve at least 2 exact district names."
    return f"""
{base_context(seed, allow_web_search=True)}

This is a targeted structural top-up, not a full rediscovery.
City summary: {json.dumps(city_outline.get("city_summary", {}), ensure_ascii=False, separators=(",", ":"))}
Known districts: {json.dumps(district_names, ensure_ascii=False, separators=(",", ":"))}
Already accepted {kind}: {json.dumps(existing_names, ensure_ascii=False, separators=(",", ":"))}

Return {requested_count} NEW real {kind}. Do not repeat, rename, or describe
any already accepted item. {requirements}

Output schema:
{{
  "repair_kind": "{kind}",
{schema},
  "source_notes": ["short source note"]
}}
Return strict JSON only.
""".strip()


def place_plan_prompt(
    seed: CitySeed,
    districts: list[dict[str, Any]],
    corridors: list[dict[str, Any]],
) -> str:
    district_payload = [
        {
            "name": d.get("name", ""),
            "role_hints": d.get("role_hints", []),
            "task_affordances": d.get("task_affordances", []),
            "keywords": d.get("keywords", []),
        }
        for d in districts
    ]
    corridor_names = [c.get("name", "") for c in corridors[:20] if c.get("name")]
    return f"""
{base_context(seed, allow_web_search=False)}

Task: create Google Places search plans for this district batch only.

District batch:
{json.dumps(district_payload, ensure_ascii=False, separators=(",", ":"))}

Relevant corridor names:
{json.dumps(corridor_names, ensure_ascii=False, separators=(",", ":"))}

Allowed included_types:
{json.dumps(seed.place_types, ensure_ascii=False, separators=(",", ":"))}

Output schema:
{{
  "place_search_plan": [
    {{
      "district_name": "must match one district batch name exactly",
      "included_types": ["Google Places types worth querying"],
      "text_queries": ["specific local query strings for concrete services"],
      "task_roles": ["why these places matter for dynamic benchmark tasks"]
    }}
  ],
  "source_notes": ["uncertainty or source notes"]
}}

Return one plan per district in the batch. At most 10 included_types and 5
text_queries per district.
""".strip()


def dynamic_storyline_prompt(seed: CitySeed, districts: list[dict[str, Any]], corridors: list[dict[str, Any]]) -> str:
    district_names = [d.get("name", "") for d in districts if d.get("name")]
    corridor_summaries = [
        {"name": c.get("name", ""), "type": c.get("corridor_type", ""), "areas": c.get("served_areas", [])}
        for c in corridors[: seed.corridor_count + 5]
    ]
    return f"""
{base_context(seed, allow_web_search=False)}

Task: design reusable map/service dynamic storyline seeds for the discovered
city substrate. These are ingredients for later ScenarioDesignerAgent tools, not
final benchmark tasks. Do not design people, characters, or social relationships.

District names:
{json.dumps(district_names, ensure_ascii=False, separators=(",", ":"))}

Corridors:
{json.dumps(corridor_summaries, ensure_ascii=False, separators=(",", ":"))}

Output schema:
{{
  "dynamic_storylines": [
    {{
      "name": "short scenario pattern name",
      "goal_type": "restaurant_reservation|healthcare_visit|retail_pickup|tourist_plan|hotel_booking|multi_stop_errand",
      "geographic_setup": "district/corridor/place-type setup",
      "initial_information": ["what the agent might see first"],
      "dynamic_events": ["weather|traffic|transit|service_state|information_update changes after planning starts"],
      "success_condition": "what a correct agent must preserve"
    }}
  ],
  "source_notes": ["uncertainty or source notes"]
}}

Return 12 to 18 dynamic storylines.
""".strip()
