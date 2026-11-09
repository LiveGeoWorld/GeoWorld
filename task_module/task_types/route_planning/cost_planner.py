"""Read-only Google Routes cost planning for the shared Routing POI pool."""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from statistics import mean
from typing import Any

from task_module._shared.batch_state import attempt_seed, task_seed
from task_module.task_types.route_planning.pool_policy import sample_route_pool_context
from task_module.task_types.route_planning.sampler import RouteSampler


SNAPSHOT_ID = "static_v1"
UNIT_PRICE_USD = 0.005


def _location_key(place: dict[str, Any]) -> tuple[float, float] | None:
    location = place.get("location") or {}
    try:
        return round(float(location["lat"]), 6), round(float(location["lng"]), 6)
    except (KeyError, TypeError, ValueError):
        return None


def _route_key(origin: dict, destination: dict, mode: str) -> tuple[str, str, str, str]:
    return (
        str(origin.get("place_id") or ""), str(destination.get("place_id") or ""),
        str(mode), SNAPSHOT_ID,
    )


class ReadOnlyRouteIndex:
    """Cache lookup contract used by both legacy-chain sampling and cost planning."""

    def __init__(
        self, payload: dict[str, Any], world: dict[str, Any],
        planned_keys: set[tuple[str, str, str, str]] | None = None,
    ) -> None:
        self._cache: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._coordinates: dict[tuple[tuple[float, float], tuple[float, float], str, str], dict[str, Any]] = {}
        self._route_ids = {
            str(entry.get("route_id") or ""): entry
            for entry in payload.get("cache") or [] if isinstance(entry, dict)
        }
        self._planned_keys = set(planned_keys or ())
        self._planned_coordinates: set[
            tuple[tuple[float, float], tuple[float, float], str, str]
        ] = set()
        world_by_id = {
            str(place.get("place_id") or ""): place
            for place in world.get("places") or [] if isinstance(place, dict)
        }
        for raw in payload.get("cache") or []:
            if not isinstance(raw, dict):
                continue
            entry = raw
            route_ref = str(raw.get("route_ref") or "")
            if route_ref and route_ref in self._route_ids:
                entry = {**raw, "route": self._route_ids[route_ref].get("route") or {}}
            origin_id = str(raw.get("origin_place_id") or "")
            destination_id = str(raw.get("dest_place_id") or "")
            mode = str(raw.get("mode") or "")
            snapshot = str(raw.get("snapshot_id") or SNAPSHOT_ID)
            self._cache[(origin_id, destination_id, mode, snapshot)] = entry
            origin = world_by_id.get(origin_id) or {"location": {"lat": raw.get("origin_lat"), "lng": raw.get("origin_lng")}}
            destination = world_by_id.get(destination_id) or {"location": {"lat": raw.get("dest_lat"), "lng": raw.get("dest_lng")}}
            origin_location = _location_key(origin)
            destination_location = _location_key(destination)
            if origin_location and destination_location:
                self._coordinates[(origin_location, destination_location, mode, snapshot)] = entry

    def get(self, origin: dict, destination: dict, mode: str, snapshot_id: str = SNAPSHOT_ID) -> dict[str, Any] | None:
        logical_key = _route_key(origin, destination, mode)
        if logical_key in self._planned_keys:
            return {"route": {"available": True, "duration_s": 1}}
        exact = self._cache.get(logical_key)
        if exact is not None:
            return exact
        origin_location = _location_key(origin)
        destination_location = _location_key(destination)
        if not origin_location or not destination_location:
            return None
        return self._coordinates.get((origin_location, destination_location, str(mode), snapshot_id))

    def contains(self, origin: dict, destination: dict, mode: str) -> bool:
        return self.get(origin, destination, mode) is not None

    def match_kind(self, origin: dict, destination: dict, mode: str, snapshot_id: str = SNAPSHOT_ID) -> str:
        logical_key = _route_key(origin, destination, mode)
        if logical_key in self._planned_keys or logical_key in self._cache:
            return "route"
        origin_location = _location_key(origin)
        destination_location = _location_key(destination)
        if not origin_location or not destination_location:
            return "missing"
        coordinate_key = (origin_location, destination_location, str(mode), snapshot_id)
        return (
            "coordinate"
            if coordinate_key in self._coordinates or coordinate_key in self._planned_coordinates
            else "missing"
        )

    def mark_planned(
        self, key: tuple[str, str, str, str], origin: dict, destination: dict, mode: str,
        snapshot_id: str = SNAPSHOT_ID,
    ) -> None:
        self._planned_keys.add(key)
        origin_location = _location_key(origin)
        destination_location = _location_key(destination)
        if origin_location and destination_location:
            self._planned_coordinates.add((
                origin_location, destination_location, str(mode), snapshot_id,
            ))


class VirtualRoutePool:
    """In-memory RoutePoiPool equivalent; add() never writes formal assets."""

    def __init__(
        self, world: dict[str, Any], place_ids: list[str], max_pois: int,
        graph_mode: str, route_cache: ReadOnlyRouteIndex,
    ) -> None:
        self._places = {
            str(place.get("place_id") or ""): place
            for place in world.get("places") or [] if place.get("place_id")
        }
        self.place_ids = [place_id for place_id in place_ids if place_id in self._places]
        self.max_pois = max(2, int(max_pois))
        self.graph_mode = str(graph_mode or "dense")
        self._route_cache = route_cache
        self.last_added: list[dict[str, Any]] = []
        self._cached_adjacency_key: tuple[str, ...] = ()
        self._cached_adjacency: dict[str, dict[str, set[str]]] = {}

    @property
    def places(self) -> list[dict[str, Any]]:
        return [self._places[place_id] for place_id in self.place_ids if place_id in self._places]

    @property
    def place_id_set(self) -> set[str]:
        return set(self.place_ids)

    def add(self, places: list[dict[str, Any]]) -> list[dict[str, Any]]:
        existing = self.place_id_set
        added: list[dict[str, Any]] = []
        for place in places:
            place_id = str(place.get("place_id") or "")
            if not place_id or place_id in existing or place_id not in self._places:
                continue
            if len(self.place_ids) >= self.max_pois:
                break
            self.place_ids.append(place_id)
            existing.add(place_id)
            added.append(self._places[place_id])
        self.last_added = added
        if added:
            self._cached_adjacency_key = ()
            self._cached_adjacency = {}
        return added

    def sample_cached_core(self, sampler: RouteSampler, pool_places: list[dict]) -> dict[str, Any]:
        pool_key = tuple(self.place_ids)
        if pool_key != self._cached_adjacency_key:
            self._cached_adjacency = sampler.build_cached_adjacency(pool_places, self._route_cache)
            self._cached_adjacency_key = pool_key
        by_id = {str(place.get("place_id") or ""): place for place in pool_places}
        return sampler.sample_cached_core_from_adjacency(by_id, self._cached_adjacency)


@dataclass
class SimulationResult:
    requests: int
    route_reuse_hits: int
    coordinate_reuse_hits: int
    new_pois: int
    final_pool_size: int
    stages: list[str]
    requested_keys: set[tuple[str, str, str, str]]
    pool_ids: list[str]


def _missing_pairs(
    sampler: RouteSampler, pool: VirtualRoutePool, cache: ReadOnlyRouteIndex,
    planned: set[tuple[str, str, str, str]], *, include_existing_dense: bool,
) -> tuple[int, int, int]:
    new_ids = {str(place.get("place_id") or "") for place in pool.last_added}
    if not new_ids and not include_existing_dense:
        return 0, 0, 0
    pairs = sampler.generate_pool_pairs(pool.places)
    if pool.graph_mode == "legacy_sparse":
        pairs = [
            pair for pair in pairs
            if str(pair["from"].get("place_id") or "") in new_ids
            or str(pair["to"].get("place_id") or "") in new_ids
        ]
    requests = 0
    route_reuse_hits = 0
    coordinate_reuse_hits = 0
    for pair in pairs:
        key = _route_key(pair["from"], pair["to"], pair["mode"])
        match_kind = cache.match_kind(pair["from"], pair["to"], pair["mode"])
        if match_kind == "route":
            pass
        elif match_kind == "coordinate":
            if new_ids and (
                str(pair["from"].get("place_id") or "") in new_ids
                or str(pair["to"].get("place_id") or "") in new_ids
            ):
                coordinate_reuse_hits += 1
        else:
            planned.add(key)
            cache.mark_planned(key, pair["from"], pair["to"], pair["mode"])
            requests += 1
    return requests, route_reuse_hits, coordinate_reuse_hits


def _selected_route_reuse_hits(core: dict[str, Any], cache: ReadOnlyRouteIndex) -> int:
    chain = [core.get("origin"), *(core.get("waypoints") or []), core.get("dest")]
    if any(not isinstance(place, dict) for place in chain):
        return 0
    leg_modes = list(core.get("leg_modes") or [])
    if len(leg_modes) != len(chain) - 1:
        mode = str(core.get("mode") or "")
        if not mode or mode == "MIXED":
            return 0
        leg_modes = [mode] * (len(chain) - 1)
    return sum(
        1 for origin, destination, mode in zip(chain, chain[1:], leg_modes)
        if cache.match_kind(origin, destination, str(mode)) == "route"
    )


def simulate_route_pool(
    *, world: dict[str, Any], cache_payload: dict[str, Any], initial_pool_ids: list[str],
    graph_mode: str, count: int, diffusion_start: int, pool_max: int,
    batch_seed: int, world_id: str, city_id: str, next_task_index: int = 1,
    attempt_count: int | None = None, continue_to_limit: bool = False,
    initial_planned_keys: set[tuple[str, str, str, str]] | None = None,
) -> SimulationResult:
    cache = ReadOnlyRouteIndex(cache_payload, world, initial_planned_keys)
    pool = VirtualRoutePool(world, initial_pool_ids, pool_max, graph_mode, cache)
    sampler = RouteSampler(world, {}, rng=random.Random(batch_seed))
    planned: set[tuple[str, str, str, str]] = set(initial_planned_keys or ())
    requests = 0
    route_reuse_hits = 0
    coordinate_reuse_hits = 0
    start_size = len(pool.place_ids)
    if pool.graph_mode != "legacy_sparse":
        recovered, recovered_route_hits, recovered_coordinate_hits = _missing_pairs(
            sampler, pool, cache, planned, include_existing_dense=True,
        )
        requests += recovered
        route_reuse_hits += recovered_route_hits
        coordinate_reuse_hits += recovered_coordinate_hits

    total_attempts = max(0, int(attempt_count if attempt_count is not None else count))
    stages: list[str] = []
    stagnant = 0
    attempt = 0
    while attempt < total_attempts or (continue_to_limit and len(pool.place_ids) < pool.max_pois and stagnant < 80):
        if continue_to_limit:
            task_index = next_task_index + attempt
            task_attempt = 1
        elif attempt < count:
            task_index = next_task_index + attempt
            task_attempt = 1
        else:
            task_index = next_task_index + max(0, count - 1)
            task_attempt = attempt - count + 2
        task_seed_value = task_seed(batch_seed, world_id, city_id, "route_planning", task_index)
        rng = random.Random(attempt_seed(task_seed_value, task_attempt))
        sampler = RouteSampler(world, {}, rng=rng)
        before = len(pool.place_ids)
        core, _distractors, stage = sample_route_pool_context(
            sampler, pool, rng, diffusion_start=diffusion_start,
        )
        stages.append(stage)
        if core.get("origin") and core.get("dest"):
            if stage in {"reuse", "reuse_full", "reuse_capacity_guard"}:
                route_reuse_hits += _selected_route_reuse_hits(core, cache)
            new_requests, new_route_hits, new_coordinate_hits = _missing_pairs(
                sampler, pool, cache, planned, include_existing_dense=False,
            )
            requests += new_requests
            route_reuse_hits += new_route_hits
            coordinate_reuse_hits += new_coordinate_hits
        stagnant = stagnant + 1 if len(pool.place_ids) == before else 0
        attempt += 1
        if not continue_to_limit and attempt >= total_attempts:
            break
        if len(pool.place_ids) >= pool.max_pois:
            if continue_to_limit or attempt >= total_attempts:
                break

    return SimulationResult(
        requests=requests, route_reuse_hits=route_reuse_hits,
        coordinate_reuse_hits=coordinate_reuse_hits,
        new_pois=len(pool.place_ids) - start_size,
        final_pool_size=len(pool.place_ids), stages=stages, requested_keys=planned,
        pool_ids=list(pool.place_ids),
    )


def build_route_cost_preview(
    *, world: dict[str, Any], cache_payload: dict[str, Any], initial_pool_ids: list[str],
    graph_mode: str, count: int, diffusion_start: int, pool_max: int,
    batch_seed: int, world_id: str, city_id: str, next_task_index: int = 1,
    samples: int = 24,
) -> dict[str, Any]:
    deterministic = simulate_route_pool(
        world=world, cache_payload=cache_payload, initial_pool_ids=initial_pool_ids,
        graph_mode=graph_mode, count=count, diffusion_start=diffusion_start, pool_max=pool_max,
        batch_seed=batch_seed, world_id=world_id, city_id=city_id, next_task_index=next_task_index,
    )
    outcomes: list[SimulationResult] = []
    long_term: list[SimulationResult] = []
    for sample_index in range(max(8, int(samples))):
        sample_seed = int.from_bytes(hashlib.blake2b(
            f"{batch_seed}|route-cost|{sample_index}".encode(), digest_size=8,
        ).digest(), "big") & 0x7FFFFFFFFFFFFFFF
        outcome = simulate_route_pool(
            world=world, cache_payload=cache_payload, initial_pool_ids=initial_pool_ids,
            graph_mode=graph_mode, count=count, diffusion_start=diffusion_start, pool_max=pool_max,
            batch_seed=sample_seed, world_id=world_id, city_id=city_id, next_task_index=next_task_index,
        )
        outcomes.append(outcome)
        long_term.append(simulate_route_pool(
            world=world, cache_payload=cache_payload, initial_pool_ids=outcome.pool_ids,
            graph_mode=graph_mode, count=0, diffusion_start=diffusion_start, pool_max=pool_max,
            batch_seed=sample_seed ^ 0x5A17, world_id=world_id, city_id=city_id,
            next_task_index=next_task_index + count, continue_to_limit=True,
            initial_planned_keys=outcome.requested_keys,
        ))

    maximum_attempts = max(count * 5, count + 10)
    maximum_runs = [simulate_route_pool(
        world=world, cache_payload=cache_payload, initial_pool_ids=initial_pool_ids,
        graph_mode=graph_mode, count=count, diffusion_start=diffusion_start, pool_max=pool_max,
        batch_seed=(batch_seed + index * 1_000_003), world_id=world_id, city_id=city_id,
        next_task_index=next_task_index, attempt_count=maximum_attempts,
    ) for index in range(6)]

    minimum = min(outcomes, key=lambda item: item.requests)
    maximum = max([*maximum_runs, *outcomes], key=lambda item: item.requests)
    expected_requests = round(mean(item.requests for item in outcomes))
    expected_route_reuse_hits = round(mean(item.route_reuse_hits for item in outcomes))
    expected_coordinate_reuse_hits = round(mean(item.coordinate_reuse_hits for item in outcomes))
    expected_new_pois = round(mean(item.new_pois for item in outcomes), 1)
    expected_final_pool = round(mean(item.final_pool_size for item in outcomes), 1)
    remaining_requests = round(mean(item.requests for item in long_term))
    current_to_limit_requests = expected_requests + remaining_requests
    if len(initial_pool_ids) >= pool_max:
        remaining_requests = 0
        current_to_limit_requests = 0
    reachable_pool = max([deterministic.final_pool_size, *(item.final_pool_size for item in long_term)])

    def metric(requests: int, new_pois: float | int, final_pool: float | int) -> dict[str, Any]:
        return {
            "requests": int(requests), "new_pois": new_pois, "final_pool_size": final_pool,
            "cost_usd": round(int(requests) * UNIT_PRICE_USD, 2),
        }

    return {
        "unit_price_usd": UNIT_PRICE_USD,
        "minimum": metric(minimum.requests, minimum.new_pois, minimum.final_pool_size),
        "expected": {
            **metric(expected_requests, expected_new_pois, expected_final_pool),
            "route_reuse_hits": expected_route_reuse_hits,
            "coordinate_reuse_hits": expected_coordinate_reuse_hits,
        },
        "maximum": metric(maximum.requests, maximum.new_pois, maximum.final_pool_size),
        "deterministic": {
            **metric(deterministic.requests, deterministic.new_pois, deterministic.final_pool_size),
            "route_reuse_hits": deterministic.route_reuse_hits,
            "coordinate_reuse_hits": deterministic.coordinate_reuse_hits,
            "stages": deterministic.stages,
        },
        "to_limit_expected": metric(
            current_to_limit_requests, max(0, reachable_pool - len(initial_pool_ids)), reachable_pool,
        ),
        "after_batch_remaining": metric(
            remaining_requests, max(0, reachable_pool - expected_final_pool), reachable_pool,
        ),
        "reachable_pool_size": min(pool_max, reachable_pool),
        "sample_count": len(outcomes),
    }
