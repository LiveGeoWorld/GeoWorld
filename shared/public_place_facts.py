"""Read-only public projection of frozen map facts; no constraint evaluation."""

from copy import deepcopy
from decimal import Decimal, InvalidOperation

from shared.opening_hours import effective_opening_hours

SERVICE_ATTRIBUTES = (
    "reservable", "dine_in", "takeout", "delivery", "curbside_pickup",
    "outdoor_seating", "live_music", "serves_breakfast", "serves_lunch",
    "serves_dinner", "serves_brunch", "serves_vegetarian_food",
    "wheelchair_accessible_entrance", "wheelchair_accessible_seating",
)


def _money(value):
    if not isinstance(value, dict) or not value:
        return None
    try:
        amount = Decimal(str(value.get("units", 0))) + Decimal(str(value.get("nanos", 0))) / Decimal(10**9)
        return float(amount) if amount.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def public_place_facts(place, *, district_name="", city_name=""):
    projection = place.get("map_projection") or {}
    details = projection.get("google_place_details") or {}
    raw_range = details.get("price_range") or {}
    start, end = raw_range.get("startPrice") or {}, raw_range.get("endPrice") or {}
    currencies = {v.get("currencyCode") for v in (start, end) if v.get("currencyCode")}
    price_range = None
    if len(currencies) == 1:
        currency = next(iter(currencies))
        price_range = {
            "currency": currency,
            "minimum": _money(start) if start.get("currencyCode") == currency else None,
            "maximum": _money(end) if end.get("currencyCode") == currency else None,
        }
    return {
        "district_name": place.get("district_name") or district_name,
        "city_name": city_name,
        "rating": projection.get("rating"),
        "user_rating_count": projection.get("user_rating_count"),
        "price_level": details.get("price_level"),
        "price_range": price_range,
        "opening_hours": deepcopy(effective_opening_hours(place)),
        "service_attributes": {key: details.get(key) if isinstance(details.get(key), bool) else None
                               for key in SERVICE_ATTRIBUTES},
    }
