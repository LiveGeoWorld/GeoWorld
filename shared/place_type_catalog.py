"""One catalog for World Studio place selection and factual POI categories.

The user-facing category controls construction scope.  ``google_query_types``
are only the concrete Google Places filters needed to obtain that scope; the
returned ``primary_type`` and ``types`` remain untouched on every POI.
"""

from __future__ import annotations

from typing import Any, Iterable


PLACE_TYPE_CATALOG: tuple[dict[str, Any], ...] = (
    {"id": "restaurant", "label": "餐厅", "google_query_types": ("restaurant",), "aliases": ("restaurant", "fast_food_restaurant", "meal_takeaway", "meal_delivery"), "suffixes": ("_restaurant",), "default_selected": True},
    {"id": "cafe", "label": "咖啡", "google_query_types": ("cafe", "coffee_shop"), "aliases": ("cafe", "coffee_shop", "tea_house"), "default_selected": True},
    {"id": "bar", "label": "酒吧", "google_query_types": ("bar",), "aliases": ("bar", "pub", "lounge_bar", "night_club"), "default_selected": False},
    {"id": "shopping", "label": "购物", "google_query_types": ("shopping_mall", "department_store", "store"), "aliases": ("shopping_mall", "department_store", "store", "book_store", "clothing_store", "gift_shop", "home_goods_store", "convenience_store", "food_store"), "default_selected": True},
    {"id": "lodging", "label": "住宿", "google_query_types": ("hotel", "lodging"), "aliases": ("hotel", "lodging", "resort_hotel", "motel", "bed_and_breakfast"), "default_selected": True},
    {"id": "tourist_attraction", "label": "景点", "google_query_types": ("tourist_attraction",), "aliases": ("tourist_attraction", "historical_place", "historical_landmark", "landmark", "visitor_center", "art_gallery"), "default_selected": True},
    {"id": "museum", "label": "博物馆", "google_query_types": ("museum",), "aliases": ("museum", "art_museum", "history_museum", "science_museum"), "default_selected": True},
    {"id": "movie_theater", "label": "影院", "google_query_types": ("movie_theater",), "aliases": ("movie_theater",), "default_selected": False},
    {"id": "park", "label": "公园", "google_query_types": ("park",), "aliases": ("park", "botanical_garden", "zoo", "aquarium"), "default_selected": True},
    {"id": "transit_station", "label": "交通站", "google_query_types": ("train_station", "subway_station", "bus_station"), "aliases": ("train_station", "subway_station", "bus_station", "bus_stop", "transit_station", "airport", "ferry_terminal"), "default_selected": True},
    {"id": "hospital", "label": "医院", "google_query_types": ("hospital",), "aliases": ("hospital", "general_hospital"), "default_selected": False},
    {"id": "medical_care", "label": "诊疗", "google_query_types": ("medical_clinic", "doctor"), "aliases": ("medical_clinic", "doctor", "dental_clinic", "health"), "default_selected": False},
    {"id": "pharmacy", "label": "药店", "google_query_types": ("pharmacy",), "aliases": ("pharmacy",), "default_selected": False},
    {"id": "university", "label": "大学", "google_query_types": ("university",), "aliases": ("university",), "default_selected": False},
)

OTHER_CATEGORY = "other"


def place_type_catalog_payload() -> dict[str, Any]:
    """Return the browser-safe catalog without classification implementation."""
    return {
        "version": "geoword.place_type_catalog.v1",
        "categories": [
            {
                "id": item["id"],
                "label": item["label"],
                "google_query_types": list(item["google_query_types"]),
                "default_selected": item["default_selected"],
            }
            for item in PLACE_TYPE_CATALOG
        ],
    }


def query_types_for_categories(category_ids: Iterable[str]) -> list[str]:
    selected = {str(value).strip() for value in category_ids if str(value).strip()}
    result: list[str] = []
    for item in PLACE_TYPE_CATALOG:
        if item["id"] in selected:
            result.extend(item["google_query_types"])
    return list(dict.fromkeys(result))


def categories_for_query_types(query_types: Iterable[str]) -> list[str]:
    requested = {_normalize(value) for value in query_types}
    result = [
        item["id"] for item in PLACE_TYPE_CATALOG
        if requested.intersection(item["google_query_types"])
    ]
    return result or default_category_ids()


def default_category_ids() -> list[str]:
    return [item["id"] for item in PLACE_TYPE_CATALOG if item["default_selected"]]


def category_for_google_types(types: Iterable[str]) -> str:
    """Resolve factual Google types to one user-visible category or ``other``."""
    categories = categories_for_google_place_types(types)
    return categories[0] if categories else OTHER_CATEGORY


def categories_for_google_place_types(types: Iterable[str]) -> list[str]:
    """Resolve every factual category represented by a Google type set."""
    actual = {_normalize(value) for value in types}
    actual.discard("")
    # Google commonly attaches the generic ``tourist_attraction`` marker to
    # museums and parks.  Resolve the more specific factual category first
    # without changing the user-facing order of World Studio chips.
    priority = {"museum": 0, "movie_theater": 1, "park": 2, "tourist_attraction": 3}
    ordered_catalog = sorted(
        PLACE_TYPE_CATALOG,
        key=lambda item: priority.get(str(item["id"]), 10),
    )
    result: list[str] = []
    for item in ordered_catalog:
        if actual.intersection(item["aliases"]):
            result.append(str(item["id"]))
        elif any(value.endswith(suffix) for value in actual for suffix in item.get("suffixes", ())):
            result.append(str(item["id"]))
    return result


def category_matches(requested: str, actual: str) -> bool:
    """Match current categories while preserving legacy task intent aliases."""
    requested = _legacy_category(_normalize(requested))
    actual = _legacy_category(_normalize(actual))
    if requested == "attraction":
        return actual in {"attraction", "tourist_attraction", "museum", "movie_theater", "park"}
    if requested == "healthcare":
        return actual in {"hospital", "medical_care", "pharmacy"}
    if requested == "transport":
        return actual == "transit_station"
    return bool(requested and actual and requested == actual)


def _legacy_category(value: str) -> str:
    return {
        "restaurant_reservation": "restaurant",
        "table_booking": "restaurant",
        "cafe_reservation": "cafe",
        "hotel_booking": "lodging",
        "hotel": "lodging",
        "attraction_visit": "attraction",
        "outdoor_visit": "park",
        "retail_visit": "shopping",
        "healthcare_visit": "medical_care",
        "local_visit": "cafe",
    }.get(value, value)


def _normalize(value: object) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
