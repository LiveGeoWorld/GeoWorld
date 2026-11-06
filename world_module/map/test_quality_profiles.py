"""Regression tests for explicit map build quality scopes."""

from __future__ import annotations

from world_module.map._internal.quality_gate import check_worldgen_quality
from world_module.map.generator import MapGenerator
from world_module.map.seed import CitySeed


def _place(index: int) -> dict:
    return {
        "place_id": f"place_{index}",
        "location": {"lat": 1.0 + index / 1000, "lng": 2.0},
        "map_projection": {"google_place_id": f"google_{index}"},
    }


def _world(*, districts: int, corridors: int, places: int) -> dict:
    return {
        "districts": [{"district_id": f"district_{index}"} for index in range(districts)],
        "corridors": [{"corridor_id": f"corridor_{index}", "geometry": {"data": {"points": []}}}
                      for index in range(corridors)],
        "places": [_place(index) for index in range(places)],
        "services": [], "information_items": [], "event_templates": [], "actors": [],
    }


def test_expansion_batch_accepts_exact_small_delta_without_complete_city_floor() -> None:
    report = check_worldgen_quality(
        _world(districts=2, corridors=4, places=24),
        profile="expansion_batch",
        expected_districts=2,
        expected_corridors=4,
        expected_places=24,
    )

    assert report["ok"] is True
    assert report["profile"] == "expansion_batch"
    assert report["expected_counts"] == {"districts": 2, "corridors": 4, "places": 24}


def test_poi_supplement_ignores_temporary_structure_counts() -> None:
    report = check_worldgen_quality(
        _world(districts=2, corridors=4, places=3),
        profile="poi_supplement",
        expected_districts=None,
        expected_corridors=None,
        expected_places=3,
    )

    assert report["ok"] is True
    assert report["expected_counts"] == {"districts": None, "corridors": None, "places": 3}


def test_generator_uses_expansion_delta_contract() -> None:
    seed = CitySeed(
        city_id="test_city", city_name="Test City", country="Testland",
        center={"lat": 1.0, "lng": 2.0}, timezone="UTC",
        district_count=2, corridor_count=4, target_place_count=24,
        quality_profile="expansion_batch",
    )

    report = MapGenerator().validate(seed, _world(districts=2, corridors=4, places=24))

    assert report["ok"] is True
    assert report["profile"] == "expansion_batch"
