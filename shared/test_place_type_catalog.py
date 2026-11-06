from shared.place_type_catalog import (
    categories_for_query_types,
    category_for_google_types,
    category_matches,
    default_category_ids,
    query_types_for_categories,
)


def test_merged_frontend_categories_expand_only_to_their_declared_google_queries() -> None:
    assert query_types_for_categories(["cafe", "lodging", "medical_care"]) == [
        "cafe", "coffee_shop", "hotel", "lodging", "medical_clinic", "doctor",
    ]


def test_google_precise_types_preserve_distinct_catalog_categories() -> None:
    assert category_for_google_types(["italian_restaurant", "restaurant"]) == "restaurant"
    assert category_for_google_types(["museum", "tourist_attraction"]) == "museum"
    assert category_for_google_types(["movie_theater"]) == "movie_theater"
    assert category_for_google_types(["bank"]) == "other"


def test_legacy_seed_and_task_labels_remain_compatible() -> None:
    assert {"cafe", "shopping", "lodging"}.issubset(
        categories_for_query_types(["cafe", "coffee_shop", "shopping_mall", "hotel"]),
    )
    assert category_matches("attraction", "museum") is True
    assert category_matches("attraction_visit", "museum") is True
    assert category_matches("hotel_booking", "lodging") is True
    assert default_category_ids()
