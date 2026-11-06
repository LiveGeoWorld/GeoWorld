from __future__ import annotations

from world_module.map._internal import substrate_builder
from world_module.map._internal.google_geo_enricher import GoogleGeoEnricher
from world_module.map.seed import CitySeed
from vis.renderer import place_detail


def _seed() -> CitySeed:
    return CitySeed(
        city_id="test_city",
        city_name="Test City",
        country="Testland",
        center={"lat": 40.0, "lng": -73.0},
        bbox={"south": 39.9, "north": 40.1, "west": -73.1, "east": -72.9},
        timezone="UTC",
        google_reviews_per_place=1,
    )


def test_google_details_cover_every_retained_place_and_preserve_exact_type(monkeypatch, example_pipeline_config) -> None:
    enricher = GoogleGeoEnricher(config=example_pipeline_config)

    def fake_details(place_id: str, *, fields: list[str], language: str) -> dict:
        assert "reservable" in fields
        assert "priceLevel" in fields
        return {
            "id": place_id,
            "primaryType": "university" if place_id == "hunter" else "italian_restaurant",
            "types": ["university", "educational_institution", "point_of_interest"],
            "websiteUri": "https://example.edu/",
            "nationalPhoneNumber": "+1 212 555 0100",
            "regularOpeningHours": {"weekdayDescriptions": ["Monday: 08:00-17:00"]},
            "priceLevel": "PRICE_LEVEL_MODERATE",
            "priceRange": {
                "startPrice": {"currencyCode": "USD", "units": "20"},
                "endPrice": {"currencyCode": "USD", "units": "30"},
            },
            "reservable": False,
            "reviews": [],
        }

    monkeypatch.setattr(enricher.maps, "place_details", fake_details)
    places = [
        {"google_place_id": "hunter", "primary_type": "university", "types": ["university"]},
        {"google_place_id": "restaurant", "primary_type": "italian_restaurant", "types": ["italian_restaurant"]},
    ]

    enriched = enricher.attach_place_details(_seed(), places)

    assert [place["google_place_details"]["details_status"] for place in enriched] == ["queried", "queried"]
    assert enriched[0]["primary_type"] == "university"
    assert enriched[1]["primary_type"] == "italian_restaurant"
    assert enriched[0]["google_place_details"]["reservable"] is False
    assert enriched[0]["google_place_details"]["price_level"] == "PRICE_LEVEL_MODERATE"
    assert enriched[0]["google_place_details"]["price_range"]["endPrice"]["units"] == "30"
    assert enricher.place_details_report()["expected_place_ids"] == 2
    assert enricher.place_details_report()["status_counts"] == {"queried": 2}


def test_base_world_projects_factual_capabilities_without_dynamic_state(monkeypatch) -> None:
    monkeypatch.setattr(substrate_builder, "add_live_overlay", lambda world, *_args, **_kwargs: world)
    world = substrate_builder.build_world(
        _seed(),
        {
            "geo_provider": "google",
            "districts": [{"name": "Center", "center": {"lat": 40.0, "lng": -73.0}}],
            "corridors": [],
            "places": [{
                "name": "Hunter College",
                "google_place_id": "hunter",
                "primary_type": "university",
                "types": ["university", "educational_institution"],
                "location": {"lat": 40.0, "lng": -73.0},
            }],
        },
    )

    assert len(world["services"]) == 1
    service = world["services"][0]
    assert service["place_id"] == world["places"][0]["place_id"]
    assert service["app_projection"]["google_primary_type"] == "university"
    assert service["app_projection"]["kind"] == "factual_place_capability"
    for field in ("state_model", "affordances", "oracle_affordances", "event_hooks"):
        assert not service[field]
    assert world["places"][0]["primary_type"] == "university"


def test_place_detail_returns_factual_google_fields_without_service_object() -> None:
    place = {
        "place_id": "place_hunter",
        "name": "Hunter College",
        "primary_type": "university",
        "location": {"lat": 40.7678398, "lng": -73.9645291},
        "address": "695 Park Ave, New York, NY 10065, USA",
        "map_projection": {
            "rating": 3.9,
            "user_rating_count": 666,
            "business_status": "OPERATIONAL",
            "types": ["university", "educational_institution"],
            "google_place_details": {
                "details_status": "queried",
                "website_uri": "https://example.edu/",
                "national_phone_number": "+1 212 555 0100",
                "current_opening_hours": {"openNow": True},
                "regular_opening_hours": {"weekdayDescriptions": ["Monday: 08:00-17:00"]},
                "field_status": {
                    "website_uri": "provided",
                    "national_phone_number": "provided",
                    "current_opening_hours": "provided",
                    "regular_opening_hours": "provided",
                },
            },
        },
    }

    detail = place_detail(place, [], [])

    assert detail["primary_type"] == "university"
    assert detail["category"] == "education"
    assert detail["service"] is None
    assert detail["location"] == {"lat": 40.7678398, "lng": -73.9645291}
    assert detail["map_projection"]["rating"] == 3.9
    assert detail["map_projection"]["user_rating_count"] == 666
    assert detail["map_projection"]["business_status"] == "OPERATIONAL"
    assert detail["map_projection"]["google_place_details"]["details_status"] == "queried"
    assert detail["map_projection"]["google_place_details"]["national_phone_number"] == "+1 212 555 0100"
    assert detail["map_projection"]["google_place_details"]["website_uri"] == "https://example.edu/"
