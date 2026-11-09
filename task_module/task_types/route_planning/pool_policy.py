"""Shared Routing POI-pool sampling policy for generation and dry-run planning."""

from __future__ import annotations

import random
from typing import Any

from task_module.task_types.route_planning.sampler import RouteSampler


def sample_route_pool_context(
    sampler: RouteSampler, route_pool: Any, rng: random.Random,
    *, diffusion_start: int = 20,
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """Sample one attempt according to the confirmed three-stage pool policy."""
    pool_places = route_pool.places
    route_pool.last_added = []
    pool_ids = route_pool.place_id_set
    pool_size = len(pool_places)

    if pool_size < diffusion_start:
        stage = "bootstrap"
        core = sampler.sample_core_pois()
        distractors = _nearby_for_each_core(sampler, core, pool_ids, rng, 4, 5)
    elif pool_size >= route_pool.max_pois:
        stage = "reuse_full"
        core = _sample_reuse_core(sampler, route_pool, pool_places)
        distractors = []
    else:
        stage = rng.choices(
            ["reuse", "diffusion", "new_origin"], weights=[20, 70, 10], k=1,
        )[0]
        if stage == "reuse":
            core = _sample_reuse_core(sampler, route_pool, pool_places)
            distractors = []
        elif stage == "diffusion":
            _anchor, expansion_core = sampler.sample_expansion_core(pool_places)
            if expansion_core is None:
                return empty_core(), [], stage
            core = sampler.sample_core_pois(
                places=[*pool_places, expansion_core], required_place=expansion_core,
            )
            excluded = pool_ids | core_place_ids(core)
            distractors = sampler.sample_nearby_unused(
                expansion_core, excluded, rng.randint(3, 4), 2000, 5000,
            )
        else:
            if route_pool.max_pois - pool_size < 2:
                return _sample_reuse_core(sampler, route_pool, pool_places), [], "reuse_capacity_guard"
            unused_places = [
                place for place in sampler.places
                if place.get("place_id") not in pool_ids
            ]
            core = sampler.sample_core_pois(places=unused_places)
            distractors = _nearby_for_each_core(sampler, core, pool_ids, rng, 4, 5)

    if core.get("origin") and core.get("dest"):
        route_pool.add([*core_places(core), *distractors])
    return core, distractors, stage


def _sample_reuse_core(sampler: RouteSampler, route_pool: Any, pool_places: list[dict]) -> dict[str, Any]:
    return (
        route_pool.sample_cached_core(sampler, pool_places)
        if route_pool.graph_mode == "legacy_sparse" and hasattr(route_pool, "sample_cached_core")
        else sampler.sample_cached_core(pool_places, route_pool._route_cache)
        if route_pool.graph_mode == "legacy_sparse"
        else sampler.sample_core_pois(places=pool_places)
    )


def _nearby_for_each_core(
    sampler: RouteSampler, core: dict[str, Any], pool_ids: set[str], rng: random.Random,
    minimum: int, maximum: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    excluded = set(pool_ids) | core_place_ids(core)
    for place in core_places(core):
        nearby = sampler.sample_nearby_unused(
            place, excluded, rng.randint(minimum, maximum), 2000, 5000,
        )
        selected.extend(nearby)
        excluded.update(item.get("place_id") for item in nearby)
    return selected


def core_places(core: dict[str, Any]) -> list[dict[str, Any]]:
    if not core.get("origin") or not core.get("dest"):
        return []
    return [core["origin"], *(core.get("waypoints") or []), core["dest"]]


def core_place_ids(core: dict[str, Any]) -> set[str]:
    return {str(place.get("place_id") or "") for place in core_places(core)}


def empty_core() -> dict[str, Any]:
    return {"origin": None, "dest": None, "waypoint": None, "waypoints": []}
