"""DynamicsBuilder for map/service live city worlds."""

from __future__ import annotations

import random
from typing import Any

from shared.schema import EventTemplate, InformationEffect, InformationItem, Provenance, StateTransition


def build_event_templates(
    world: dict[str, Any],
    services: list[dict[str, Any]],
    information: list[InformationItem],
    rng: random.Random,
    built_at: str,
    storylines: list[dict[str, Any]],
) -> list[EventTemplate]:
    reservable = [s for s in services if "reservable" in s.get("affordances", [])]
    weather_sensitive = [s for s in services if s.get("state_model", {}).get("weather_sensitive")]
    templates: list[EventTemplate] = []

    for service in rng.sample(services, k=min(len(services), 8)):
        templates.append(
            EventTemplate(
                event_template_id=f"evt_{service['service_id']}_peak_queue",
                name=f"Peak demand increases queue for {service['service_id']}",
                trigger_type="time",
                scope_type="service",
                preconditions=["time.window=evening_peak"],
                state_transitions=[
                    StateTransition(service["service_id"], "state_model.queue_time_min", "add", rng.choice([10, 15, 20])),
                    StateTransition(service["service_id"], "state_model.crowding_level", "set", "high"),
                ],
                information_effects=[InformationEffect(channel="LocalServiceApp", visibility_delay_seconds=rng.choice([0, 60, 180]))],
                app_event_projection={"LocalServiceApp": {"event": "service_state_refresh"}},
                director_notes={"task_use": ["force_replan", "compare_wait_time"]},
                provenance=_dynamics_prov(built_at),
            )
        )

    for service in rng.sample(reservable, k=min(len(reservable), 6)):
        slot = rng.choice(service.get("state_model", {}).get("reservation_slots", ["19:00"]))
        templates.append(
            EventTemplate(
                event_template_id=f"evt_{service['service_id']}_slot_loss",
                name=f"Reservation slot disappears for {service['service_id']}",
                trigger_type="service_state",
                scope_type="service",
                preconditions=[f"reservation_slot={slot}"],
                state_transitions=[
                    StateTransition(service["service_id"], f"state_model.reservation_inventory.{slot}", "set", 0)
                ],
                information_effects=[InformationEffect(channel="LocalServiceApp", visibility_delay_seconds=0)],
                app_event_projection={"LocalServiceApp": {"event": "slot_inventory_changed"}},
                director_notes={"task_use": ["reservation_race", "alternate_slot_required"]},
                provenance=_dynamics_prov(built_at),
            )
        )

    for service in rng.sample(weather_sensitive or services, k=min(len(weather_sensitive or services), 5)):
        templates.append(
            EventTemplate(
                event_template_id=f"evt_{service['service_id']}_rain_disruption",
                name=f"Rain changes feasibility for {service['service_id']}",
                trigger_type="weather",
                scope_type="service",
                preconditions=["weather.condition=rain"],
                state_transitions=[
                    StateTransition(service["service_id"], "state_model.queue_time_min", "add", 15),
                    StateTransition(service["service_id"], "state_model.weather_suitability", "set", "poor"),
                ],
                information_effects=[InformationEffect(channel="WeatherCityEventApp", visibility_delay_seconds=0)],
                app_event_projection={"WeatherCityEventApp": {"event": "rain_alert"}},
                director_notes={"task_use": ["indoor_preference", "weather_replan"]},
                provenance=_dynamics_prov(built_at),
            )
        )

    for corridor in rng.sample(world.get("corridors", []), k=min(len(world.get("corridors", [])), 6)):
        templates.append(
            EventTemplate(
                event_template_id=f"evt_{corridor['corridor_id']}_delay",
                name=f"Mobility delay on {corridor['name']}",
                trigger_type="traffic" if "road" in corridor.get("corridor_type", "") else "transit",
                scope_type="corridor",
                preconditions=["time.window=commute_or_evening_peak"],
                # The factual MapApp projection is immutable. A simulated
                # mobility disruption is a public city notice, not a rewrite
                # of a Google-derived route or corridor fact.
                state_transitions=[],
                information_effects=[InformationEffect(channel="CityNewsApp", visibility_delay_seconds=rng.choice([0, 120, 300]))],
                app_event_projection={"CityNewsApp": {"event": "mobility_notice"}},
                director_notes={"task_use": ["arrival_constraint", "mode_switch"]},
                provenance=_dynamics_prov(built_at),
            )
        )

    stale_infos = [info for info in information if info.observability.staleness == "stale" or info.observability.conflict_group]
    for stale_info in rng.sample(stale_infos or information, k=min(4, len(stale_infos or information))):
        templates.append(
            EventTemplate(
                event_template_id=f"evt_{stale_info.info_id}_conflict_update",
                name="A stale or contested city claim conflicts with a newer update",
                trigger_type="information_update",
                scope_type="information",
                preconditions=["agent_has_seen_review_or_post"],
                information_effects=[
                    InformationEffect(
                        info_id=stale_info.info_id,
                        channel="ReviewSocialApp",
                        visibility_delay_seconds=180,
                        payload={"conflict": "newer_or_contested_city_state_available"},
                    )
                ],
                app_event_projection={"ReviewSocialApp": {"event": "information_conflict"}},
                director_notes={"task_use": ["information_conflict", "source_freshness"]},
                provenance=_dynamics_prov(built_at),
            )
        )

    for index, storyline in enumerate(storylines[:8]):
        if not services:
            break
        service = services[index % len(services)]
        event_text = "; ".join(storyline.get("dynamic_events", [])[:3]) or "city conditions change after planning starts"
        goal_type = storyline.get("goal_type", service.get("service_type", "local_service"))
        channel = _channel_from_storyline(event_text)
        templates.append(
            EventTemplate(
                event_template_id=f"evt_storyline_{index + 1}_{service['service_id']}",
                name=storyline.get("name", f"LLM-designed dynamic pattern {index + 1}"),
                trigger_type=_trigger_from_storyline(event_text, goal_type),
                scope_type="service",
                preconditions=[storyline.get("geographic_setup", "after_initial_plan_started")],
                state_transitions=[
                    StateTransition(service["service_id"], "state_model.dynamic_storyline_pressure", "set", event_text),
                    StateTransition(service["service_id"], "state_model.queue_time_min", "add", 10),
                ],
                information_effects=[
                    InformationEffect(
                        channel=channel,
                        visibility_delay_seconds=rng.choice([0, 120, 300]),
                        payload={"storyline": storyline.get("name", ""), "success_condition": storyline.get("success_condition", "")},
                    )
                ],
                app_event_projection={channel: {"event": "llm_designed_city_change", "goal_type": goal_type}},
                director_notes={
                    "task_use": ["llm_storyline", goal_type],
                    "dynamic_events": storyline.get("dynamic_events", []),
                    "success_condition": storyline.get("success_condition", ""),
                },
                provenance=_dynamics_prov(built_at),
            )
        )

    return templates


def _trigger_from_storyline(text: str, goal_type: str) -> str:
    lower = f"{text} {goal_type}".lower()
    if any(token in lower for token in ["rain", "weather", "heat", "storm"]):
        return "weather"
    if any(token in lower for token in ["train", "transit", "station", "transfer", "last train"]):
        return "transit"
    if any(token in lower for token in ["traffic", "road", "taxi"]):
        return "traffic"
    if any(token in lower for token in ["slot", "reservation", "closed", "availability", "queue"]):
        return "service_state"
    return "information_update"


def _channel_from_storyline(text: str) -> str:
    trigger = _trigger_from_storyline(text, "")
    return {
        "weather": "WeatherCityEventApp",
        "traffic": "CityNewsApp",
        "transit": "CityNewsApp",
        "service_state": "LocalServiceApp",
        "information_update": "ReviewSocialApp",
    }[trigger]


def _dynamics_prov(built_at: str) -> Provenance:
    return Provenance(
        source=["v7_dynamics_builder"],
        built_at=built_at,
        generator="pipeline.worldgen.builders.dynamics_builder",
    )
