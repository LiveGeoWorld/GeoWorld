"""Objective search ambiguity metrics for place_discovery sampling."""

from __future__ import annotations

from typing import Any

from app.runtime.geo import distance_m
from app.runtime.local_service import LocalServiceAppRuntime


BUDGET_PRICE_COMPATIBILITY: dict[str, set[str]] = {
    "low": {"low"},
    "medium": {"low", "medium"},
    "high": {"low", "medium", "medium_high", "high"},
}

NOISY_AVOID_KEYWORDS = ["嘈杂", "吵", "热闹", "音响", "网红"]
QUEUE_AVOID_KEYWORDS = ["排队", "等太久", "等很久", "长时间"]
CROWD_AVOID_KEYWORDS = ["拥挤", "推挤", "人多", "网红", "游客"]
INDOOR_PREFER_KEYWORDS = ["室内", "坐下", "停留", "靠背", "坐着", "插座"]
QUIET_PREFER_KEYWORDS = ["安静", "柔和", "不吵", "不嘈杂", "舒适"]

PRICE_BAND_ORDER = {"low": 1, "medium": 2, "medium_low": 1.5, "medium_high": 3, "high": 4}


def extract_service_attrs(service: dict[str, Any]) -> dict[str, Any]:
    """Extract comparable attributes from a service or service detail record."""
    state = service.get("state_model", {})
    return {
        "price_band": service.get("price_band") or state.get("price_band") or "medium",
        "noise_level": service.get("noise_level") or state.get("noise_level") or "",
        "queue_time_min": service.get("queue_time_min") if service.get("queue_time_min") is not None else state.get("queue_time_min"),
        "crowding_level": service.get("crowding_level") or state.get("crowding_level") or "",
        "indoor_available": service.get("indoor_available") if service.get("indoor_available") is not None else state.get("indoor_available"),
        "rating": service.get("rating") or 0,
    }

def compute_search_ambiguity_metrics(
    *,
    gold_service: dict[str, Any],
    gold_place: dict[str, Any],
    candidates: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    """Count service candidate density around a gold service."""
    loc = gold_place.get("location", {})
    district_id = gold_place.get("district_id", "")
    primary_type = gold_place.get("primary_type", "")
    place_id = gold_place.get("place_id", "")

    same_district = [place for _service, place in candidates if place.get("district_id") == district_id]
    nearby = [
        place
        for _service, place in candidates
        if place.get("place_id") != place_id
        and loc
        and place.get("location")
        and distance_m(loc, place.get("location", {})) <= 900
    ]
    same_primary = [place for _service, place in candidates if place.get("primary_type") == primary_type]

    state = gold_service.get("state_model", {})
    projection = gold_place.get("map_projection", {})
    verifiable_attrs = sum(
        1
        for value in [
            projection.get("rating"),
            projection.get("user_rating_count"),
            state.get("price_band"),
            state.get("queue_time_min"),
            state.get("crowding_level"),
            state.get("noise_level"),
            state.get("indoor_available"),
            state.get("open_status"),
        ]
        if value not in (None, "")
    )

    return {
        "same_district_service_count": len(same_district),
        "nearby_same_service_count_900m": len(nearby),
        "same_primary_type_count": len(same_primary),
        "verifiable_attribute_count": verifiable_attrs,
    }


def search_ambiguity_score_from_metrics(metrics: dict[str, Any]) -> int:
    """Map density metrics to a 0-10 search ambiguity score."""
    score = 0.0
    score += min(3.0, float(metrics.get("same_district_service_count", 0)) * 0.35)
    score += min(2.0, float(metrics.get("nearby_same_service_count_900m", 0)) * 0.5)
    score += min(2.0, float(metrics.get("same_primary_type_count", 0)) * 0.15)
    score += min(1.0, float(metrics.get("verifiable_attribute_count", 0)) * 0.2)
    return int(min(10, max(0, round(score))))


def build_search_landscape(
    *,
    gold_service: dict[str, Any],
    gold_place: dict[str, Any],
    candidates: list[tuple[dict[str, Any], dict[str, Any]]],
    runtime: LocalServiceAppRuntime,
    character: dict[str, Any] | None = None,
    character_constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record the full in-scope service panorama and character-driven exclusions."""
    metrics = compute_search_ambiguity_metrics(
        gold_service=gold_service,
        gold_place=gold_place,
        candidates=candidates,
    )
    score = search_ambiguity_score_from_metrics(metrics)
    gold_loc = gold_place.get("location", {})

    candidates_in_scope: list[dict[str, Any]] = []
    for service, place in candidates:
        if place.get("district_id") != gold_place.get("district_id"):
            continue
        dist = round(distance_m(gold_loc, place.get("location", {}))) if gold_loc and place.get("location") else None
        candidates_in_scope.append({
            "place_id": place.get("place_id", ""),
            "service_id": service.get("service_id", ""),
            "name": place.get("name", ""),
            "distance_m": dist,
            "primary_type": place.get("primary_type", ""),
            "district_id": place.get("district_id", ""),
            "is_gold": place.get("place_id") == gold_place.get("place_id"),
        })

    gold_detail = runtime.call("get_service_detail", {"service_id": gold_service.get("service_id", "")}).get("detail", {}) or gold_service

    confusable: list[dict[str, Any]] = []
    for service, place in candidates:
        if place.get("place_id") == gold_place.get("place_id"):
            continue
        dist = round(distance_m(gold_loc, place.get("location", {}))) if gold_loc and place.get("location") else None
        if dist is not None and dist > 1200:
            continue
        exclusion_reasons = _exclusion_reasons(
            runtime=runtime,
            service=service,
            place=place,
            gold_place=gold_place,
            gold_detail=gold_detail,
            character=character,
            character_constraints=character_constraints,
            distance_m_value=dist,
        )
        if not exclusion_reasons:
            exclusion_reasons = _similarity_exclusion_reasons(place, gold_place, dist)
        confusable.append({
            "place_id": place.get("place_id", ""),
            "service_id": service.get("service_id", ""),
            "name": place.get("name", ""),
            "distance_m": dist,
            "primary_type": place.get("primary_type", ""),
            "district_id": place.get("district_id", ""),
            "exclusion_reasons": exclusion_reasons,
        })

    confusable.sort(key=lambda item: item.get("distance_m") if item.get("distance_m") is not None else 10**9)
    return {
        "search_ambiguity_score": score,
        **metrics,
        "candidates_in_scope": candidates_in_scope,
        "confusable_candidates": confusable[:12],
    }


def _exclusion_reasons(
    *,
    runtime: LocalServiceAppRuntime,
    service: dict[str, Any],
    place: dict[str, Any],
    gold_place: dict[str, Any],
    gold_detail: dict[str, Any],
    character: dict[str, Any] | None,
    character_constraints: dict[str, Any] | None,
    distance_m_value: int | None,
) -> list[str]:
    detail = runtime.call("get_service_detail", {"service_id": service.get("service_id", "")}).get("detail", {})
    if not detail:
        return []

    violations: list[str] = []
    if character:
        prefs = character.get("preferences", {})
        budget = prefs.get("budget", "medium")
        allowed_prices = BUDGET_PRICE_COMPATIBILITY.get(budget, {"low", "medium"})
        max_walk = int((character_constraints or {}).get("character_max_walk_m", 800))
        char_mobility = character.get("mobility", {})
        char_loc = {"lat": char_mobility.get("lat"), "lng": char_mobility.get("lng")}
        avoid_text = " ".join(prefs.get("avoid", []))

        d_price = detail.get("price_band", "")
        if d_price and d_price not in allowed_prices:
            violations.append(f"价格档位 {d_price} 超出预算 {budget}")

        d_loc = detail.get("location", {})
        if char_loc.get("lat") and d_loc:
            walk_dist = distance_m(char_loc, d_loc)
            if walk_dist > max_walk * 1.5:
                violations.append(f"距离约 {walk_dist:.0f}m 超出步行上限 {max_walk}m")

        if any(kw in avoid_text for kw in NOISY_AVOID_KEYWORDS) and detail.get("noise_level") == "high":
            violations.append(f"噪声水平 {detail.get('noise_level')} 过高")

        if any(kw in avoid_text for kw in QUEUE_AVOID_KEYWORDS):
            d_queue = detail.get("queue_time_min")
            if d_queue is not None and int(d_queue) > 30:
                violations.append(f"排队 {d_queue} 分钟过长")

        if any(kw in avoid_text for kw in CROWD_AVOID_KEYWORDS) and detail.get("crowding_level") == "high":
            violations.append(f"拥挤度 {detail.get('crowding_level')} 过高")

    if not violations and gold_detail:
        violations = _attribute_diff_reasons(gold_detail, detail, gold_place, place, distance_m_value)

    return violations


def _attribute_diff_reasons(
    gold_detail: dict[str, Any],
    cand_detail: dict[str, Any],
    gold_place: dict[str, Any],
    cand_place: dict[str, Any],
    distance_m_value: int | None,
) -> list[str]:
    """Objective gold-vs-candidate attribute differences when role constraints do not distinguish."""
    reasons: list[str] = []
    gold_attrs = extract_service_attrs(gold_detail)
    cand_attrs = extract_service_attrs(cand_detail)

    gold_rating = float(gold_attrs.get("rating") or 0)
    cand_rating = float(cand_attrs.get("rating") or 0)
    if gold_rating > 0 and cand_rating > 0 and gold_rating - cand_rating >= 0.5:
        reasons.append(f"评分低于 gold {gold_rating - cand_rating:.1f} 分")

    gold_price = gold_attrs.get("price_band", "")
    cand_price = cand_attrs.get("price_band", "")
    if gold_price in PRICE_BAND_ORDER and cand_price in PRICE_BAND_ORDER:
        if PRICE_BAND_ORDER[cand_price] > PRICE_BAND_ORDER[gold_price] + 1:
            reasons.append(f"价格档位 {cand_price} 显著高于 gold {gold_price}")

    gold_queue = gold_attrs.get("queue_time_min")
    cand_queue = cand_attrs.get("queue_time_min")
    if gold_queue is not None and cand_queue is not None:
        if int(cand_queue) - int(gold_queue) >= 15:
            reasons.append(f"排队比 gold 多 {int(cand_queue) - int(gold_queue)} 分钟")

    if distance_m_value is not None and distance_m_value > 800:
        reasons.append(f"距 gold 约 {distance_m_value}m，位置偏远")

    return reasons


def _similarity_exclusion_reasons(
    place: dict[str, Any],
    gold_place: dict[str, Any],
    distance_m_value: int | None,
) -> list[str]:
    reasons: list[str] = []
    if place.get("primary_type") == gold_place.get("primary_type"):
        reasons.append("同类型服务候选")
    if place.get("district_id") == gold_place.get("district_id"):
        reasons.append("同片区候选")
    if distance_m_value is not None and distance_m_value <= 500:
        reasons.append(f"距 gold 约 {distance_m_value}m，易混淆")
    return reasons or ["区域内可见候选"]
