"""Segmented LLM + web-search discovery of real urban substrate candidates."""

from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from shared.llm_client import YunwuResponsesClient
from world_module.map.seed import CitySeed, seed_to_prompt_payload


CORRIDOR_GROUPS = [
    {
        "name": "rail_intermodal_airport",
        "types": "rail_corridor|airport_access|intermodal_corridor",
        "focus": "rail, metro, airport access, and major transfer paths that constrain timing and replanning",
    },
    {
        "name": "road_bus",
        "types": "road_corridor|bus_corridor",
        "focus": "named arterials, taxi/road axes, bus corridors, and expressway access that support service tasks",
    },
    {
        "name": "commercial_pedestrian",
        "types": "commercial_axis|pedestrian_corridor|intermodal_corridor",
        "focus": "station exits, shopping streets, underground passages, office-retail paths, and walkable service bands",
    },
    {
        "name": "waterfront_tourism_nightlife",
        "types": "waterfront_axis|commercial_axis|pedestrian_corridor",
        "focus": "waterfront, tourism, nightlife, event, and leisure axes with weather/crowding uncertainty",
    },
]


@dataclass
class YunwuWebSearchDiscovery:
    model: str = "gpt-5.5"
    config_path: str | None = None
    llm_max_workers: int = 4

    def discover(self, seed: CitySeed) -> dict[str, Any]:
        print(f"[discovery] city + districts via {self.model} web search", flush=True)
        district_doc = self._call_json(_build_city_district_prompt(seed), max_output_tokens=16000)
        districts = _dedupe_named(district_doc.get("district_candidates", []))
        if len(districts) > seed.district_count + 4:
            districts = districts[: seed.district_count + 4]
        print(f"[discovery] districts={len(districts)}", flush=True)

        corridor_target = max(4, math.ceil(seed.corridor_count / len(CORRIDOR_GROUPS)) + 2)
        corridor_jobs = [
            (group, corridor_target)
            for group in CORRIDOR_GROUPS
        ]
        print(
            f"[discovery] corridors in {len(corridor_jobs)} parallel groups, target_each={corridor_target}",
            flush=True,
        )
        corridor_docs = _parallel_map(
            corridor_jobs,
            lambda job: self._call_json(
                _build_corridor_prompt(seed, districts, job[0], job[1]),
                max_output_tokens=14000,
            ),
            max_workers=min(self.llm_max_workers, len(corridor_jobs)),
        )
        corridors = _dedupe_named(
            [
                corridor
                for doc in corridor_docs
                for corridor in doc.get("corridor_candidates", [])
            ]
        )[: seed.corridor_count + 5]
        print(f"[discovery] corridors={len(corridors)}", flush=True)

        district_batches = _chunks(districts, 6)
        print(f"[discovery] place search plans in {len(district_batches)} parallel batches", flush=True)
        place_docs = _parallel_map(
            district_batches,
            lambda batch: self._call_json(
                _build_place_plan_prompt(seed, batch, corridors),
                max_output_tokens=10000,
                use_web_search=False,
            ),
            max_workers=min(self.llm_max_workers, len(district_batches)),
        )
        place_search_plan = [
            plan
            for doc in place_docs
            for plan in doc.get("place_search_plan", [])
        ]
        print(f"[discovery] place_search_plan={len(place_search_plan)}", flush=True)

        print("[discovery] dynamic storylines", flush=True)
        story_doc = self._call_json(
            _build_dynamic_storyline_prompt(seed, districts, corridors),
            max_output_tokens=12000,
            use_web_search=False,
        )

        source_notes = []
        for doc in [district_doc, *corridor_docs, *place_docs, story_doc]:
            source_notes.extend(doc.get("source_notes", []))

        return {
            "city_summary": district_doc.get("city_summary", {}),
            "district_candidates": districts,
            "corridor_candidates": corridors,
            "place_search_plan": place_search_plan,
            "dynamic_storylines": story_doc.get("dynamic_storylines", []),
            "source_notes": source_notes,
            "discovery_policy": {
                "mode": "segmented_parallel_llm_web_search",
                "model": self.model,
                "llm_max_workers": self.llm_max_workers,
                "corridor_groups": [group["name"] for group in CORRIDOR_GROUPS],
            },
        }

    def _call_json(self, prompt: str, *, max_output_tokens: int, use_web_search: bool = True) -> dict[str, Any]:
        client = YunwuResponsesClient(model=self.model, config_path=self.config_path)
        extra: dict[str, Any] = {"max_output_tokens": max_output_tokens}
        if use_web_search:
            extra["tools"] = [{"type": "web_search_preview"}]
        response = client.call(
            prompt,
            extra=extra,
        )
        text = extract_response_text(response)
        data = extract_json_object(text)
        data.setdefault("source_response_excerpt", text[:600])
        return data


def _base_context(seed: CitySeed) -> str:
    payload = json.dumps(seed_to_prompt_payload(seed), ensure_ascii=False, separators=(",", ":"))
    return f"""
You are building real-data substrate for GeoAgentBench v7, a dynamic city-life
agent benchmark. Use web search for real public facts, but optimize the output
for executable benchmark construction, not encyclopedic completeness.

First principles:
- A district matters if it creates service choices, constraints, substitutions,
  local context, or realistic failure modes.
- A corridor matters if it carries mobility and also has service-bearing places
  around nodes, station exits, streets, or transfer points.
- Information should support future scenario design: crowding, stale data,
  service availability, weather exposure, transfer complexity, and social timing.

City seed JSON: {payload}

Return strict compact JSON only. No markdown. Keep strings short. Use real,
geocodable names. Use at most 2 evidence URLs per object.
""".strip()


def _build_city_district_prompt(seed: CitySeed) -> str:
    return f"""
{_base_context(seed)}

Task: discover the city summary and the main functional districts.

Output schema:
{{
  "city_summary": {{
    "name": "...",
    "country": "...",
    "urban_structure_notes": ["short facts"],
    "benchmark_design_intent": ["why this city supports dynamic service tasks"],
    "evidence_urls": ["https://..."]
  }},
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

Return {seed.district_count} to {seed.district_count + 4} districts. Do not
include corridors, place plans, actors, or storylines in this call.
""".strip()


def _build_corridor_prompt(
    seed: CitySeed,
    districts: list[dict[str, Any]],
    group: dict[str, str],
    target_count: int,
) -> str:
    district_names = [d.get("name", "") for d in districts if d.get("name")]
    return f"""
{_base_context(seed)}

Known district candidates:
{json.dumps(district_names, ensure_ascii=False, separators=(",", ":"))}

Task: discover about {target_count} corridors for group `{group["name"]}`.
Focus: {group["focus"]}.
Allowed corridor_type values for this call: {group["types"]}.

Output schema:
{{
  "corridor_candidates": [
    {{
      "name": "real road, rail line, transfer path, street, waterfront, or commercial axis",
      "corridor_type": "{group["types"]}",
      "role_hints": ["commuter_spine","service_band"],
      "served_areas": ["district names"],
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
      "evidence_summary": "short factual basis",
      "evidence_urls": ["https://..."]
    }}
  ],
  "source_notes": ["uncertainty or source notes"]
}}

Do not include districts, place plans, actors, or storylines in this call.
Each corridor must have at least 3 geocodable node_names and at least 2
service bands.
""".strip()


def _build_place_plan_prompt(
    seed: CitySeed,
    district_batch: list[dict[str, Any]],
    corridors: list[dict[str, Any]],
) -> str:
    districts_payload = [
        {
            "name": d.get("name", ""),
            "role_hints": d.get("role_hints", []),
            "task_affordances": d.get("task_affordances", []),
            "keywords": d.get("keywords", []),
        }
        for d in district_batch
    ]
    corridor_names = [c.get("name", "") for c in corridors[:20] if c.get("name")]
    return f"""
{_base_context(seed)}

Task: create Google Places search plans for this district batch only.

District batch:
{json.dumps(districts_payload, ensure_ascii=False, separators=(",", ":"))}

Relevant corridor names:
{json.dumps(corridor_names, ensure_ascii=False, separators=(",", ":"))}

Allowed included_types should come from or be compatible with:
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

Return one plan per district in the batch. Keep each plan focused: at most 10
included_types and at most 5 text_queries.
""".strip()


def _build_dynamic_storyline_prompt(
    seed: CitySeed,
    districts: list[dict[str, Any]],
    corridors: list[dict[str, Any]],
) -> str:
    district_names = [d.get("name", "") for d in districts if d.get("name")]
    corridor_summaries = [
        {"name": c.get("name", ""), "type": c.get("corridor_type", ""), "areas": c.get("served_areas", [])}
        for c in corridors[: seed.corridor_count]
    ]
    return f"""
{_base_context(seed)}

Task: design reusable map/service dynamic storyline seeds for this city
substrate. These are not final benchmark tasks; they are ingredients for a
ScenarioDesignerAgent that will later use world tools. Do not design people,
characters, or social relationships.

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


def extract_response_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    chunks: list[str] = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    if chunks:
        return "\n".join(chunks)
    recursive_chunks = _collect_text_fields(response)
    if recursive_chunks:
        return "\n".join(recursive_chunks)
    return json.dumps(response, ensure_ascii=False)


def extract_json_object(text: str) -> dict[str, Any]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if not match:
            raise ValueError("Yunwu response did not contain a JSON object")
        return json.loads(match.group(0))


def _parallel_map(items: list[Any], fn: Any, max_workers: int) -> list[Any]:
    if not items:
        return []
    workers = max(1, min(max_workers, len(items)))
    if workers == 1:
        return [fn(item) for item in items]
    results: list[Any] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_index = {executor.submit(fn, item): index for index, item in enumerate(items)}
        for future in as_completed(future_to_index):
            results[future_to_index[future]] = future.result()
    return results


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _dedupe_named(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    out = []
    for item in items:
        name = str(item.get("name", "")).strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(item)
    return out


def _collect_text_fields(value: Any) -> list[str]:
    chunks: list[str] = []
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            chunks.append(text)
        for child in value.values():
            chunks.extend(_collect_text_fields(child))
    elif isinstance(value, list):
        for child in value:
            chunks.extend(_collect_text_fields(child))
    return chunks
