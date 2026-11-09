"""Read-only Google Routes cost simulation for Multi-Entity construction."""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from statistics import mean
from typing import Any

from task_module._shared.batch_state import attempt_seed, task_seed
from task_module.task_types.multi_entity.sampler import MultiEntitySampler
from task_module.task_types.route_planning.cost_planner import ReadOnlyRouteIndex, UNIT_PRICE_USD


@dataclass
class SimulationResult:
    requests: int
    route_reuse_hits: int
    coordinate_reuse_hits: int
    completed_tasks: int
    sampling_attempts: int
    branches: list[str]


def _origin(person: dict[str, Any]) -> dict[str, Any]:
    return {
        "place_id": str(person.get("home_place_id") or ""),
        "name": str(person.get("home_place_name") or person.get("name") or ""),
        "location": {"lat": person.get("home_lat", 0), "lng": person.get("home_lng", 0)},
    }


def _destination(venue: dict[str, Any]) -> dict[str, Any]:
    return {
        "place_id": str(venue.get("place_id") or ""),
        "name": str(venue.get("name") or ""),
        "location": venue.get("location") or {},
    }


def simulate_multi_entity_cost(
    *, world: dict[str, Any], characters: dict[str, Any], cache_payload: dict[str, Any],
    routing_place_ids: list[str], count: int, batch_seed: int, world_id: str,
    city_id: str, next_task_index: int = 1, max_sampling_attempts_per_task: int = 12,
) -> SimulationResult:
    """Simulate normal route preparation while leaving cache and world untouched."""
    route_index = ReadOnlyRouteIndex(cache_payload, world)
    routing_ids = {str(place_id) for place_id in routing_place_ids if place_id}
    routing_places = [
        place for place in world.get("places") or []
        if str(place.get("place_id") or "") in routing_ids
    ]
    requests = 0
    route_reuse_hits = 0
    coordinate_reuse_hits = 0
    completed_tasks = 0
    sampling_attempts = 0
    branches: list[str] = []

    while completed_tasks < max(0, int(count)):
        task_index = int(next_task_index) + completed_tasks
        selection: dict[str, Any] | None = None
        participants: list[dict[str, Any]] = []
        sampler: MultiEntitySampler | None = None
        for attempt_index in range(1, max(1, int(max_sampling_attempts_per_task)) + 1):
            sampling_attempts += 1
            task_seed_value = task_seed(batch_seed, world_id, city_id, "multi_entity", task_index)
            rng = random.Random(attempt_seed(task_seed_value, attempt_index))
            sampler = MultiEntitySampler(world, characters, route_index, rng=rng)
            participants = sampler.sample_participants(count=rng.randint(2, 4))
            if len(participants) < 2:
                continue
            selection = sampler.sample_activity_places(routing_places)
            if selection is not None:
                break
        if selection is None or sampler is None:
            break

        venues = [selection["target"], *(selection.get("neighbors") or [])]
        for person in participants:
            origin = _origin(person)
            for venue in venues:
                destination = _destination(venue)
                distance_m = sampler.route_sampler.distance(origin, destination)
                for mode in sampler.route_sampler._modes_for_distance(distance_m):
                    match_kind = route_index.match_kind(origin, destination, mode)
                    if match_kind == "route":
                        route_reuse_hits += 1
                    elif match_kind == "coordinate":
                        coordinate_reuse_hits += 1
                    else:
                        route_index.mark_planned(
                            (origin["place_id"], destination["place_id"], mode, "static_v1"),
                            origin, destination, mode,
                        )
                        requests += 1
        branches.append(str(selection.get("branch") or ""))
        completed_tasks += 1

    return SimulationResult(
        requests=requests, route_reuse_hits=route_reuse_hits,
        coordinate_reuse_hits=coordinate_reuse_hits, completed_tasks=completed_tasks,
        sampling_attempts=sampling_attempts, branches=branches,
    )


def build_multi_entity_cost_preview(
    *, world: dict[str, Any], characters: dict[str, Any], cache_payload: dict[str, Any],
    routing_place_ids: list[str], count: int, batch_seed: int, world_id: str,
    city_id: str, next_task_index: int = 1, samples: int = 24,
) -> dict[str, Any]:
    deterministic = simulate_multi_entity_cost(
        world=world, characters=characters, cache_payload=cache_payload,
        routing_place_ids=routing_place_ids, count=count, batch_seed=batch_seed,
        world_id=world_id, city_id=city_id, next_task_index=next_task_index,
    )
    if deterministic.completed_tasks != max(0, int(count)):
        raise ValueError("Multi-Entity cost preview could not sample the requested complete batch")
    outcomes: list[SimulationResult] = []
    for sample_index in range(max(8, int(samples))):
        sample_seed = int.from_bytes(hashlib.blake2b(
            f"{batch_seed}|multi-entity-cost|{sample_index}".encode(), digest_size=8,
        ).digest(), "big") & 0x7FFFFFFFFFFFFFFF
        outcome = simulate_multi_entity_cost(
            world=world, characters=characters, cache_payload=cache_payload,
            routing_place_ids=routing_place_ids, count=count, batch_seed=sample_seed,
            world_id=world_id, city_id=city_id, next_task_index=next_task_index,
        )
        if outcome.completed_tasks == max(0, int(count)):
            outcomes.append(outcome)

    if not outcomes:
        raise ValueError("Multi-Entity cost range has no complete simulated batch")

    minimum = min(outcomes, key=lambda item: item.requests)
    maximum = max(outcomes, key=lambda item: item.requests)

    def metric(result: SimulationResult) -> dict[str, Any]:
        return {
            "requests": result.requests,
            "route_reuse_hits": result.route_reuse_hits,
            "coordinate_reuse_hits": result.coordinate_reuse_hits,
            "completed_tasks": result.completed_tasks,
            "sampling_attempts": result.sampling_attempts,
            "branches": result.branches,
            "cost_usd": round(result.requests * UNIT_PRICE_USD, 2),
        }

    expected_requests = round(mean(item.requests for item in outcomes))
    return {
        "unit_price_usd": UNIT_PRICE_USD,
        "deterministic": metric(deterministic),
        "minimum": metric(minimum),
        "expected": {
            "requests": expected_requests,
            "route_reuse_hits": round(mean(item.route_reuse_hits for item in outcomes)),
            "coordinate_reuse_hits": round(mean(item.coordinate_reuse_hits for item in outcomes)),
            "completed_tasks": round(mean(item.completed_tasks for item in outcomes), 1),
            "sampling_attempts": round(mean(item.sampling_attempts for item in outcomes), 1),
            "cost_usd": round(expected_requests * UNIT_PRICE_USD, 2),
        },
        "maximum": metric(maximum),
        "sample_count": len(outcomes),
    }
