"""ServiceCapabilityBuilder for v7 live city worlds."""

from __future__ import annotations

import random
from typing import Any


def enrich_service_state(
    services: list[dict[str, Any]],
    places_by_id: dict[str, dict[str, Any]],
    rng: random.Random,
) -> None:
    """Attach executable runtime state to service endpoints."""
    for service in services:
        place = places_by_id.get(service["place_id"], {})
        rating = place.get("map_projection", {}).get("rating") or 4.0
        primary_type = place.get("primary_type", "")
        queue = rng.choice([5, 10, 15, 20, 30])
        if rating >= 4.3:
            queue += 10
        state = service.setdefault("state_model", {})
        state.update(
            {
                "open_status": "open",
                "queue_time_min": queue,
                "crowding_level": rng.choice(["low", "medium", "high"]),
                "noise_level": _noise_level(primary_type, rng),
                "indoor_available": primary_type not in {"park"},
                "weather_sensitive": primary_type in {"park", "tourist_attraction"},
                "price_band": rng.choice(["low", "medium", "medium_high", "high"]),
                "reservation_slots": _slots_for(service.get("service_type", ""), rng),
                "max_party_size": rng.choice([2, 4, 4, 6]),
                "last_updated": "scenario_start",
            }
        )
        if "reservable" in service.get("affordances", []):
            state["reservation_inventory"] = {
                slot: rng.choice([0, 1, 2, 3]) for slot in state["reservation_slots"]
            }


def _slots_for(service_type: str, rng: random.Random) -> list[str]:
    if service_type == "restaurant_reservation":
        return rng.sample(["18:00", "18:30", "19:00", "19:30", "20:00", "20:30"], k=4)
    if service_type == "hotel_booking":
        return ["tonight", "tomorrow"]
    return ["now", "later_today"]


def _noise_level(primary_type: str, rng: random.Random) -> str:
    if primary_type in {"bar", "shopping_mall"}:
        return rng.choice(["medium", "high"])
    if primary_type in {"park", "hospital"}:
        return rng.choice(["low", "medium_low"])
    return rng.choice(["low", "medium_low", "medium", "high"])
