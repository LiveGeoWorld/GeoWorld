"""Regression coverage for append-only city expansion assembly."""

from __future__ import annotations

import pytest

from vis.server import (
    _candidate_gate_report,
    _candidate_target_gap,
    _candidate_city_id,
    _merge_characters,
    _merge_city_world,
    _plan_expansion_targets,
    _plan_new_city_targets,
)


def test_city_expansion_keeps_existing_entities_and_namespaces_new_ids() -> None:
    existing = {
        "cities": [{"city_id": "city_a", "name": "City A"}],
        "districts": [{"district_id": "district_1"}],
        "corridors": [],
        "places": [{"place_id": "place_1", "district_id": "district_1"}],
        "services": [],
        "information_items": [],
        "event_templates": [],
        "actors": [],
    }
    addition = {
        "cities": [{"city_id": "city_a", "name": "City A"}],
        "districts": [{"district_id": "district_1"}],
        "corridors": [],
        "places": [{"place_id": "place_1", "district_id": "district_1"}],
        "services": [],
        "information_items": [],
        "event_templates": [],
        "actors": [],
    }

    merged = _merge_city_world(existing, addition, "exp_001", {"city_id": "city_a"})

    assert [item["district_id"] for item in merged["districts"]] == ["district_1", "exp_001__district_1"]
    assert [item["place_id"] for item in merged["places"]] == ["place_1", "exp_001__place_1"]
    assert merged["places"][1]["district_id"] == "exp_001__district_1"
    assert merged["places"][1]["introduced_version"] == 2
    assert merged["places"][1]["source_batch_id"] == "exp_001"
    assert merged["generation_metadata"]["expansion_history"][0]["added_counts"]["places"] == 1


def test_city_expansion_keeps_existing_characters_and_relationships() -> None:
    existing = {
        "characters": [{"character_id": "char_1", "name": "Existing"}],
        "relationships": [],
        "relationship_graph": {"nodes": [{"id": "char_1"}], "edges": []},
    }
    addition = {
        "characters": [{"character_id": "char_2", "name": "New"}],
        "relationships": [{"character_id": "char_2", "related_id": "char_2"}],
        "relationship_graph": {"nodes": [{"id": "char_2"}], "edges": [{"from_id": "char_2", "to_id": "char_2"}]},
    }

    merged = _merge_characters(existing, addition, "exp_001")

    assert [item["character_id"] for item in merged["characters"]] == ["char_1", "exp_001__char_2"]
    assert merged["characters"][1]["introduced_version"] == 2
    assert merged["relationships"][0] == {
        "character_id": "exp_001__char_2", "related_id": "exp_001__char_2",
    }
    assert merged["relationship_graph"]["nodes"][1]["id"] == "exp_001__char_2"


def test_expansion_rejects_duplicate_poi_and_character_before_write() -> None:
    existing_world = {"places": [{"place_id": "old", "google_place_id": "stable-poi"}]}
    addition_world = {"places": [{"place_id": "new", "google_place_id": "stable-poi"}]}
    merged_world, map_metadata = _merge_city_world(
        existing_world, addition_world, "exp_002", {}, 2, return_metadata=True,
    )
    assert len(merged_world["places"]) == 1
    assert map_metadata["rejected_places"] == 1

    existing_characters = {"characters": [{"character_id": "person_a", "name": "A"}]}
    addition_characters = {"characters": [{"character_id": "person_a", "name": "A"}]}
    merged_characters, character_metadata = _merge_characters(
        existing_characters, addition_characters, "exp_002", 2, return_metadata=True,
    )
    assert len(merged_characters["characters"]) == 1
    assert character_metadata["rejected_characters"] == 1


def test_expansion_targets_are_cumulative_and_return_a_real_batch_delta() -> None:
    prior = {
        "artifacts": {
            "map_world": {"metadata": {"counts": {"districts": 6, "corridors": 5, "places": 80}}},
            "characters": {"metadata": {"characters": 12}},
        }
    }
    plan = _plan_expansion_targets(prior, {"districts": 9, "corridors": 7, "places": 120, "characters": 16})
    assert plan["current"] == {"districts": 6, "corridors": 5, "places": 80, "characters": 12}
    assert plan["delta"] == {"districts": 3, "corridors": 2, "places": 40, "characters": 4}


def test_expansion_targets_cannot_reduce_or_request_unsupported_poi_only_batch() -> None:
    prior = {
        "artifacts": {
            "map_world": {"metadata": {"counts": {"districts": 6, "corridors": 5, "places": 80}}},
            "characters": {"metadata": {"characters": 12}},
        }
    }
    with pytest.raises(ValueError, match="小于当前"):
        _plan_expansion_targets(prior, {"districts": 5, "corridors": 7, "places": 120, "characters": 16})
    with pytest.raises(ValueError, match="POI-only"):
        _plan_expansion_targets(prior, {"districts": 6, "corridors": 5, "places": 120, "characters": 16})
    with pytest.raises(ValueError, match="结构扩增"):
        _plan_expansion_targets(prior, {"districts": 8, "corridors": 7, "places": 80, "characters": 16})


def test_candidate_city_id_distinguishes_city_country_pairs() -> None:
    assert _candidate_city_id("Shanghai", "China") == "shanghai_china"
    assert _candidate_city_id("Shanghai", "United States") == "shanghai_united_states"


def test_new_city_targets_share_the_same_contract_as_expansion_controls() -> None:
    seed = {"district_count": 4, "corridor_count": 4, "places_per_district": 18}
    assert _plan_new_city_targets(seed, {"districts": 8, "corridors": 7, "places": 180, "characters": 22}) == {
        "districts": 8,
        "corridors": 7,
        "places": 180,
        "characters": 22,
    }
    with pytest.raises(ValueError, match="至少需要 2"):
        _plan_new_city_targets(seed, {"districts": 1, "corridors": 4, "places": 10, "characters": 2})


def test_candidate_version_must_meet_requested_cumulative_targets() -> None:
    world = {
        "districts": [{}, {}], "corridors": [{}, {}], "places": [{}, {}, {}],
        "services": [], "information_items": [], "event_templates": [], "actors": [],
    }
    characters = {"characters": [{}, {}]}
    counts, gaps = _candidate_target_gap(
        world, characters, {"districts": 3, "corridors": 2, "places": 4, "characters": 3},
    )
    assert counts == {"districts": 2, "corridors": 2, "places": 3, "characters": 2}
    assert gaps == {"districts": 1, "places": 1, "characters": 1}


def test_candidate_gate_distinguishes_target_shortfall_from_integrity_error() -> None:
    world = {"districts": [], "corridors": [], "places": [], "services": [],
             "information_items": [], "event_templates": [], "actors": []}
    characters = {"characters": [{"character_id": "char_1", "name": "One"}], "relationships": []}
    report = _candidate_gate_report(
        world, characters, {"districts": 0, "corridors": 0, "places": 0, "characters": 2},
        character_deduplication={"rejected_characters": 1},
    )
    assert report["hard_errors"] == []
    assert report["can_register_actual"] is True
    assert report["target_gaps"] == {"characters": 1}
    assert report["repairable_gaps"] == [{
        "metric": "characters", "missing": 1, "reason": "duplicate_character_identity_removed",
    }]

    broken = _candidate_gate_report(
        world,
        {"characters": [{"character_id": "char_1"}],
         "relationships": [{"character_id": "char_1", "related_id": "missing"}]},
        {"districts": 0, "corridors": 0, "places": 0, "characters": 1},
    )
    assert broken["target_gaps"] == {}
    assert broken["can_register_actual"] is False
    assert broken["hard_errors"][0]["code"] == "dangling_relationship"
