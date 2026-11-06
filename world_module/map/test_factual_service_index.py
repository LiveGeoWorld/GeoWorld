from world_module.map._internal.factual_service_index import (
    build_factual_service_index,
    build_factual_service_index_artifact,
    read_factual_service_index,
)
from shared.io import write_json


def test_google_types_materialize_factual_service_categories_without_state() -> None:
    services = build_factual_service_index([
        {"place_id": "restaurant", "primary_type": "italian_restaurant", "map_projection": {"types": ["italian_restaurant", "restaurant", "food"]}},
        {"place_id": "university", "primary_type": "university", "map_projection": {"types": ["university", "point_of_interest"]}},
        {"place_id": "park", "primary_type": "park", "map_projection": {"types": ["park"]}},
    ])

    assert [(item["place_id"], item["service_type"]) for item in services] == [
        ("restaurant", "restaurant"), ("university", "university"), ("park", "park"),
    ]
    assert all(item["state_model"] == {} for item in services)
    assert all(not item["event_hooks"] for item in services)


def test_historical_service_ids_are_preserved_when_reindexing_a_world() -> None:
    services = build_factual_service_index(
        [{"place_id": "poi", "primary_type": "restaurant", "map_projection": {"types": ["restaurant"]}}],
        [{"service_id": "historical_service", "place_id": "poi", "service_type": "restaurant_reservation", "state_model": {"queue_time_min": 50}}],
    )

    assert services[0]["service_id"] == "historical_service"
    assert services[0]["service_type"] == "restaurant"
    assert services[0]["state_model"] == {}


def test_distinct_leisure_types_and_unknown_google_types_are_not_collapsed() -> None:
    services = build_factual_service_index([
        {"place_id": "museum", "primary_type": "museum", "map_projection": {"types": ["museum", "tourist_attraction"]}},
        {"place_id": "theater", "primary_type": "movie_theater", "map_projection": {"types": ["movie_theater"]}},
        {"place_id": "bank", "primary_type": "bank", "map_projection": {"types": ["bank", "point_of_interest"]}},
    ])

    assert [(item["place_id"], item["service_type"]) for item in services] == [
        ("museum", "museum"), ("theater", "movie_theater"), ("bank", "other"),
    ]


def test_persisted_index_is_used_only_for_its_exact_poi_type_signature(tmp_path) -> None:
    world = {
        "cities": [{"city_id": "city_a"}],
        "places": [{"place_id": "p1", "primary_type": "restaurant", "map_projection": {"types": ["restaurant"]}}],
        "services": [],
    }
    path = tmp_path / "factual_service_index.json"
    write_json(path, build_factual_service_index_artifact(world))

    indexed = read_factual_service_index(path, world)
    assert indexed is not None
    assert indexed[0]["service_type"] == "restaurant"

    changed = {**world, "places": [{"place_id": "p1", "primary_type": "museum", "map_projection": {"types": ["museum"]}}]}
    assert read_factual_service_index(path, changed) is None
