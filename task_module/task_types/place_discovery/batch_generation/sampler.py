"""Sampling targets and search landscapes for place_discovery batches."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from app.runtime.local_service import LocalServiceAppRuntime
from app.runtime.geo import distance_m

from task_module.task_types.place_discovery.batch_generation.search_ambiguity import (
    BUDGET_PRICE_COMPATIBILITY,
    CROWD_AVOID_KEYWORDS,
    INDOOR_PREFER_KEYWORDS,
    NOISY_AVOID_KEYWORDS,
    QUEUE_AVOID_KEYWORDS,
    QUIET_PREFER_KEYWORDS,
    build_search_landscape,
    extract_service_attrs,
)


DISTRICT_DISPLAY_NAMES = {
    "district_shanghai_smoke_people_s_park": "人民广场 / 人民公园",
    "district_shanghai_smoke_west_nanjing_road": "南京西路",
    "district_shanghai_smoke_the_bund": "外滩",
    "district_shanghai_smoke_yu_garden": "豫园",
    "district_shanghai_smoke_xintiandi": "新天地",
}


@dataclass
class SampledDiscoveryTarget:
    city_id: str
    service_type: str
    gold_service: dict[str, Any]
    gold_place: dict[str, Any]
    start: dict[str, Any]
    candidate_services: list[dict[str, Any]]
    search_landscape: dict[str, Any]
    character: dict[str, Any] | None = None
    character_constraints: dict[str, Any] | None = None

    @property
    def distractors(self) -> list[dict[str, Any]]:
        """UI-compatible distractor list derived from confusable candidates."""
        return [
            {
                "place_id": item.get("place_id", ""),
                "service_id": item.get("service_id", ""),
                "name": item.get("name", ""),
                "coordinate": {},
                "reason_to_exclude": "；".join(item.get("exclusion_reasons", [])),
            }
            for item in self.search_landscape.get("confusable_candidates", [])[:4]
        ]


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
    characters_by_city: dict[str, dict[str, Any]] | None = None,
    count: int,
    city_weights: dict[str, float],
    service_type_weights: dict[str, float],
    rng: random.Random,
    use_characters: bool = True,
    quality_min_search_ambiguity: int = 2,
    quality_max_search_ambiguity: int = 8,
    max_service_attempts: int = 40,
    excluded_service_ids: set[str] | None = None,
) -> list[SampledDiscoveryTarget]:
    targets: list[SampledDiscoveryTarget] = []
    for _ in range(count):
        city_id = _sample_city(worlds, city_weights, rng)
        target = _sample_service_target(
            city_id=city_id,
            world=worlds[city_id],
            service_type=weighted_choice(service_type_weights, rng),
            rng=rng,
            characters_by_city=characters_by_city,
            use_characters=use_characters,
            quality_min_search_ambiguity=quality_min_search_ambiguity,
            quality_max_search_ambiguity=quality_max_search_ambiguity,
            max_service_attempts=max_service_attempts,
            excluded_service_ids=excluded_service_ids or set(),
        )
        if target is not None:
            targets.append(target)
    return targets


def _sample_city(worlds: dict[str, dict[str, Any]], city_weights: dict[str, float], rng: random.Random) -> str:
    available = {city_id: city_weights.get(city_id, 0.0) for city_id in worlds}
    if not any(weight > 0 for weight in available.values()):
        available = {city_id: 1.0 for city_id in worlds}
    return weighted_choice(available, rng)


def _sample_service_target(
    *,
    city_id: str,
    world: dict[str, Any],
    service_type: str,
    rng: random.Random,
    characters_by_city: dict[str, dict[str, Any]] | None,
    use_characters: bool,
    quality_min_search_ambiguity: int,
    quality_max_search_ambiguity: int,
    max_service_attempts: int,
    excluded_service_ids: set[str],
) -> SampledDiscoveryTarget | None:
    runtime = LocalServiceAppRuntime(
        services=world.get("services", []) or _factual_place_aliases(world),
        places=world.get("places", []),
        cities=world.get("cities", []),
    )
    candidates = _semantic_service_candidates(runtime, service_type, city_id)
    if not candidates:
        return None

    characters = []
    if use_characters and characters_by_city:
        characters = characters_by_city.get(city_id, {}).get("characters", [])

    for _ in range(max_service_attempts):
        service, place = rng.choice(candidates)
        if service.get("service_id") in excluded_service_ids:
            continue

        pure_landscape = build_search_landscape(
            gold_service=service,
            gold_place=place,
            candidates=candidates,
            runtime=runtime,
            character=None,
            character_constraints=None,
        )
        confusable = pure_landscape.get("confusable_candidates", [])
        # Top-3 discovery needs two genuine alternatives; do not silently
        # downgrade an open-ended ranking task to a single-answer task.
        if len(confusable) < 2:
            continue

        character = None
        constraints: dict[str, Any] | None = None
        if characters:
            character, constraints = _select_discriminative_character(
                gold_service=service,
                gold_place=place,
                confusable_candidates=confusable[:5],
                characters=characters,
                service_type=service_type,
                runtime=runtime,
                rng=rng,
            )
            if not character:
                continue

        landscape = build_search_landscape(
            gold_service=service,
            gold_place=place,
            candidates=candidates,
            runtime=runtime,
            character=character,
            character_constraints=constraints,
        )
        score = int(landscape.get("search_ambiguity_score", 0))
        if not (quality_min_search_ambiguity <= score <= quality_max_search_ambiguity):
            continue
        if len(landscape.get("confusable_candidates", [])) < 2:
            continue

        start = _character_start_context(world, character, city_id)
        if not start:
            continue

        candidate_services = [service]
        for item in landscape.get("confusable_candidates", [])[:2]:
            sid = item.get("service_id", "")
            matched = next((s for s, _p in candidates if s.get("service_id") == sid), None)
            if matched:
                candidate_services.append(matched)

        return SampledDiscoveryTarget(
            city_id=city_id,
            service_type=service_type,
            gold_service=service,
            gold_place=place,
            start=start,
            candidate_services=candidate_services,
            search_landscape=landscape,
            character=character,
            character_constraints=constraints,
        )
    return None


def _factual_place_aliases(world: dict[str, Any]) -> list[dict[str, Any]]:
    """Provide task-local handles for factual POIs when static services are absent."""
    aliases = []
    for place in world.get("places", []):
        place_id = str(place.get("place_id", ""))
        if not place_id:
            continue
        aliases.append({
            "service_id": f"poi_{place_id}", "place_id": place_id,
            "service_type": "local_visit", "name": place.get("name", ""),
            "state_model": {}, "factual_poi_alias": True,
        })
    return aliases


def _semantic_service_candidates(
    runtime: LocalServiceAppRuntime,
    service_type: str,
    city_id: str,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs = []
    for service in runtime.services:
        place = runtime._place_by_id(service.get("place_id", ""))  # noqa: SLF001
        if not place or place.get("city_id") != city_id:
            continue
        if runtime._service_type_matches(service, place, service_type):  # noqa: SLF001
            if _passes_batch_type_guard(service_type, place):
                pairs.append((service, place))
    return pairs


def _passes_batch_type_guard(service_type: str, place: dict[str, Any]) -> bool:
    place_type = str(place.get("primary_type", "") or "")
    projection_types = set(str(item) for item in place.get("map_projection", {}).get("types", []) or [])
    types = {place_type, *projection_types}
    if service_type == "attraction_visit":
        return bool(types & {"tourist_attraction", "park", "museum", "art_gallery", "zoo"})
    if service_type == "cafe_reservation":
        return bool(types & {"cafe", "coffee_shop"})
    if service_type == "restaurant_reservation":
        if types & {"cafe", "coffee_shop"}:
            return False
        return bool(types & {"restaurant", "chinese_restaurant", "fast_food_restaurant", "food", "meal_takeaway"})
    if service_type == "hotel_booking":
        return bool(types & {"hotel", "lodging"})
    return True


def _character_start_context(
    world: dict[str, Any],
    character: dict[str, Any] | None,
    city_id: str,
) -> dict[str, Any]:
    if not character:
        return {}
    mobility = character.get("mobility", {}) or {}
    current_place = dict(mobility.get("current_place", {}) or {})
    if not current_place.get("name"):
        current_place = _resolve_legacy_character_place(world, mobility)
    if not current_place.get("name") or not current_place.get("location"):
        return {}

    district_id = str(
        current_place.get("district_id") or mobility.get("home_district_id") or ""
    )
    district = _district_by_id(world, district_id)
    city = next((item for item in world.get("cities", []) if item.get("city_id") == city_id), {})
    city_name = str(city.get("name") or city_id)
    place_name = str(current_place.get("name", ""))
    return {
        "area_name": f"{place_name}（{city_name}）附近",
        "name": place_name,
        "address": current_place.get("address", ""),
        "city_id": city_id,
        "city_name": city_name,
        "place_id": current_place.get("place_id", ""),
        "source_place_id": current_place.get("source_place_id", ""),
        "source": current_place.get("source", ""),
        "district_id": district_id,
        "district_name": district.get("name", ""),
        "place_type": current_place.get("place_type", ""),
        "coordinate": current_place.get("location", {}),
    }


def _resolve_legacy_character_place(
    world: dict[str, Any], mobility: dict[str, Any]
) -> dict[str, Any]:
    try:
        lat = float(mobility.get("lat"))
        lng = float(mobility.get("lng"))
    except (TypeError, ValueError):
        return {}

    for place in world.get("places", []):
        if _same_coordinate(place.get("location", {}), lat, lng):
            return {
                "place_id": str(place.get("place_id", "")),
                "source_place_id": str(
                    place.get("source_place_id") or place.get("google_place_id") or ""
                ),
                "name": str(place.get("name", "")),
                "address": str(place.get("formatted_address") or place.get("address") or ""),
                "location": {"lat": lat, "lng": lng},
                "place_type": str(place.get("primary_type", "")),
                "district_id": str(place.get("district_id", "")),
                "source": "world_place",
            }

    home_district_id = str(mobility.get("home_district_id", ""))
    districts = sorted(
        world.get("districts", []),
        key=lambda item: item.get("district_id") != home_district_id,
    )
    for district in districts:
        for anchor in district.get("director_notes", {}).get("anchor_places", []):
            if not _same_coordinate(anchor.get("location", {}), lat, lng):
                continue
            return {
                "source_place_id": str(
                    anchor.get("source_place_id")
                    or anchor.get("google_place_id")
                    or anchor.get("osm_place_id")
                    or anchor.get("place_id")
                    or ""
                ),
                "name": str(anchor.get("name", "")),
                "address": str(anchor.get("formatted_address") or anchor.get("address") or ""),
                "location": {"lat": lat, "lng": lng},
                "place_type": str(anchor.get("primary_type") or anchor.get("type") or ""),
                "district_id": str(district.get("district_id", "")),
                "source": "district_anchor",
            }
    return {}


def _same_coordinate(location: dict[str, Any], lat: float, lng: float) -> bool:
    try:
        return abs(float(location.get("lat")) - lat) <= 1e-7 and abs(float(location.get("lng")) - lng) <= 1e-7
    except (TypeError, ValueError):
        return False


def _district_by_id(world: dict[str, Any], district_id: str) -> dict[str, Any]:
    for district in world.get("districts", []):
        if district.get("district_id") == district_id:
            return district
    return {}


def _service_attr(service: dict[str, Any], key: str) -> Any:
    if key in service and service[key] not in (None, ""):
        return service[key]
    return service.get("state_model", {}).get(key)


def _select_compatible_character(
    *,
    gold_service: dict[str, Any],
    gold_place: dict[str, Any],
    characters: list[dict[str, Any]],
    service_type: str,
    rng: random.Random,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Deprecated: use _select_discriminative_character when confusable candidates exist."""
    scored: list[tuple[int, dict[str, Any], list[str], list[str]]] = []
    for character in characters:
        score, satisfied_hard, satisfied_soft = _character_gold_compatibility(
            character=character,
            gold_service=gold_service,
            gold_place=gold_place,
            service_type=service_type,
        )
        if score >= 0:
            scored.append((score, character, satisfied_hard, satisfied_soft))

    if not scored:
        return None, {}

    scored.sort(key=lambda item: -item[0])
    top_threshold = max(1, len(scored) // 3)
    top_pool = scored[:top_threshold]
    _chosen_score, chosen_char, chosen_hard, chosen_soft = rng.choice(top_pool)

    constraints = _derive_character_constraints(
        character=chosen_char,
        gold_service=gold_service,
        gold_place=gold_place,
        service_type=service_type,
        satisfied_hard=chosen_hard,
        satisfied_soft=chosen_soft,
    )
    return chosen_char, constraints


def _select_discriminative_character(
    *,
    gold_service: dict[str, Any],
    gold_place: dict[str, Any],
    confusable_candidates: list[dict[str, Any]],
    characters: list[dict[str, Any]],
    service_type: str,
    runtime: LocalServiceAppRuntime,
    rng: random.Random,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Select a character with strong constraint discrimination against confusable candidates."""
    scored: list[tuple[float, int, dict[str, Any], list[str], list[str], int]] = []

    for character in characters:
        compat_score, satisfied_hard, satisfied_soft = _character_gold_compatibility(
            character=character,
            gold_service=gold_service,
            gold_place=gold_place,
            service_type=service_type,
        )
        if compat_score < 0:
            continue

        discrim_score, excluded_count = _character_discrimination_score(
            character=character,
            gold_service=gold_service,
            gold_place=gold_place,
            confusable_candidates=confusable_candidates,
            runtime=runtime,
        )
        if excluded_count == 0:
            discrim_score = 0

        combined = compat_score * 0.4 + discrim_score * 0.6
        scored.append((combined, compat_score, character, satisfied_hard, satisfied_soft, excluded_count))

    if not scored:
        return None, {}

    scored.sort(key=lambda item: -item[0])
    top_threshold = max(1, len(scored) // 3)
    top_pool = scored[:top_threshold]
    _chosen_combined, _chosen_compat, chosen_char, chosen_hard, chosen_soft, _excluded = rng.choice(top_pool)

    constraints = _derive_character_constraints(
        character=chosen_char,
        gold_service=gold_service,
        gold_place=gold_place,
        service_type=service_type,
        satisfied_hard=chosen_hard,
        satisfied_soft=chosen_soft,
    )
    return chosen_char, constraints


def _character_discrimination_score(
    *,
    character: dict[str, Any],
    gold_service: dict[str, Any],
    gold_place: dict[str, Any],
    confusable_candidates: list[dict[str, Any]],
    runtime: LocalServiceAppRuntime,
) -> tuple[int, int]:
    """Return (discrimination_score, excluded_count) for role constraints vs confusable candidates."""
    del gold_place
    prefs = character.get("preferences", {})
    mobility = character.get("mobility", {})
    budget = prefs.get("budget", "medium")
    allowed_prices = BUDGET_PRICE_COMPATIBILITY.get(budget, {"low", "medium"})
    max_walk = int(mobility.get("max_walk_m", 800))
    char_loc = {"lat": mobility.get("lat"), "lng": mobility.get("lng")}

    prefer_text = " ".join(prefs.get("prefer", []))
    avoid_text = " ".join(prefs.get("avoid", []))
    all_pref_text = f"{prefer_text} {avoid_text}"

    gold_detail = runtime.call("get_service_detail", {
        "service_id": gold_service.get("service_id", ""),
    }).get("detail", {}) or gold_service
    gold_attrs = extract_service_attrs(gold_detail)

    score = 0
    excluded = 0

    for candidate in confusable_candidates:
        detail = runtime.call("get_service_detail", {
            "service_id": candidate.get("service_id", ""),
        }).get("detail", {})
        if not detail:
            continue

        cand_attrs = extract_service_attrs(detail)
        violations = _attribute_violations(
            gold_attrs,
            cand_attrs,
            allowed_prices=allowed_prices,
            max_walk=max_walk,
            char_loc=char_loc,
            cand_loc=detail.get("location", {}),
            all_pref_text=all_pref_text,
            avoid_text=avoid_text,
        )

        if violations:
            excluded += 1
            if any("超出" in v or "过长" in v or "过高" in v for v in violations):
                score += 3
            else:
                score += 1

    return score, excluded


def _attribute_violations(
    gold_attrs: dict[str, Any],
    cand_attrs: dict[str, Any],
    *,
    allowed_prices: set[str],
    max_walk: int,
    char_loc: dict[str, Any],
    cand_loc: dict[str, Any],
    all_pref_text: str,
    avoid_text: str,
) -> list[str]:
    """Check candidate violations against role constraints."""
    violations: list[str] = []

    cand_price = cand_attrs.get("price_band", "")
    if cand_price and cand_price not in allowed_prices:
        violations.append(f"价格档位 {cand_price} 超出预算")

    if char_loc.get("lat") is not None and char_loc.get("lng") is not None and cand_loc:
        walk_dist = distance_m(char_loc, cand_loc)
        if walk_dist > max_walk * 1.5:
            violations.append(f"距离约 {walk_dist:.0f}m 超出步行上限 {max_walk}m")

    if any(kw in all_pref_text for kw in NOISY_AVOID_KEYWORDS):
        gold_noise = gold_attrs.get("noise_level", "")
        cand_noise = cand_attrs.get("noise_level", "")
        if gold_noise in {"low", "medium_low"} and cand_noise == "high":
            violations.append(f"噪声水平 {cand_noise} 过高")

    if any(kw in avoid_text for kw in QUEUE_AVOID_KEYWORDS):
        gold_queue = gold_attrs.get("queue_time_min")
        cand_queue = cand_attrs.get("queue_time_min")
        if gold_queue is not None and cand_queue is not None:
            if int(cand_queue) > 30 and int(gold_queue) <= 20:
                violations.append(f"排队 {cand_queue} 分钟过长")

    if any(kw in avoid_text for kw in CROWD_AVOID_KEYWORDS):
        gold_crowd = gold_attrs.get("crowding_level", "")
        cand_crowd = cand_attrs.get("crowding_level", "")
        if gold_crowd == "low" and cand_crowd == "high":
            violations.append(f"拥挤度 {cand_crowd} 过高")

    if any(kw in all_pref_text for kw in INDOOR_PREFER_KEYWORDS):
        gold_indoor = gold_attrs.get("indoor_available")
        cand_indoor = cand_attrs.get("indoor_available")
        if gold_indoor and not cand_indoor:
            violations.append("无室内座位")

    return violations


def _character_gold_compatibility(
    *,
    character: dict[str, Any],
    gold_service: dict[str, Any],
    gold_place: dict[str, Any],
    service_type: str,
) -> tuple[int, list[str], list[str]]:
    prefs = character.get("preferences", {})
    mobility = character.get("mobility", {})
    prefer_text = " ".join(prefs.get("prefer", []))
    avoid_text = " ".join(prefs.get("avoid", []))
    all_pref_text = f"{prefer_text} {avoid_text}"

    satisfied_hard: list[str] = []
    satisfied_soft: list[str] = []
    score = 0

    budget = prefs.get("budget", "medium")
    price_band = _service_attr(gold_service, "price_band") or "medium"
    allowed = BUDGET_PRICE_COMPATIBILITY.get(budget, {"low", "medium"})
    if price_band not in allowed:
        return -1, [], []
    satisfied_hard.append(f"预算 {budget} 兼容价格档位 {price_band}")

    max_walk = int(mobility.get("max_walk_m", 800))
    char_lat = mobility.get("lat")
    char_lng = mobility.get("lng")
    gold_loc = gold_place.get("location", {})
    if char_lat is not None and char_lng is not None and gold_loc:
        dist = distance_m({"lat": char_lat, "lng": char_lng}, gold_loc)
        if dist > max_walk * 1.5:
            return -1, [], []
        satisfied_hard.append(f"距角色位置约 {dist:.0f}m，在 {max_walk}m 可接受范围内")

    noise_level = _service_attr(gold_service, "noise_level") or ""
    prefers_quiet = any(kw in all_pref_text for kw in QUIET_PREFER_KEYWORDS)
    avoids_noisy = any(kw in all_pref_text for kw in NOISY_AVOID_KEYWORDS)
    if (prefers_quiet or avoids_noisy) and noise_level in {"low", "medium_low"}:
        score += 3
        satisfied_soft.append(f"噪声水平 {noise_level} 符合安静偏好")
    elif (prefers_quiet or avoids_noisy) and noise_level == "high":
        score -= 3

    avoids_queue = any(kw in avoid_text for kw in QUEUE_AVOID_KEYWORDS)
    queue_time = _service_attr(gold_service, "queue_time_min")
    if avoids_queue and queue_time is not None:
        if int(queue_time) <= 20:
            score += 2
            satisfied_soft.append(f"排队约 {queue_time} 分钟，较短")
        elif int(queue_time) > 30:
            score -= 2

    avoids_crowd = any(kw in avoid_text for kw in CROWD_AVOID_KEYWORDS)
    crowding = _service_attr(gold_service, "crowding_level") or ""
    if avoids_crowd and crowding == "low":
        score += 2
        satisfied_soft.append(f"拥挤度 {crowding}，不拥挤")
    elif avoids_crowd and crowding == "high":
        score -= 1

    prefers_indoor = any(kw in prefer_text for kw in INDOOR_PREFER_KEYWORDS)
    indoor = _service_attr(gold_service, "indoor_available")
    if prefers_indoor and indoor:
        score += 2
        satisfied_soft.append("有室内座位/可停留")

    if budget == "high" and price_band in {"high", "medium_high"}:
        score += 1
        satisfied_soft.append("高预算匹配高档场所")

    return score, satisfied_hard, satisfied_soft


def _derive_character_constraints(
    *,
    character: dict[str, Any],
    gold_service: dict[str, Any],
    gold_place: dict[str, Any],
    service_type: str,
    satisfied_hard: list[str],
    satisfied_soft: list[str],
) -> dict[str, Any]:
    del gold_service, gold_place
    prefs = character.get("preferences", {})
    mobility = character.get("mobility", {})
    budget = prefs.get("budget", "medium")
    max_walk = int(mobility.get("max_walk_m", 800))
    dietary = prefs.get("dietary", "")

    hard_constraints: list[str] = []
    soft_preferences: list[str] = []

    budget_label = {"low": "平价", "medium": "中等", "high": "较高"}.get(budget, "中等")
    hard_constraints.append(f"预算{budget_label}，不接受超出预算的选项")
    hard_constraints.append(f"步行距离不超过 {max_walk} 米")

    if service_type == "restaurant_reservation" and dietary and dietary not in {"无", ""}:
        hard_constraints.append(f"饮食限制：{dietary}")

    for item in prefs.get("prefer", [])[:3]:
        soft_preferences.append(f"偏好：{item}")
    for item in prefs.get("avoid", [])[:3]:
        soft_preferences.append(f"避免：{item}")

    return {
        "hard_constraints": hard_constraints,
        "soft_preferences": soft_preferences,
        "satisfied_hard": satisfied_hard,
        "satisfied_soft": satisfied_soft,
        "character_budget": budget,
        "character_max_walk_m": max_walk,
        "character_dietary": dietary,
        "character_home_district_id": mobility.get("home_district_id", ""),
    }
