"""Read-only Google Routes cost preview for Region Stage 3 construction."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from task_module._shared.batch_state import attempt_seed, task_seed
from task_module.task_types.region_aware.generator import (
    _character_route_endpoint,
    _resolve_region_routing_place_ids,
    _routing_preferred_candidates,
    _stage3_local_candidate_pool,
    _stage3_route_modes_for,
)
from task_module.task_types.region_aware.stage3_candidate_builder import Stage3CandidateBuilder
from task_module.task_types.route_planning.cost_planner import ReadOnlyRouteIndex, UNIT_PRICE_USD


@dataclass
class RegionCostResult:
    requests: int = 0
    route_reuse_hits: int = 0
    coordinate_reuse_hits: int = 0
    candidate_places: int = 0
    completed_slots: int = 0


def region_routing_place_ids(
    world: dict[str, Any], route_pool_payload: dict[str, Any], cache_payload: dict[str, Any],
) -> set[str]:
    return _resolve_region_routing_place_ids(
        world, route_pool_payload, list(cache_payload.get("cache") or []),
    )


def _count_group(
    index: ReadOnlyRouteIndex, character: dict[str, Any], places: list[dict[str, Any]],
) -> tuple[int, int, int]:
    origin = _character_route_endpoint(character)
    requests = route_hits = coordinate_hits = 0
    for place in places:
        for mode in _stage3_route_modes_for(character, place):
            kind = index.match_kind(origin, place, mode)
            if kind == "route":
                route_hits += 1
            elif kind == "coordinate":
                coordinate_hits += 1
            else:
                requests += 1
                index.mark_planned(
                    (str(origin["place_id"]), str(place["place_id"]), mode, "static_v1"),
                    origin, place, mode,
                )
    return requests, route_hits, coordinate_hits


def build_region_cost_preview(
    *, world: dict[str, Any], characters: dict[str, Any], cache_payload: dict[str, Any],
    route_pool_payload: dict[str, Any], rules_asset: dict[str, Any],
    stage2_asset: dict[str, Any], service_detail_cache: dict[str, Any], count: int,
    batch_seed: int, world_id: str, city_id: str, next_task_index: int = 1,
) -> dict[str, Any]:
    routing_ids = region_routing_place_ids(world, route_pool_payload, cache_payload)
    if not routing_ids:
        raise ValueError("当前城市没有可供 Region 使用的 Routing POI池。")
    builder = Stage3CandidateBuilder(
        world=world, characters=characters, rules_asset=rules_asset,
        stage2_asset=stage2_asset, service_detail_cache=service_detail_cache,
    )
    pools = list(builder.inspect()["rule_type_pools"])
    people = [
        item for item in characters.get("characters") or []
        if isinstance(item, dict) and item.get("character_id")
    ]
    if not pools or not people:
        raise ValueError("Region 缺少可用的地域规则候选或人物。")
    random.Random(batch_seed).shuffle(pools)
    index = ReadOnlyRouteIndex(cache_payload, world)
    result = RegionCostResult()
    character_use = {str(item["character_id"]): 0 for item in people}
    rule_use = {str(item["rule_id"]): 0 for item in pools}
    seen: set[tuple[str, str, str]] = set()
    exhausted: set[tuple[str, str]] = set()
    slot_attempt = 0
    maximum_attempts = max(1, len(people) * len(pools) * 2)
    while result.completed_slots < max(0, int(count)) and slot_attempt < maximum_attempts:
        slot_attempt += 1
        task_index = int(next_task_index) + result.completed_slots
        rng = random.Random(attempt_seed(
            task_seed(batch_seed, world_id, city_id, "region_aware", task_index), slot_attempt,
        ))
        available = [
            (person, pool) for person in people for pool in pools
            if (str(person["character_id"]), str(pool["rule_id"])) not in exhausted
        ]
        if not available:
            break
        minimum_use = min(
            (character_use[str(person["character_id"])], rule_use[str(pool["rule_id"])])
            for person, pool in available
        )
        person, pool = rng.choice([
            pair for pair in available
            if (character_use[str(pair[0]["character_id"])], rule_use[str(pair[1]["rule_id"])]) == minimum_use
        ])
        rule_id = str(pool["rule_id"])
        matched, _telemetry = builder.retrieve_rule_candidates(rule_id)
        eligible = [
            item for item in matched
            if (str(person["character_id"]), rule_id, str(item["place"].get("place_id") or "")) not in seen
        ]
        if not eligible:
            exhausted.add((str(person["character_id"]), rule_id))
            continue
        target = rng.choice(_routing_preferred_candidates(eligible, routing_ids))
        target_place = target["place"]
        related = builder.places_for_rule(rule_id)
        local = _stage3_local_candidate_pool(target_place, related)
        places_by_id = {
            str(place.get("place_id") or ""): place
            for place in [target_place, *local, *[item["place"] for item in matched]]
            if place.get("place_id")
        }
        requests, route_hits, coordinate_hits = _count_group(index, person, list(places_by_id.values()))
        result.requests += requests
        result.route_reuse_hits += route_hits
        result.coordinate_reuse_hits += coordinate_hits
        result.candidate_places += len(places_by_id)
        result.completed_slots += 1
        character_use[str(person["character_id"])] += 1
        rule_use[rule_id] += 1
        seen.add((str(person["character_id"]), rule_id, str(target_place["place_id"])))
        slot_attempt = 0
    metric = {
        "requests": result.requests,
        "route_reuse_hits": result.route_reuse_hits,
        "coordinate_reuse_hits": result.coordinate_reuse_hits,
        "candidate_places": result.candidate_places,
        "completed_slots": result.completed_slots,
        "cost_usd": round(result.requests * UNIT_PRICE_USD, 2),
    }
    eligible_types = {
        str(value)
        for pool in pools
        for value in pool.get("affected_poi_primary_types") or []
    }
    relevant_place_count = len([
        place for place in world.get("places") or []
        if str(place.get("primary_type") or "") in eligible_types
    ])
    # A failed Stage-3 candidate may advance through the finite person/rule/POI
    # inventory. Three modes per person-place pair is a conservative arithmetic
    # ceiling and avoids an expensive full candidate-retrieval scan in the UI.
    maximum_requests = len(people) * relevant_place_count * 3
    maximum_metric = {
        "requests": maximum_requests,
        "route_reuse_hits": 0,
        "coordinate_reuse_hits": 0,
        "candidate_places": len(people) * relevant_place_count,
        "completed_slots": len(people),
        "cost_usd": round(maximum_requests * UNIT_PRICE_USD, 2),
    }
    minimum_metric = {
        "requests": 0, "route_reuse_hits": 0, "coordinate_reuse_hits": 0,
        "candidate_places": 0, "completed_slots": 0, "cost_usd": 0.0,
    }
    return {
        "unit_price_usd": UNIT_PRICE_USD,
        "minimum": minimum_metric,
        "expected": dict(metric),
        "maximum": maximum_metric,
        "deterministic": dict(metric),
        "estimate_kind": "seeded_stage3_route_materialization",
    }
