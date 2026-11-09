"""Bounded pre-generation route coverage for Region-Aware construction.

This module prepares a version-bound derived route asset.  It intentionally
does not run from the Region task generator: formal generation must remain
read-only and reject cache gaps before any prompt-model call.
"""

from __future__ import annotations

from typing import Any

from task_module.task_types.route_planning.sampler import RouteSampler


ROUTE_MODES = ("DRIVE", "WALK", "TRANSIT")


def collect_candidate_route_pairs(
    sampler: Any,
    kb: Any,
    *,
    candidate_attempts: int,
    max_pairs: int,
) -> dict[str, Any]:
    """Collect a deterministic, bounded set of forward actor-to-POI pairs.

    The sampler is the same Region sampler used by construction.  This makes
    the coverage set task-relevant without allowing the actual generator to
    make external requests while it samples candidates.
    """
    if candidate_attempts < 1 or max_pairs < 1:
        raise ValueError("candidate_attempts and max_pairs must be positive")

    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    skipped: dict[str, int] = {"no_constraints": 0, "no_character": 0, "not_enough_pois": 0}

    for attempt in range(1, candidate_attempts + 1):
        city_name, task_kb = sampler.sample_city()
        constraints, keywords, _pool_small = sampler.sample_constraints(
            task_kb, n_keywords=2, n_per_keyword=2
        )
        if not constraints:
            skipped["no_constraints"] += 1
            continue
        task_kb.increment_used([str(item.get("id") or "") for item in constraints])

        character = sampler.sample_character()
        if not character or not character.get("character_id"):
            skipped["no_character"] += 1
            continue
        main_pois = sampler.sample_pois_for_constraints(constraints, count=5)
        required_types = {
            str(place_type)
            for constraint in constraints
            for place_type in (constraint.get("hidden_rule", {}).get("required_place_types", []) or [])
        }
        minimum_pois = 1 if required_types else 3
        if len(main_pois) < minimum_pois:
            skipped["not_enough_pois"] += 1
            continue
        distractors = sampler.sample_distractors(main_pois, constraints, count=2)
        actor = _actor(character)
        destinations = [*main_pois, *distractors]
        candidate_pairs: list[str] = []
        for destination in destinations:
            destination_id = str(destination.get("place_id") or "")
            if not destination_id:
                continue
            key = (actor["place_id"], destination_id)
            candidate_pairs.append(_pair_label(*key))
            if key not in pairs and len(pairs) < max_pairs:
                pairs[key] = {
                    "origin": actor,
                    "destination": _place(destination),
                    "candidate_attempts": [attempt],
                }
            elif key in pairs:
                pairs[key]["candidate_attempts"].append(attempt)
        candidates.append({
            "attempt": attempt,
            "city_id": city_name,
            "character_id": character["character_id"],
            "constraint_ids": [str(item.get("id") or "") for item in constraints],
            "candidate_pairs": candidate_pairs,
        })

    return {
        "candidate_attempts": candidate_attempts,
        "max_pairs": max_pairs,
        "selected_pairs": list(pairs.values()),
        "candidate_scenarios": candidates,
        "skipped_candidates": skipped,
    }


def missing_route_modes(
    route_cache: Any, selected_pairs: list[dict[str, Any]],
    modes: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Return only uncached modes; cached unavailable results are coverage."""
    missing: list[dict[str, Any]] = []
    for pair in selected_pairs:
        origin = pair["origin"]
        destination = pair["destination"]
        pair_modes = modes or RouteSampler._modes_for_distance(
            RouteSampler.distance(origin, destination)
        )
        for mode in pair_modes:
            if route_cache.get(origin, destination, mode) is None:
                missing.append({"origin": origin, "destination": destination, "mode": mode})
    return missing


def coverage_status(route_cache: Any, candidate_scenarios: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize which sampled candidates now have one factual forward edge."""
    eligible: list[int] = []
    incomplete: list[int] = []
    for candidate in candidate_scenarios:
        actor_id = str(candidate.get("character_id") or "")
        all_reachable = True
        for label in candidate.get("candidate_pairs") or []:
            _origin, destination = label.split("->", 1)
            origin = {"place_id": f"character:{actor_id}"}
            dest = {"place_id": destination}
            if not any(_has_available_route(route_cache.get(origin, dest, mode)) for mode in ROUTE_MODES):
                all_reachable = False
                break
        if all_reachable and candidate.get("candidate_pairs"):
            eligible.append(int(candidate["attempt"]))
        else:
            incomplete.append(int(candidate["attempt"]))
    return {
        "candidate_scenarios": len(candidate_scenarios),
        "fully_covered_attempts": eligible,
        "incomplete_attempts": incomplete,
        "fully_covered_count": len(eligible),
    }


def _has_available_route(entry: dict[str, Any] | None) -> bool:
    route = (entry or {}).get("route") or {}
    return bool(route.get("available") is not False and route.get("distance_m", 0) > 0)


def _actor(character: dict[str, Any]) -> dict[str, Any]:
    return {
        "place_id": f"character:{character['character_id']}",
        "name": str(character.get("name") or "character"),
        "location": {
            "lat": character.get("home_lat"),
            "lng": character.get("home_lng"),
        },
    }


def _place(place: dict[str, Any]) -> dict[str, Any]:
    return {
        "place_id": str(place.get("place_id") or ""),
        "name": str(place.get("name") or "place"),
        "location": dict(place.get("location") or {}),
    }


def _pair_label(origin_id: str, destination_id: str) -> str:
    return f"{origin_id}->{destination_id}"
