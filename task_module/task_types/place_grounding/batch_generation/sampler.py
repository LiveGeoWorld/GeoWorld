"""Sampling POI targets for grounding batch generation."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from task_module.task_types.place_grounding.batch_generation.ambiguity import build_ambiguity_landscape


@dataclass
class SampledTarget:
    city_id: str
    granularity: str
    place: dict[str, Any]
    character: dict[str, Any] | None
    ambiguity_landscape: dict[str, Any]


def weighted_choice(weights: dict[str, float], rng: random.Random) -> str:
    items = [(key, max(0.0, float(value))) for key, value in weights.items()]
    total = sum(value for _key, value in items)
    if total <= 0:
        return items[0][0]
    pick = rng.random() * total
    upto = 0.0
    for key, value in items:
        upto += value
        if pick <= upto:
            return key
    return items[-1][0]


def sample_targets(
    *,
    worlds: dict[str, dict[str, Any]],
    characters_by_city: dict[str, dict[str, Any]],
    count: int,
    city_weights: dict[str, float],
    granularity_weights: dict[str, float],
    category_weights: dict[str, float],
    rng: random.Random,
    quality_min_ambiguity: int = 2,
    quality_max_ambiguity: int = 8,
    max_place_attempts: int = 40,
    excluded_place_ids: set[str] | None = None,
) -> list[SampledTarget]:
    targets: list[SampledTarget] = []
    for _ in range(count):
        city_id = _sample_city(worlds, city_weights, rng)
        target = _sample_poi(
            city_id=city_id,
            world=worlds[city_id],
            granularity=weighted_choice(granularity_weights, rng),
            category_weights=category_weights,
            rng=rng,
            quality_min_ambiguity=quality_min_ambiguity,
            quality_max_ambiguity=quality_max_ambiguity,
            max_place_attempts=max_place_attempts,
            excluded_place_ids=excluded_place_ids or set(),
        )
        if target is not None:
            targets.append(target)
    return targets


def _sample_city(worlds: dict[str, dict[str, Any]], city_weights: dict[str, float], rng: random.Random) -> str:
    available = {city_id: city_weights.get(city_id, 0.0) for city_id in worlds}
    if not any(weight > 0 for weight in available.values()):
        available = {city_id: 1.0 for city_id in worlds}
    return weighted_choice(available, rng)


def _sample_poi(
    *,
    city_id: str,
    world: dict[str, Any],
    granularity: str,
    category_weights: dict[str, float],
    rng: random.Random,
    quality_min_ambiguity: int,
    quality_max_ambiguity: int,
    max_place_attempts: int,
    excluded_place_ids: set[str],
) -> SampledTarget | None:
    places = _candidate_places(world)
    if not places:
        return None
    if category_weights:
        category = weighted_choice(category_weights, rng)
        typed = [place for place in places if _matches_category(place, category)]
        if typed:
            places = typed

    for _ in range(max_place_attempts):
        place = rng.choice(places)
        if place.get("place_id") in excluded_place_ids:
            continue
        if not place.get("primary_type") or not place.get("district_id"):
            continue
        landscape = build_ambiguity_landscape(place, world)
        score = int(landscape.get("ambiguity_score", 0))
        if score < quality_min_ambiguity or score > quality_max_ambiguity:
            continue
        resolved_granularity = "exact_poi" if granularity == "district_area" else granularity
        return SampledTarget(
            city_id=city_id,
            granularity=resolved_granularity,
            place=place,
            character=None,
            ambiguity_landscape=landscape,
        )
    return None


def _candidate_places(world: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        place
        for place in world.get("places", [])
        if place.get("place_id") and place.get("name") and place.get("location")
    ]


def _matches_category(place: dict[str, Any], category: str) -> bool:
    raw = str(category or "").casefold()
    types = [place.get("primary_type", ""), *place.get("map_projection", {}).get("types", [])]
    return any(raw and raw in str(item).casefold() for item in types)
