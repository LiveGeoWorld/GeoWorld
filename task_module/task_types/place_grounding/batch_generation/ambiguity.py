"""Objective map ambiguity metrics for place_grounding sampling."""

from __future__ import annotations

from typing import Any

from app.runtime.geo import distance_m

CONFUSABLE_RADIUS_M = 500


def compute_ambiguity_metrics(place: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    """Count nearby POI density around a gold place."""
    loc = place.get("location", {})
    if not loc:
        return _empty_metrics()

    primary_type = place.get("primary_type", "")
    place_id = place.get("place_id", "")
    candidates = [
        item
        for item in world.get("places", [])
        if item.get("place_id") and item.get("place_id") != place_id and item.get("location")
    ]

    def count_in_radius(radius_m: int, *, same_type_only: bool) -> int:
        total = 0
        for other in candidates:
            if same_type_only and other.get("primary_type") != primary_type:
                continue
            if distance_m(loc, other.get("location", {})) <= radius_m:
                total += 1
        return total

    same_type = [item for item in candidates if item.get("primary_type") == primary_type]
    nearest_same_type_m = _nearest_distance_m(loc, same_type)
    nearest = same_type[0] if same_type and nearest_same_type_m is not None else None
    if nearest is not None and nearest_same_type_m is not None:
        for item in same_type:
            dist = distance_m(loc, item.get("location", {}))
            if dist == nearest_same_type_m:
                nearest = item
                break

    return {
        "same_type_within_300m": count_in_radius(300, same_type_only=True),
        "same_type_within_500m": count_in_radius(CONFUSABLE_RADIUS_M, same_type_only=True),
        "same_type_within_1000m": count_in_radius(1000, same_type_only=True),
        "all_places_within_300m": count_in_radius(300, same_type_only=False),
        "all_places_within_500m": count_in_radius(CONFUSABLE_RADIUS_M, same_type_only=False),
        "nearest_same_type_m": nearest_same_type_m,
        "nearest_same_type_name": nearest.get("name", "") if nearest else "",
    }


def ambiguity_score_from_metrics(metrics: dict[str, Any]) -> int:
    """Map density metrics to a 0-10 ambiguity score."""
    score = 0.0
    score += min(3.0, float(metrics.get("same_type_within_300m", 0)) * 1.0)
    score += min(2.0, float(metrics.get("same_type_within_500m", 0)) * 0.35)
    score += min(1.0, float(metrics.get("same_type_within_1000m", 0)) * 0.08)
    score += min(1.0, float(metrics.get("all_places_within_500m", 0)) * 0.12)

    nearest = metrics.get("nearest_same_type_m")
    if isinstance(nearest, (int, float)):
        if nearest <= 50:
            score += 3.0
        elif nearest <= 120:
            score += 2.0
        elif nearest <= 250:
            score += 1.0

    return int(min(10, max(0, round(score))))


def build_ambiguity_landscape(place: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    """Record the full confusable POI panorama for a gold place."""
    metrics = compute_ambiguity_metrics(place, world)
    score = ambiguity_score_from_metrics(metrics)
    loc = place.get("location", {})
    primary_type = place.get("primary_type", "")
    confusable: list[dict[str, Any]] = []

    for other in world.get("places", []):
        if other.get("place_id") == place.get("place_id") or not other.get("location"):
            continue
        if other.get("primary_type") != primary_type:
            continue
        dist = round(distance_m(loc, other.get("location", {})))
        if dist > CONFUSABLE_RADIUS_M:
            continue
        confusable.append({
            "place_id": other.get("place_id", ""),
            "name": other.get("name", ""),
            "distance_m": dist,
            "primary_type": other.get("primary_type", ""),
            "district_id": other.get("district_id", ""),
            "similarity_reason": "same_type",
        })

    confusable.sort(key=lambda item: item.get("distance_m", 10**9))
    return {
        "ambiguity_score": score,
        **metrics,
        "confusable_candidates": confusable,
    }


def display_difficulty_from_ambiguity(ambiguity_score: int) -> str:
    """Map ambiguity score to easy/medium/hard for UI compatibility only."""
    if ambiguity_score <= 3:
        return "easy"
    if ambiguity_score <= 6:
        return "medium"
    return "hard"


def _nearest_distance_m(loc: dict[str, Any], places: list[dict[str, Any]]) -> float | None:
    distances = [distance_m(loc, item.get("location", {})) for item in places if item.get("location")]
    return min(distances) if distances else None


def _empty_metrics() -> dict[str, Any]:
    return {
        "same_type_within_300m": 0,
        "same_type_within_500m": 0,
        "same_type_within_1000m": 0,
        "all_places_within_300m": 0,
        "all_places_within_500m": 0,
        "nearest_same_type_m": None,
        "nearest_same_type_name": "",
    }
