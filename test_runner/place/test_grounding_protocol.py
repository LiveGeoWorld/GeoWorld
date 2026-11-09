from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

from app.runtime.map_app import MapAppRuntime
from shared.io import write_json
from app.runtime.environment import Environment
from test_runner.place.session import _load_mapapp_world, _normalize_legacy_grounding_judge
from test_runner.place.sandbox_adapter import PlaceGroundingSandbox


class _FakeMapApp:
    city_names = {"london": "London", "cape_town": "Cape Town"}

    def search_grounding_candidates(self, args):
        if args.get("city_id") == "cape_town" or args.get("keyword") == "missing":
            return {"candidates": []}
        return {"candidates": [{
            "candidate_id": "internal-london-poi",
            "name": "London POI",
            "address": "1 London Road",
            "city_id": "london",
            "city_name": "London",
        }]}

    def select_grounded_location(self, args):
        return {"selected": {"candidate_id": args["candidate_id"], "name": "London POI"}}


def _sandbox():
    env = Environment()
    app = PlaceGroundingSandbox(SimpleNamespace(map_app=_FakeMapApp()))
    env.register_app(app)
    env.configure_public_tools({
        "MapApp": ["search_city", "search_places", "get_place_detail", "select_grounded_location"],
        "UserInterfaceApp": ["final_answer"],
    })
    return env, app


def test_city_then_place_uses_opaque_handles_and_finishes_after_submission():
    env, app = _sandbox()
    city = env.execute_tool("MapApp__search_city", {"city_name": "London"})
    handle = city["cities"][0]["city_handle"]
    assert handle.startswith("city_ref_")
    assert "london" not in city["cities"][0]
    assert [item["name"] for item in env.get_tool_schemas()] == [
        "MapApp__search_city", "MapApp__search_places", "MapApp__get_place_detail",
        "MapApp__select_grounded_location", "UserInterfaceApp__final_answer",
    ]

    places = env.execute_tool("MapApp__search_places", {
        "keyword": "poi", "city_handle": handle,
    })
    candidate_handle = places["candidates"][0]["candidate_handle"]
    assert "internal-london-poi" not in str(places)
    env.execute_tool("MapApp__select_grounded_location", {"candidate_handle": candidate_handle})
    env.execute_tool("UserInterfaceApp__final_answer", {"content": "London POI"})

    assert env.terminal_state == {"reason": "grounding_submitted"}
    assert app.events[-1]["agent_action"] == "UserInterfaceApp.send_message_to_user"
    assert app.events[-2]["args"]["candidate_id"] == "internal-london-poi"


def test_place_search_schema_requires_exact_names_and_separates_query_roles():
    env, _ = _sandbox()
    schema = next(
        item for item in env.get_tool_schemas()
        if item["name"] == "MapApp__search_places"
    )["parameters"]

    city_schema = next(
        item for item in env.get_tool_schemas()
        if item["name"] == "MapApp__search_city"
    )
    assert "exact English city name" in city_schema["description"]
    assert "city_name='Shanghai', not '上海'" in city_schema["description"]
    assert "English city name" in city_schema["parameters"]["properties"]["city_name"]["description"]

    assert schema["additionalProperties"] is False
    assert schema["required"] == ["keyword"]
    assert set(schema["properties"]) == {
        "keyword", "city_handle", "near", "near_candidate_ref", "near_radius_m", "category",
    }
    assert "One concise public place name or English category" in schema["properties"]["keyword"]["description"]
    assert "exact original-language" in schema["properties"]["keyword"]["description"]
    assert "上海新世界城" in schema["properties"]["keyword"]["description"]
    assert "preferably copied from MapApp" in schema["properties"]["near"]["description"]
    assert "English place category" in schema["properties"]["category"]["description"]
    assert "Prefer the exact original-language public name returned by MapApp" in schema["properties"]["keyword"]["description"]
    assert "Do not append city names, street names" in schema["properties"]["keyword"]["description"]
    assert "V&A Waterfront shopping mall Cape Town" in schema["properties"]["keyword"]["description"]
    assert "MapApp__search_city" in schema["properties"]["city_handle"]["description"]
    assert "MapApp__search_places" in schema["properties"]["near_candidate_ref"]["description"]

    select_schema = next(
        item for item in env.get_tool_schemas()
        if item["name"] == "MapApp__select_grounded_location"
    )
    submit_schema = next(
        item for item in env.get_tool_schemas()
        if item["name"] == "UserInterfaceApp__final_answer"
    )
    assert "REQUIRED SUBMISSION CHECKPOINT" in select_schema["description"]
    assert "does not count as selecting it" in select_schema["description"]
    assert "FINAL STEP ONLY" in submit_schema["description"]
    assert "merely writing the correct place name" in submit_schema["description"]
    assert "invalid_grounding_stage" in submit_schema["description"]


def test_empty_city_or_place_lookup_ends_episode_without_a_retry_tool():
    env, _ = _sandbox()
    result = env.execute_tool("MapApp__search_city", {"city_name": "Paris"})
    assert result["status"] == "terminal"
    assert env.terminal_state["reason"] == "city_lookup_empty"
    assert len(env.get_tool_schemas()) == 5

    env, _ = _sandbox()
    result = env.execute_tool("MapApp__search_places", {"keyword": "missing"})
    assert result["status"] == "terminal"
    assert env.terminal_state["reason"] == "place_lookup_empty"
    assert len(env.get_tool_schemas()) == 5


def test_existing_wrong_city_then_empty_place_lookup_ends_episode():
    env, _ = _sandbox()
    city = env.execute_tool("MapApp__search_city", {"city_name": "Cape Town"})
    result = env.execute_tool("MapApp__search_places", {
        "keyword": "poi", "city_handle": city["cities"][0]["city_handle"],
    })
    assert result["status"] == "terminal"
    assert env.terminal_state["reason"] == "place_lookup_empty"


def test_grounding_search_does_not_broaden_a_nonmatching_query_to_arbitrary_candidates():
    map_app = MapAppRuntime(
        places=[{
            "place_id": "place-1", "name": "Known Cafe", "city_id": "london",
            "district_id": "central", "primary_type": "cafe", "address": "1 Road",
            "location": {"lat": 51.5, "lng": -0.1}, "map_projection": {"types": ["cafe"]},
        }],
        cities=[{"city_id": "london", "name": "London"}],
    )
    assert map_app.search_grounding_candidates({"keyword": "nonmatching-term"})["candidates"] == []


def test_grounding_search_treats_google_type_underscores_as_natural_language_spaces():
    map_app = MapAppRuntime(
        places=[{
            "place_id": "cinema", "name": "Southbank Cinema", "city_id": "london",
            "district_id": "southbank", "primary_type": "movie_theater",
            "address": "Cinema Road", "location": {"lat": 51.5, "lng": -0.1},
            "map_projection": {"types": ["movie_theater"]},
        }],
        cities=[{"city_id": "london", "name": "London"}],
    )
    assert [item["name"] for item in map_app.search_grounding_candidates({
        "keyword": "movie theater",
    })["candidates"]] == ["Southbank Cinema"]


def test_grounding_search_uses_explicit_frozen_alias_without_translating_names():
    map_app = MapAppRuntime(
        places=[{
            "place_id": "new-world", "name": "上海新世界城", "aliases": ["New World City"],
            "city_id": "shanghai", "district_id": "huangpu", "primary_type": "shopping_mall",
            "address": "南京西路2号", "location": {"lat": 31.234, "lng": 121.475},
            "map_projection": {"types": ["shopping_mall"]},
        }],
        cities=[{"city_id": "shanghai", "name": "Shanghai"}],
    )

    response = map_app.search_grounding_candidates({"keyword": "New World City", "city_id": "shanghai"})

    assert [item["name"] for item in response["candidates"]] == ["上海新世界城"]
    assert response["candidates"][0]["match"]["method"] == "lexical"


def test_version_bound_alias_cache_is_overlaid_on_the_matching_place(tmp_path: Path):
    world_path = tmp_path / "world.json"
    alias_path = tmp_path / "place_alias_cache.json"
    write_json(world_path, {
        "cities": [{"city_id": "shanghai", "name": "Shanghai"}],
        "places": [{
            "place_id": "new-world", "name": "上海新世界城", "city_id": "shanghai",
            "district_id": "huangpu", "primary_type": "shopping_mall", "address": "南京西路2号",
            "location": {"lat": 31.234, "lng": 121.475},
            "map_projection": {"types": ["shopping_mall"]},
        }],
        "services": [],
        "districts": [],
    })
    write_json(alias_path, {"aliases": [{
        "place_id": "new-world", "status": "available",
        "english_display_name": "New World City",
    }]})

    world = _load_mapapp_world({
        "world_paths": [str(world_path)],
        "place_alias_cache_paths": [str(alias_path)],
    })
    map_app = MapAppRuntime(places=world["places"], cities=world["cities"])

    response = map_app.search_grounding_candidates({"keyword": "New World City"})

    assert [item["name"] for item in response["candidates"]] == ["上海新世界城"]


def test_grounding_search_falls_back_to_bounded_name_similarity_for_a_typo():
    map_app = MapAppRuntime(
        places=[{
            "place_id": "trenchtown", "name": "Trenchtown", "city_id": "cape_town",
            "district_id": "observatory", "primary_type": "restaurant", "address": "Lower Main Road",
            "location": {"lat": -33.94, "lng": 18.47}, "map_projection": {"types": ["restaurant"]},
        }],
        cities=[{"city_id": "cape_town", "name": "Cape Town"}],
    )

    response = map_app.search_grounding_candidates({"keyword": "Trenchtwn", "city_id": "cape_town"})

    assert [item["name"] for item in response["candidates"]] == ["Trenchtown"]
    assert response["candidates"][0]["match"]["method"] == "fuzzy"


def test_grounding_search_normalizes_fullwidth_place_name_characters():
    map_app = MapAppRuntime(
        places=[{
            "place_id": "mall", "name": "IAPM Mall", "city_id": "shanghai",
            "district_id": "xuhui", "primary_type": "shopping_mall", "address": "999 Huaihai Road",
            "location": {"lat": 31.22, "lng": 121.46}, "map_projection": {"types": ["shopping_mall"]},
        }],
        cities=[{"city_id": "shanghai", "name": "Shanghai"}],
    )

    response = map_app.search_grounding_candidates({"keyword": "ＩＡＰＭ Ｍａｌｌ"})

    assert [item["name"] for item in response["candidates"]] == ["IAPM Mall"]
    assert response["candidates"][0]["match"]["method"] == "lexical"


def test_grounding_search_treats_directional_entrance_suffix_as_the_same_place_core():
    map_app = MapAppRuntime(
        places=[{
            "place_id": "new-world-north", "name": "新世界城北门", "city_id": "shanghai",
            "district_id": "huangpu", "primary_type": "shopping_mall", "address": "凤阳路99号",
            "location": {"lat": 31.235, "lng": 121.47}, "map_projection": {"types": ["shopping_mall"]},
        }],
        cities=[{"city_id": "shanghai", "name": "Shanghai"}],
    )

    response = map_app.search_grounding_candidates({"keyword": "上海新世界城", "city_id": "shanghai"})

    assert [item["name"] for item in response["candidates"]] == ["新世界城北门"]
    assert response["candidates"][0]["match"]["method"] == "fuzzy"


def test_grounding_fuzzy_fallback_does_not_treat_address_or_category_as_a_name_alias():
    map_app = MapAppRuntime(
        places=[{
            "place_id": "cafe", "name": "Known Cafe", "city_id": "london",
            "district_id": "central", "primary_type": "restaurant", "address": "Trenchtown Road",
            "location": {"lat": 51.5, "lng": -0.1}, "map_projection": {"types": ["restaurant"]},
        }],
        cities=[{"city_id": "london", "name": "London"}],
    )

    assert map_app.search_grounding_candidates({"keyword": "Trenchtwn Road"})["candidates"] == []


def test_near_candidate_ref_resolves_a_returned_candidate_without_exposing_its_coordinates():
    map_app = MapAppRuntime(
        places=[
            {
                "place_id": "brasserie", "name": "Brasserie Blanc - Southbank",
                "city_id": "london", "district_id": "southbank",
                "primary_type": "restaurant", "address": "Southbank Road",
                "location": {"lat": 51.5079, "lng": -0.1164},
                "map_projection": {"types": ["restaurant", "food"]},
            },
            {
                "place_id": "cinema", "name": "Southbank Cinema",
                "city_id": "london", "district_id": "southbank",
                "primary_type": "movie_theater", "address": "Cinema Road",
                "location": {"lat": 51.5081, "lng": -0.1167},
                "map_projection": {"types": ["movie_theater", "point_of_interest"]},
            },
        ],
        cities=[{"city_id": "london", "name": "London"}],
    )
    env = Environment()
    app = PlaceGroundingSandbox(SimpleNamespace(map_app=map_app))
    env.register_app(app)
    env.configure_public_tools({"MapApp": ["search_places"]})

    anchor = env.execute_tool("MapApp__search_places", {"keyword": "Brasserie Blanc"})
    ref = anchor["candidates"][0]["candidate_handle"]
    assert "lat" not in str(anchor)
    assert anchor["candidates"][0]["type"] == "restaurant"
    nearby = env.execute_tool("MapApp__search_places", {
        "keyword": "movie theater", "near_candidate_ref": ref, "near_radius_m": 500,
    })

    assert nearby["candidates"][0]["name"] == "Southbank Cinema"
    assert nearby["candidates"][0]["type"] == "movie_theater"
    assert "brasserie" not in str(nearby)
    assert app.events[-1]["args"]["near_candidate_id"] == "brasserie"


def test_near_anchor_does_not_return_another_city_when_city_is_not_explicitly_given():
    map_app = MapAppRuntime(
        places=[
            {
                "place_id": "anchor", "name": "London Anchor", "city_id": "london",
                "district_id": "central", "primary_type": "restaurant", "address": "1 London Road",
                "location": {"lat": 51.5, "lng": -0.1},
                "map_projection": {"types": ["restaurant"]},
            },
            {
                "place_id": "london-cinema", "name": "London Cinema", "city_id": "london",
                "district_id": "central", "primary_type": "movie_theater", "address": "2 London Road",
                "location": {"lat": 51.5002, "lng": -0.1001},
                "map_projection": {"types": ["movie_theater"]},
            },
            {
                "place_id": "ny-cinema", "name": "New York Cinema", "city_id": "new_york",
                "district_id": "midtown", "primary_type": "movie_theater", "address": "3 New York Road",
                "location": {"lat": 40.7, "lng": -74.0},
                "map_projection": {"types": ["movie_theater"]},
            },
        ],
        cities=[
            {"city_id": "london", "name": "London"},
            {"city_id": "new_york", "name": "New York"},
        ],
    )
    response = map_app.search_grounding_candidates({
        "keyword": "movie theater", "near": "London Anchor",
    })
    assert [item["name"] for item in response["candidates"]] == ["London Cinema"]


def test_unknown_near_candidate_ref_ends_without_a_recovery_hint():
    env, _ = _sandbox()
    result = env.execute_tool("MapApp__search_places", {
        "keyword": "poi", "near_candidate_ref": "place_ref_404",
    })
    assert result["status"] == "terminal"
    assert env.terminal_state["reason"] == "candidate_not_returned_by_lookup"


def test_exact_poi_session_ignores_legacy_radius_check():
    task = {
        "task_type": "place_grounding",
        "summary": {"acceptable_granularity": "exact_poi"},
    }
    judge = {
        "stages": [{"nodes": [{
            "hard_checks": [{"check_type": "field_equals"}],
            "soft_checks": [{"check_type": "geo_within_radius"}],
        }]}]
    }

    normalized = _normalize_legacy_grounding_judge(task, judge)

    assert normalized["stages"][0]["nodes"][0]["hard_checks"] == [{"check_type": "field_equals"}]
    assert normalized["stages"][0]["nodes"][0]["soft_checks"] == []
    assert judge["stages"][0]["nodes"][0]["soft_checks"] == [{"check_type": "geo_within_radius"}]


def test_frozen_anchor_lookup_and_saved_route_share_the_same_location(tmp_path):
    from types import SimpleNamespace
    from shared.map_place_index import map_index_places
    from test_runner.place.runtime import AppRuntime
    from test_runner.place.sandbox_adapter import MapAppSandbox, discovery_route_evidence

    origin = {"lat": 40.713208, "lng": -73.987787}
    destination = {"lat": 40.75, "lng": -73.95}
    diner = {"place_id": "diner", "city_id": "ny", "name": "Diner", "location": destination,
             "map_projection": {"google_place_id": "diner-source"}}
    anchor = {"google_place_id": "clinic-source", "name": "New York Gouverneur",
              "location": origin, "address": "227 Madison St"}
    world = {
        "cities": [{"city_id": "ny", "name": "New York"}],
        "places": [diner],
        "districts": [{"city_id": "ny", "director_notes": {"anchor_places": [
            anchor, anchor, {**anchor, "google_place_id": "diner-source"},
        ]}}],
    }
    row = {"origin_place_id": "character", "origin_name": anchor["name"],
           "origin_lat": origin["lat"], "origin_lng": origin["lng"], "dest_place_id": "diner",
           "mode": "DRIVE", "route": {"duration_s": 1033, "distance_m": 8588}}
    session = SimpleNamespace(
        world=world, world_slice={}, curated_source={"example": {
            "start": {"city_id": "ny", "source_place_id": "clinic-source", "coordinate": origin},
            "evidence_ledger": {"construction_contract": {"route_evidence": [row]}},
        }},
    )
    assert len(map_index_places(world)) == 2
    assert world["places"] == [diner]
    search = AppRuntime(session).map_app.search_places({"keyword": anchor["name"], "limit": 1})
    place = search["places"][0]
    assert (place["name"], place["location"], place["address"]) == (anchor["name"], origin, anchor["address"])
    path = tmp_path / "world.json"
    write_json(path, world)
    route_map = MapAppSandbox(world_paths=[str(path)], route_cache_data={"cache": [row]})
    route_map._load_cache_data(discovery_route_evidence(session))
    result = route_map.query_routes(
        origin={"name": place["name"], "location": origin},
        destination={"name": "Diner", "location": destination}, mode="DRIVE",
    )
    assert {r["duration_s"] for r in result["routes"]} == {1033}
