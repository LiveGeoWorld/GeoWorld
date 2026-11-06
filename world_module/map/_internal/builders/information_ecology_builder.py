"""InformationEcologyBuilder for v7 live city worlds.

Information items are observable claims, not truth. Real store reviews should
come from Google Place Details when available; synthetic runtime information is
kept as service/city claims rather than pretending to be user reviews.
"""

from __future__ import annotations

import random
from typing import Any

from shared.schema import InformationItem, Observability, Provenance


def build_information_ecology(
    world: dict[str, Any],
    services: list[dict[str, Any]],
    places_by_id: dict[str, dict[str, Any]],
    rng: random.Random,
    built_at: str,
    storylines: list[dict[str, Any]],
) -> dict[str, Any]:
    # Base city artifacts carry source-backed review text only. Synthetic city
    # feeds and service claims are task-time scenario material.
    information = _build_google_place_reviews(services, places_by_id, built_at)
    graph = _information_graph(information)
    quality = _information_quality(information)
    return {"information_items": information, "information_graph": graph, "information_quality": quality}


def _build_google_place_reviews(
    services: list[dict[str, Any]],
    places_by_id: dict[str, dict[str, Any]],
    built_at: str,
) -> list[InformationItem]:
    infos = []
    service_by_place_id = {service["place_id"]: service for service in services}
    for place_id, place in places_by_id.items():
        service = service_by_place_id.get(place_id)
        map_projection = place.get("map_projection", {})
        details = map_projection.get("google_place_details", {})
        for index, review in enumerate(details.get("reviews", [])):
            content = review.get("text") or review.get("original_text") or ""
            if not content:
                continue
            review_id = _safe_id(review.get("review_id") or f"{place_id}_{index + 1}")
            infos.append(
                InformationItem(
                    info_id=f"info_google_review_{place_id}_{review_id}",
                    content_type="review",
                    about={
                        "place_id": place_id,
                        **({"service_id": service["service_id"]} if service else {}),
                    },
                    content=content,
                    observability=Observability(
                        visible_via=["ReviewSocialApp"],
                        available_at="scenario_start",
                        visibility="visible",
                        confidence="high",
                        staleness="google_reported",
                    ),
                    supports_constraints=["place_reputation", "service_experience", "user_sentiment"],
                    app_payload={
                        "source": "google_place_details",
                        "author_type": "google_reviewer",
                        "author_id": review.get("author_uri") or review.get("author_display_name", ""),
                        "author_display_name": review.get("author_display_name", ""),
                        "claim_type": "place_review",
                        "rating": review.get("rating"),
                        "place_rating": map_projection.get("rating"),
                        "place_user_rating_count": map_projection.get("user_rating_count"),
                        "relative_publish_time": review.get("relative_publish_time", ""),
                        "publish_time": review.get("publish_time", ""),
                        "language_code": review.get("language_code", ""),
                    },
                    provenance=_google_review_prov(built_at),
                )
            )
    return infos


def _build_runtime_service_claims(
    services: list[dict[str, Any]],
    places_by_id: dict[str, dict[str, Any]],
    rng: random.Random,
    built_at: str,
) -> list[InformationItem]:
    infos = []
    for index, service in enumerate(services[: min(60, len(services))]):
        place = places_by_id.get(service["place_id"], {})
        state = service.get("state_model", {})
        conflict_group = f"service_runtime_{service['place_id']}" if rng.random() < 0.35 else None
        infos.append(
            InformationItem(
                info_id=f"info_{service['service_id']}_runtime_claim",
                content_type="service_claim",
                about={"place_id": service["place_id"], "service_id": service["service_id"]},
                content=_runtime_service_text(place, state, rng),
                observability=Observability(
                    visible_via=["LocalServiceApp", "MapApp"],
                    available_at="scenario_start",
                    visibility="visible",
                    confidence=rng.choice(["medium", "high"]),
                    staleness=rng.choice(["fresh", "recent"]),
                    conflict_group=conflict_group,
                ),
                supports_constraints=["noise_level", "price_band", "queue_time_min", "crowding_level"],
                app_payload={
                    "source": "synthetic_runtime_overlay",
                    "claim_type": "service_runtime_state",
                    "evidence_strength": rng.choice(["medium", "strong"]),
                    "queue_time_min": state.get("queue_time_min"),
                    "crowding_level": state.get("crowding_level"),
                    "noise_level": state.get("noise_level"),
                    "price_band": state.get("price_band"),
                },
                provenance=_info_prov(built_at),
            )
        )
        if conflict_group and index % 2 == 0:
            infos.append(
                InformationItem(
                    info_id=f"info_{service['service_id']}_runtime_counterclaim",
                    content_type="service_claim",
                    about={"place_id": service["place_id"], "service_id": service["service_id"]},
                    content=f"Current conditions at {place.get('name', service['service_id'])} can shift by time window; refresh service state before committing.",
                    observability=Observability(
                        visible_via=["LocalServiceApp", "MapApp"],
                        available_at="after_service_refresh",
                        visibility="delayed",
                        confidence="medium",
                        staleness="fresh",
                        conflict_group=conflict_group,
                    ),
                    supports_constraints=["source_freshness", "time_window_sensitivity"],
                    app_payload={
                        "source": "synthetic_runtime_overlay",
                        "claim_type": "runtime_counterclaim",
                        "evidence_strength": "medium",
                    },
                    provenance=_info_prov(built_at),
                )
            )
    return infos


def _build_service_updates(
    services: list[dict[str, Any]],
    places_by_id: dict[str, dict[str, Any]],
    rng: random.Random,
    built_at: str,
) -> list[InformationItem]:
    infos = []
    for service in services[: min(45, len(services))]:
        if rng.random() > 0.55:
            continue
        place = places_by_id.get(service["place_id"], {})
        infos.append(
            InformationItem(
                info_id=f"info_{service['service_id']}_availability_push",
                content_type="service_update",
                about={"service_id": service["service_id"]},
                content=f"Availability for {place.get('name', service['service_id'])} may change during peak dining windows.",
                observability=Observability(
                    visible_via=["LocalServiceApp"],
                    available_at="after_service_refresh",
                    visibility="delayed",
                    confidence="medium",
                    staleness="fresh",
                ),
                supports_constraints=["reservation_inventory", "open_status", "queue_time_min"],
                app_payload={"refresh_required": True, "claim_type": "service_operability"},
                provenance=_info_prov(built_at),
            )
        )
    return infos


def _build_city_feed_posts(
    world: dict[str, Any],
    rng: random.Random,
    built_at: str,
) -> list[InformationItem]:
    infos = []
    districts = world.get("districts", [])
    corridors = world.get("corridors", [])
    for district in districts:
        for post_index in range(2):
            claim_type = rng.choice(["crowding", "meeting_point_reliability", "station_exit_friction", "event_spillover"])
            conflict_group = f"{district['district_id']}_{claim_type}_evening"
            infos.append(
                InformationItem(
                    info_id=f"info_post_{district['district_id']}_{claim_type}_{post_index + 1}",
                    content_type="social_post",
                    about={"district_id": district["district_id"]},
                    content=_district_post_text(district, claim_type, rng),
                    observability=Observability(
                        visible_via=["ReviewSocialApp"],
                        available_at="scenario_start" if post_index == 0 else "after_social_refresh",
                        visibility="visible" if post_index == 0 else rng.choice(["delayed", "conditional"]),
                        confidence=rng.choice(["low", "medium", "medium", "high"]),
                        staleness=rng.choice(["fresh", "fresh", "recent"]),
                        conflict_group=conflict_group,
                    ),
                    supports_constraints=[claim_type, "walking_friction", "route_reliability"],
                    app_payload={
                        "author_type": "public_city_feed",
                        "author_id": "anonymous_public",
                        "claim_type": claim_type,
                        "sentiment": rng.choice(["negative", "mixed", "neutral"]),
                        "evidence_strength": rng.choice(["weak", "medium", "medium"]),
                    },
                    provenance=_info_prov(built_at),
                )
            )
    for corridor in corridors:
        claim_type = rng.choice(["transit_delay_risk", "crowding", "transfer_complexity"])
        infos.append(
            InformationItem(
                info_id=f"info_post_{corridor['corridor_id']}_{claim_type}",
                content_type="social_post",
                about={"corridor_id": corridor["corridor_id"]},
                content=_corridor_post_text(corridor, claim_type),
                observability=Observability(
                    visible_via=["ReviewSocialApp", "MapApp"],
                    available_at="after_map_or_social_refresh",
                    visibility=rng.choice(["visible", "delayed", "conditional"]),
                    confidence=rng.choice(["low", "medium", "medium"]),
                    staleness=rng.choice(["fresh", "recent"]),
                    conflict_group=f"{corridor['corridor_id']}_{claim_type}",
                ),
                supports_constraints=[claim_type, "arrival_risk", "mode_switch"],
                app_payload={
                    "author_type": "public_city_feed",
                    "author_id": "anonymous_public",
                    "claim_type": claim_type,
                    "sentiment": "mixed",
                    "evidence_strength": "weak",
                },
                provenance=_info_prov(built_at),
            )
        )
    return infos


def _build_local_tips(
    world: dict[str, Any],
    rng: random.Random,
    built_at: str,
) -> list[InformationItem]:
    tips = []
    places = world.get("places", [])
    for place in rng.sample(places, k=min(40, len(places))):
        tips.append(
            InformationItem(
                info_id=f"info_tip_{place['place_id']}",
                content_type="local_tip",
                about={"place_id": place["place_id"]},
                content=f"Local tip: around {place.get('name', 'this area')}, meeting slightly away from the main exit can reduce walking friction during peaks.",
                observability=Observability(
                    visible_via=["ReviewSocialApp"],
                    available_at="scenario_start",
                    visibility=rng.choice(["visible", "conditional"]),
                    confidence=rng.choice(["medium", "high"]),
                    staleness="recent",
                ),
                supports_constraints=["meeting_point_reliability", "walking_friction"],
                app_payload={
                    "author_type": "public_city_feed",
                    "author_id": "anonymous_public",
                    "claim_type": "local_tip",
                    "evidence_strength": "medium",
                },
                provenance=_info_prov(built_at),
            )
        )
    return tips


def _build_storyline_hints(
    services: list[dict[str, Any]],
    storylines: list[dict[str, Any]],
    built_at: str,
) -> list[InformationItem]:
    infos = []
    for index, storyline in enumerate(storylines[:8]):
        if not services:
            break
        service = services[index % len(services)]
        infos.append(
            InformationItem(
                info_id=f"info_storyline_{index + 1}_{service['service_id']}",
                content_type="scenario_hint",
                about={"service_id": service["service_id"]},
                content=f"{storyline.get('name', 'Dynamic city pattern')}: {'; '.join(storyline.get('initial_information', [])[:3])}",
                observability=Observability(
                    visible_via=["ReviewSocialApp", "LocalServiceApp", "MapApp"],
                    available_at="scenario_start",
                    visibility="conditional",
                    confidence="medium",
                    staleness="scenario_dependent",
                ),
                supports_constraints=["scenario_design", "dynamic_replanning"],
                app_payload={"storyline": storyline, "claim_type": "scenario_hint"},
                provenance=_info_prov(built_at),
            )
        )
    return infos


def _information_graph(information: list[InformationItem]) -> dict[str, Any]:
    by_conflict_group: dict[str, list[str]] = {}
    by_author: dict[str, list[str]] = {}
    by_about: dict[str, list[str]] = {}
    for info in information:
        if info.observability.conflict_group:
            by_conflict_group.setdefault(info.observability.conflict_group, []).append(info.info_id)
        author_id = info.app_payload.get("author_id")
        if author_id:
            by_author.setdefault(author_id, []).append(info.info_id)
        for target_id in info.about.values():
            by_about.setdefault(target_id, []).append(info.info_id)
    return {
        "by_conflict_group": by_conflict_group,
        "by_author": by_author,
        "by_about": by_about,
    }


def _information_quality(information: list[InformationItem]) -> dict[str, Any]:
    errors = []
    warnings = []
    type_counts: dict[str, int] = {}
    delayed_like = 0
    conflict_groups = set()
    for info in information:
        type_counts[info.content_type] = type_counts.get(info.content_type, 0) + 1
        if info.observability.visibility != "visible":
            delayed_like += 1
        if info.observability.conflict_group:
            conflict_groups.add(info.observability.conflict_group)
    if type_counts.get("review", 0) == 0:
        warnings.append("no real Google reviews were returned for the retained POIs")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "type_counts": type_counts}


def _runtime_service_text(place: dict[str, Any], state: dict[str, Any], rng: random.Random) -> str:
    name = place.get("name", "This place")
    return rng.choice(
        [
            f"{name} currently reports {state.get('noise_level', 'medium')} noise, price band {state.get('price_band', 'medium')}, and queue around {state.get('queue_time_min', 10)} minutes.",
            f"{name} is operational for quick plans, but current queue and crowding should be refreshed before committing.",
            f"{name} is convenient, though nearby station traffic can change the current service experience.",
        ]
    )


def _district_post_text(district: dict[str, Any], claim_type: str, rng: random.Random) -> str:
    name = district.get("name", "this area")
    templates = {
        "crowding": f"Public feed: {name} feels crowded around the main exits this evening; leave buffer for meeting up.",
        "meeting_point_reliability": f"Public feed: meeting directly inside {name} can be confusing; a secondary landmark may be safer.",
        "station_exit_friction": f"Public feed: station exits around {name} are uneven today; short walks may take longer than expected.",
        "event_spillover": f"Public feed: an event or shopping flow near {name} is spilling into restaurants and transit paths.",
    }
    return templates.get(claim_type, f"Public feed: {name} has changing local conditions.")


def _corridor_post_text(corridor: dict[str, Any], claim_type: str) -> str:
    name = corridor.get("name", "this route")
    if claim_type == "transit_delay_risk":
        return f"Transit chatter: {name} is still usable, but small delays may affect tight dinner arrivals."
    if claim_type == "transfer_complexity":
        return f"Transit chatter: transfers on {name} are harder for visitors during peak crowds."
    return f"Transit chatter: {name} feels crowded enough that backup routes are worth checking."


def _info_prov(built_at: str) -> Provenance:
    return Provenance(
        source=["v7_information_ecology_builder"],
        built_at=built_at,
        generator="pipeline_v2.map_module.builders.information_ecology_builder",
    )


def _google_review_prov(built_at: str) -> Provenance:
    return Provenance(
        source=["google_places_details"],
        built_at=built_at,
        generator="pipeline_v2.map_module.builders.information_ecology_builder",
    )


def _safe_id(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value))[:80].strip("_") or "review"
