"""Read-only Google Routes cost preview for incremental Itinerary construction."""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from statistics import mean
from typing import Any

from task_module._shared.batch_state import attempt_seed, task_seed
from task_module.task_types.itinerary.generator import _resolve_itinerary_routing_place_ids
from task_module.task_types.itinerary.sampler import ItinerarySampler
from task_module.task_types.route_planning.cost_planner import ReadOnlyRouteIndex, UNIT_PRICE_USD


DAILY_EDGES = (9 * 60, 12 * 60, 14 * 60, 17 * 60, 19 * 60, 22 * 60)


@dataclass(frozen=True)
class PlanningSegment:
    day: int
    weekday_index: int
    start_minute: int
    end_minute: int
    origin: dict[str, Any]
    target: dict[str, Any]

    @property
    def maximum_activity_blocks(self) -> int:
        return len(_legal_end_minutes(self.start_minute, self.end_minute))


@dataclass
class SampleResult:
    requests: int
    route_reuse_hits: int
    coordinate_reuse_hits: int
    activity_blocks: int
    days: list[int]


def itinerary_routing_place_ids(
    world: dict[str, Any], route_pool_payload: dict[str, Any], cache_payload: dict[str, Any],
) -> set[str]:
    """Expose the generator's active-version Routing coverage compatibility rule."""
    return _resolve_itinerary_routing_place_ids(
        world, route_pool_payload, list(cache_payload.get("cache") or []),
    )


def _legal_end_minutes(start: int, latest_end: int) -> list[int]:
    if latest_end <= start:
        return []
    return sorted({latest_end, *(edge for edge in DAILY_EDGES if start < edge <= latest_end)})


def _route_place(place: dict[str, Any]) -> dict[str, Any]:
    return {
        "place_id": str(place.get("place_id") or ""),
        "name": str(place.get("name") or ""),
        "location": dict(place.get("location") or {}),
    }


def _sample_fixed_skeleton(
    *, world: dict[str, Any], characters: dict[str, Any], cache: ReadOnlyRouteIndex,
    routing_place_ids: set[str], batch_seed: int, world_id: str, city_id: str,
    task_index: int,
) -> tuple[ItinerarySampler, dict[str, Any], list[PlanningSegment], int] | None:
    seed_value = task_seed(batch_seed, world_id, city_id, "itinerary", task_index)
    rng = random.Random(attempt_seed(seed_value, 1))
    sampler = ItinerarySampler(
        world, characters, cache, rng=rng, routing_place_ids=routing_place_ids,
    )
    character = sampler.sample_character()
    if not character:
        return None
    num_days = sampler.determine_duration()
    hotel = sampler.sample_hotel()
    if not hotel:
        return None
    transit_hubs = sampler._pick_transit_hubs(hotel.get("district_id", ""), count=2)  # noqa: SLF001
    if not transit_hubs:
        return None
    scenario = sampler.sample_scenario_profile(rng)
    sampler.build_scenario_sequence(num_days, rng)
    start_weekday_index = sampler.sample_start_weekday()
    arrival = transit_hubs[0]
    departure = transit_hubs[-1]
    segments: list[PlanningSegment] = []
    for day in range(1, num_days + 1):
        weekday_index = (start_weekday_index + day - 1) % 7
        if day == 1:
            segments.append(PlanningSegment(day, weekday_index, 11 * 60, 14 * 60, arrival, hotel))
            if day < num_days:
                segments.append(PlanningSegment(day, weekday_index, 14 * 60 + 30, 22 * 60, hotel, hotel))
        elif day == num_days:
            segments.append(PlanningSegment(day, weekday_index, 7 * 60 + 30, 10 * 60 + 30, hotel, hotel))
            segments.append(PlanningSegment(day, weekday_index, 11 * 60, 14 * 60, hotel, departure))
        else:
            segments.append(PlanningSegment(day, weekday_index, 7 * 60 + 30, 22 * 60, hotel, hotel))
    return sampler, character, segments, num_days


def _count_edge(
    cache: ReadOnlyRouteIndex, sampler: ItinerarySampler,
    origin: dict[str, Any], destination: dict[str, Any], *, mark: bool,
) -> tuple[int, int, int]:
    origin = _route_place(origin)
    destination = _route_place(destination)
    if not origin["place_id"] or not destination["place_id"] or origin["place_id"] == destination["place_id"]:
        return (0, 0, 0)
    distance_m = sampler.route_sampler.distance(origin, destination)
    requests = route_hits = coordinate_hits = 0
    for mode in sampler.route_sampler._modes_for_distance(distance_m):
        kind = cache.match_kind(origin, destination, mode)
        if kind == "route":
            route_hits += 1
        elif kind == "coordinate":
            coordinate_hits += 1
        else:
            requests += 1
            if mark:
                cache.mark_planned(
                    (origin["place_id"], destination["place_id"], mode, "static_v1"),
                    origin, destination, mode,
                )
    return requests, route_hits, coordinate_hits


def _add(left: tuple[int, int, int], right: tuple[int, int, int]) -> tuple[int, int, int]:
    return tuple(a + b for a, b in zip(left, right))  # type: ignore[return-value]


def _personal_end(rng: random.Random, cursor: int, end: int) -> int:
    return rng.choice(_legal_end_minutes(cursor, end))


def _sample_segment_pois(
    sampler: ItinerarySampler, segment: PlanningSegment, rng: random.Random,
    available_types: list[str], selected_place_ids: set[str],
) -> list[dict[str, Any]]:
    cursor = segment.start_minute
    selected: list[dict[str, Any]] = []
    while cursor < segment.end_minute:
        direction = rng.choice(["personal_activity", *available_types])
        if direction == "personal_activity":
            cursor = _personal_end(rng, cursor, segment.end_minute)
            continue
        poi = sampler.sample_poi_for_service_type(
            direction,
            excluded_place_ids=selected_place_ids,
            weekday_index=segment.weekday_index,
        )
        if not poi:
            cursor = _personal_end(rng, cursor, segment.end_minute)
            continue
        intervals = [
            (max(cursor, int(opened)), min(segment.end_minute, int(closed)))
            for opened, closed in poi.get("opening_intervals") or []
            if max(cursor, int(opened)) < min(segment.end_minute, int(closed))
        ]
        if not intervals:
            cursor = _personal_end(rng, cursor, segment.end_minute)
            continue
        activity_start, activity_latest = intervals[0]
        legal_ends = _legal_end_minutes(activity_start, activity_latest)
        if not legal_ends:
            cursor = _personal_end(rng, cursor, segment.end_minute)
            continue
        selected.append(poi)
        selected_place_ids.add(str(poi.get("place_id") or ""))
        cursor = rng.choice(legal_ends)
    return selected


def simulate_itinerary_cost(
    *, world: dict[str, Any], characters: dict[str, Any], cache_payload: dict[str, Any],
    routing_place_ids: set[str], count: int, batch_seed: int, world_id: str,
    city_id: str, next_task_index: int = 1, sample_index: int = 0,
) -> SampleResult:
    cache = ReadOnlyRouteIndex(cache_payload, world)
    requests = route_hits = coordinate_hits = activity_blocks = 0
    days: list[int] = []
    for offset in range(max(0, int(count))):
        task_index = int(next_task_index) + offset
        fixed = _sample_fixed_skeleton(
            world=world, characters=characters, cache=cache,
            routing_place_ids=routing_place_ids, batch_seed=batch_seed,
            world_id=world_id, city_id=city_id, task_index=task_index,
        )
        if fixed is None:
            raise ValueError("Itinerary cost preview cannot build the fixed seeded task structure")
        sampler, _character, segments, num_days = fixed
        days.append(num_days)
        available_types = sampler.available_service_types()
        if not available_types:
            raise ValueError("Itinerary requires Routing POIs with usable activity service types")
        decision_seed = int.from_bytes(hashlib.blake2b(
            f"{batch_seed}|itinerary-cost|{sample_index}|{task_index}".encode(), digest_size=8,
        ).digest(), "big")
        decision_rng = random.Random(decision_seed)
        selected_place_ids: set[str] = set()
        for segment in segments:
            # The real builder checks the next fixed boundary before asking an
            # Agent for another activity direction.
            totals = _add((requests, route_hits, coordinate_hits), _count_edge(
                cache, sampler, segment.origin, segment.target, mark=True,
            ))
            requests, route_hits, coordinate_hits = totals
            pois = _sample_segment_pois(
                sampler, segment, decision_rng, available_types, selected_place_ids,
            )
            activity_blocks += len(pois)
            # The formal DAG validates every sampled POI against the next fixed
            # boundary before it materializes the incoming activity edge.
            for poi in pois:
                totals = _add((requests, route_hits, coordinate_hits), _count_edge(
                    cache, sampler, poi, segment.target, mark=True,
                ))
                requests, route_hits, coordinate_hits = totals
            chain = [segment.origin, *pois]
            for origin, destination in zip(chain, chain[1:]):
                totals = _add((requests, route_hits, coordinate_hits), _count_edge(
                    cache, sampler, origin, destination, mark=True,
                ))
                requests, route_hits, coordinate_hits = totals
    return SampleResult(requests, route_hits, coordinate_hits, activity_blocks, days)


def _mandatory_minimum(
    *, world: dict[str, Any], characters: dict[str, Any], cache_payload: dict[str, Any],
    routing_place_ids: set[str], count: int, batch_seed: int, world_id: str,
    city_id: str, next_task_index: int,
) -> SampleResult:
    cache = ReadOnlyRouteIndex(cache_payload, world)
    requests = route_hits = coordinate_hits = 0
    days: list[int] = []
    for offset in range(max(0, int(count))):
        fixed = _sample_fixed_skeleton(
            world=world, characters=characters, cache=cache,
            routing_place_ids=routing_place_ids, batch_seed=batch_seed,
            world_id=world_id, city_id=city_id,
            task_index=int(next_task_index) + offset,
        )
        if fixed is None:
            raise ValueError("Itinerary cost preview cannot build the fixed seeded task structure")
        sampler, _character, segments, num_days = fixed
        days.append(num_days)
        for segment in segments:
            totals = _add((requests, route_hits, coordinate_hits), _count_edge(
                cache, sampler, segment.origin, segment.target, mark=True,
            ))
            requests, route_hits, coordinate_hits = totals
    return SampleResult(requests, route_hits, coordinate_hits, 0, days)


def _maximum_missing_for_edge(
    cache: ReadOnlyRouteIndex, sampler: ItinerarySampler,
    origins: list[dict[str, Any]], destinations: list[dict[str, Any]],
) -> int:
    maximum = 0
    for origin in origins:
        for destination in destinations:
            if str(origin.get("place_id") or "") == str(destination.get("place_id") or ""):
                continue
            maximum = max(maximum, _count_edge(cache, sampler, origin, destination, mark=False)[0])
    return maximum


def _structural_maximum(
    *, world: dict[str, Any], characters: dict[str, Any], cache_payload: dict[str, Any],
    routing_place_ids: set[str], count: int, batch_seed: int, world_id: str,
    city_id: str, next_task_index: int,
) -> SampleResult:
    cache = ReadOnlyRouteIndex(cache_payload, world)
    requests = activity_blocks = 0
    days: list[int] = []
    for offset in range(max(0, int(count))):
        fixed = _sample_fixed_skeleton(
            world=world, characters=characters, cache=cache,
            routing_place_ids=routing_place_ids, batch_seed=batch_seed,
            world_id=world_id, city_id=city_id,
            task_index=int(next_task_index) + offset,
        )
        if fixed is None:
            raise ValueError("Itinerary cost preview cannot build the fixed seeded task structure")
        sampler, _character, segments, num_days = fixed
        days.append(num_days)
        candidates_by_id = {
            str(item.get("place_id") or ""): item
            for item in sampler.routing_activity_candidates()
            if item.get("place_id")
        }
        candidates = list(candidates_by_id.values())
        remaining = len(candidates)
        internal_max = _maximum_missing_for_edge(cache, sampler, candidates, candidates)
        for segment in segments:
            direct = _count_edge(cache, sampler, segment.origin, segment.target, mark=False)[0]
            requests += direct
            blocks = min(segment.maximum_activity_blocks, remaining)
            remaining -= blocks
            activity_blocks += blocks
            if not blocks:
                continue
            requests += _maximum_missing_for_edge(cache, sampler, [segment.origin], candidates)
            requests += blocks * _maximum_missing_for_edge(
                cache, sampler, candidates, [segment.target],
            )
            requests += max(0, blocks - 1) * internal_max
    return SampleResult(requests, 0, 0, activity_blocks, days)


def _metric(result: SampleResult) -> dict[str, Any]:
    return {
        "requests": int(result.requests),
        "route_reuse_hits": int(result.route_reuse_hits),
        "coordinate_reuse_hits": int(result.coordinate_reuse_hits),
        "activity_blocks": int(result.activity_blocks),
        "days": list(result.days),
        "cost_usd": round(int(result.requests) * UNIT_PRICE_USD, 2),
    }


def build_itinerary_cost_preview(
    *, world: dict[str, Any], characters: dict[str, Any], cache_payload: dict[str, Any],
    route_pool_payload: dict[str, Any], count: int, batch_seed: int, world_id: str,
    city_id: str, next_task_index: int = 1, samples: int = 24,
) -> dict[str, Any]:
    routing_place_ids = itinerary_routing_place_ids(world, route_pool_payload, cache_payload)
    if not routing_place_ids:
        raise ValueError("当前城市没有可供 Itinerary 使用的 Routing POI池。")
    minimum = _mandatory_minimum(
        world=world, characters=characters, cache_payload=cache_payload,
        routing_place_ids=routing_place_ids, count=count, batch_seed=batch_seed,
        world_id=world_id, city_id=city_id, next_task_index=next_task_index,
    )
    maximum = _structural_maximum(
        world=world, characters=characters, cache_payload=cache_payload,
        routing_place_ids=routing_place_ids, count=count, batch_seed=batch_seed,
        world_id=world_id, city_id=city_id, next_task_index=next_task_index,
    )
    outcomes = [
        simulate_itinerary_cost(
            world=world, characters=characters, cache_payload=cache_payload,
            routing_place_ids=routing_place_ids, count=count, batch_seed=batch_seed,
            world_id=world_id, city_id=city_id, next_task_index=next_task_index,
            sample_index=sample_index,
        )
        for sample_index in range(max(8, int(samples)))
    ]
    expected_requests = round(mean(item.requests for item in outcomes))
    expected = {
        "requests": expected_requests,
        "route_reuse_hits": round(mean(item.route_reuse_hits for item in outcomes)),
        "coordinate_reuse_hits": round(mean(item.coordinate_reuse_hits for item in outcomes)),
        "activity_blocks": round(mean(item.activity_blocks for item in outcomes), 1),
        "days": list(minimum.days),
        "cost_usd": round(expected_requests * UNIT_PRICE_USD, 2),
    }
    return {
        "unit_price_usd": UNIT_PRICE_USD,
        "minimum": _metric(minimum),
        "expected": expected,
        "maximum": _metric(maximum),
        "sample_count": len(outcomes),
        "estimate_kind": "bounded_agent_proxy_simulation",
    }
