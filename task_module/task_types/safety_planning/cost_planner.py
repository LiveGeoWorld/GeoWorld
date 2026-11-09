"""Read-only Google Routes cost preview for Safety Planning construction."""

from __future__ import annotations

import random
import hashlib
from dataclasses import dataclass
from statistics import mean
from typing import Any

from task_module._shared.batch_state import attempt_seed, task_seed
from task_module.task_types.route_planning.cost_planner import ReadOnlyRouteIndex, UNIT_PRICE_USD
from task_module.task_types.safety_planning.generator import _resolve_safety_routing_place_ids
from task_module.task_types.safety_planning.sampler import SafetySampler
from task_module.task_types.safety_planning.local_candidates import local_activity


@dataclass
class SafetyCostResult:
    requests: int = 0
    route_reuse_hits: int = 0
    coordinate_reuse_hits: int = 0
    candidate_places: int = 0
    completed_slots: int = 0


def safety_routing_place_ids(
    world: dict[str, Any], route_pool_payload: dict[str, Any], cache_payload: dict[str, Any],
) -> set[str]:
    return _resolve_safety_routing_place_ids(
        world, route_pool_payload, list(cache_payload.get("cache") or []),
    )


def _origin(character: dict[str, Any]) -> dict[str, Any]:
    return {
        "place_id": str(character.get("home_place_id") or ""),
        "name": str(character.get("home_place_name") or character.get("name") or ""),
        "location": {
            "lat": character.get("home_lat", 0),
            "lng": character.get("home_lng", 0),
        },
    }


def _count_routes(
    index: ReadOnlyRouteIndex, sampler: SafetySampler, origin: dict[str, Any],
    destinations: list[dict[str, Any]],
) -> tuple[int, int, int]:
    requests = route_hits = coordinate_hits = 0
    for destination in destinations:
        if not origin.get("place_id") or not destination.get("place_id"):
            continue
        distance_m = sampler.route_sampler.distance(origin, destination)
        for mode in sampler.route_sampler._modes_for_distance(distance_m):
            kind = index.match_kind(origin, destination, mode)
            if kind == "route":
                route_hits += 1
            elif kind == "coordinate":
                coordinate_hits += 1
            else:
                requests += 1
                index.mark_planned(
                    (str(origin["place_id"]), str(destination["place_id"]), mode, "static_v1"),
                    origin, destination, mode,
                )
    return requests, route_hits, coordinate_hits


def simulate_safety_cost(
    *, world: dict[str, Any], characters: dict[str, Any], cache_payload: dict[str, Any],
    route_pool_payload: dict[str, Any], count: int, batch_seed: int, world_id: str,
    city_id: str, next_task_index: int = 1, sample_index: int = 0,
    structural_maximum: bool = False,
) -> SafetyCostResult:
    routing_ids = safety_routing_place_ids(world, route_pool_payload, cache_payload)
    if not routing_ids:
        raise ValueError("当前城市没有可供 Safety Planning 使用的 Routing POI池。")
    index = ReadOnlyRouteIndex(cache_payload, world)
    result = SafetyCostResult()
    for offset in range(max(0, int(count))):
        task_index = int(next_task_index) + offset
        seed_value = task_seed(batch_seed, world_id, city_id, "safety_planning", task_index)
        rng = random.Random(attempt_seed(seed_value, 1))
        sampler = SafetySampler(
            world, characters, index, rng=rng, routing_place_ids=routing_ids,
        )
        character = sampler.sample_character()
        risky = sampler.sample_risky_target() if character else None
        if not character or not risky:
            raise ValueError("Safety preview cannot reproduce the fixed character and target")
        activity = local_activity(
            places=world.get("places") or [], services=world.get("services") or [],
            target_id=risky["place_id"], selected_service_type=risky["service_type"],
        )
        by_id = {str(place.get("place_id") or ""): place for place in world.get("places") or []}
        local_ids = [str(pid) for pid in activity.get("local_place_ids") or []]
        candidates = [by_id[pid] for pid in local_ids if pid in by_id and pid != risky["place_id"]]
        if not candidates:
            raise ValueError("Safety requires an ordinary competitor in Routing coverage")
        if structural_maximum:
            selected = candidates
        else:
            decision_seed = int.from_bytes(hashlib.blake2b(
                f"{batch_seed}|safety-cost|{sample_index}|{task_index}".encode(), digest_size=8,
            ).digest(), "big")
            decision_rng = random.Random(decision_seed)
            # Explicit uninformed proxy, not a semantic candidate selector or
            # a learned distribution of construction-Agent decisions.
            selected = decision_rng.sample(candidates, decision_rng.randint(1, len(candidates)))
        destinations = [risky, *selected]
        requests, route_hits, coordinate_hits = _count_routes(
            index, sampler, _origin(character), destinations,
        )
        result.requests += requests
        result.route_reuse_hits += route_hits
        result.coordinate_reuse_hits += coordinate_hits
        result.candidate_places += len(destinations)
        result.completed_slots += 1
    return result


def _metric(result: SafetyCostResult) -> dict[str, Any]:
    return {
        "requests": result.requests,
        "route_reuse_hits": result.route_reuse_hits,
        "coordinate_reuse_hits": result.coordinate_reuse_hits,
        "candidate_places": result.candidate_places,
        "completed_slots": result.completed_slots,
        "cost_usd": round(result.requests * UNIT_PRICE_USD, 2),
    }


def build_safety_cost_preview(*, samples: int = 24, **kwargs: Any) -> dict[str, Any]:
    maximum = simulate_safety_cost(**kwargs, structural_maximum=True)
    outcomes = [simulate_safety_cost(**kwargs, sample_index=index)
                for index in range(max(8, int(samples)))]
    expected_requests = mean(item.requests for item in outcomes)
    expected = {
        "requests": round(expected_requests, 2),
        "route_reuse_hits": round(mean(item.route_reuse_hits for item in outcomes), 2),
        "coordinate_reuse_hits": round(mean(item.coordinate_reuse_hits for item in outcomes), 2),
        "candidate_places": round(mean(item.candidate_places for item in outcomes), 2),
        "completed_slots": maximum.completed_slots,
        "cost_usd": round(expected_requests * UNIT_PRICE_USD, 2),
    }
    return {
        "unit_price_usd": UNIT_PRICE_USD,
        # C can omit route constraints; no universally mandatory uncached
        # edge has been established for the new choice contract.
        "minimum": _metric(SafetyCostResult(completed_slots=maximum.completed_slots)),
        "expected": expected,
        "maximum": _metric(maximum),
        "sample_count": len(outcomes),
        "estimate_kind": "bounded_agent_proxy_simulation",
        "upper_bound_scope": "retained_task_structure_only",
        "excluded_from_upper_bound": ["rejected_candidate_attempts", "infrastructure_retries"],
        "proxy_assumptions": {
            "fixed": "shared task/attempt seed character; target is a structural proxy, not a prediction of the time-first generator",
            "time_coverage": "does not simulate point-time opening selection or evolving occupancy history",
            "retained_competitor_count": "uniform from 1 to available Routing competitors",
            "retained_competitor_membership": "uniform subset without replacement; not a semantic prediction",
            "route_modes": "existing distance-based mode policy",
            "minimum": "zero: new choice contract establishes no mandatory uncached route",
            "maximum": "all available competitors retained once; no rejected-attempt multiplier",
            "model_decision_distribution": "uninformed proxy, not measured Agent probabilities",
        },
    }
