from __future__ import annotations

from typing import Any

import pytest

from world_module.map._internal.google_geo_enricher import GoogleGeoEnricher
from world_module.map._internal.open_geo_enricher import OpenStreetMapGeoEnricher
from world_module.map.seed import CitySeed


def _seed(*, multiplier: float = 1.25) -> CitySeed:
    return CitySeed(
        city_id="candidate_budget_test",
        city_name="Candidate Budget Test",
        country="Testland",
        center={"lat": 10.0, "lng": 20.0},
        timezone="UTC",
        bbox={"south": 9.0, "north": 11.0, "west": 19.0, "east": 21.0},
        district_count=2,
        corridor_count=1,
        places_per_district=10,
        target_place_count=20,
        poi_candidate_multiplier=multiplier,
    )


def test_output_variant_keeps_city_identity_for_repair_discovery_cache() -> None:
    seed = _seed()
    repair_seed = CitySeed(**{**seed.__dict__, "output_variant": "poi_repair_test_1"})

    assert repair_seed.city_id == seed.city_id
    assert repair_seed.variant == "poi_repair_test_1"


def _place(prefix: str, index: int) -> dict[str, Any]:
    return {
        "id": f"{prefix}_{index}",
        "displayName": {"text": f"{prefix} {index}"},
        "formattedAddress": "1 Test Street",
        "location": {"latitude": 10.0 + index / 10000, "longitude": 20.0},
        "primaryType": "cafe",
        "types": ["cafe"],
    }


def _plans() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    plans = [
        {"district_name": "North", "included_types": ["cafe"]},
        {"district_name": "South", "included_types": ["cafe"]},
    ]
    districts = [
        {"name": "North", "center": {"lat": 10.1, "lng": 20.0}},
        {"name": "South", "center": {"lat": 9.9, "lng": 20.0}},
    ]
    corridors = [{"name": "Main", "nodes": [{"location": {"lat": 10.0, "lng": 20.0}}]}]
    return plans, districts, corridors


@pytest.mark.parametrize("enricher_class", [GoogleGeoEnricher, OpenStreetMapGeoEnricher])
def test_poi_collection_respects_candidate_budget_without_external_calls(enricher_class: type[Any]) -> None:
    seed = _seed()
    enricher = object.__new__(enricher_class)
    calls: list[tuple[str, int]] = []

    def district_search(
        _seed: CitySeed,
        plan: dict[str, Any],
        _district: dict[str, Any],
        *,
        candidate_limit: int,
    ) -> list[dict[str, Any]]:
        calls.append((str(plan["district_name"]), candidate_limit))
        return [_place(str(plan["district_name"]), i) for i in range(candidate_limit)]

    def corridor_search(
        _seed: CitySeed,
        _corridors: list[dict[str, Any]],
        *,
        candidate_limit: int,
    ) -> list[dict[str, Any]]:
        calls.append(("corridor", candidate_limit))
        return [_place("corridor", i) for i in range(candidate_limit)]

    enricher._search_district_places = district_search
    enricher._enrich_corridor_places = corridor_search
    plans, districts, corridors = _plans()
    if enricher_class is GoogleGeoEnricher:
        places, report = enricher._enrich_places(
            seed,
            plans,
            districts,
            corridors,
            attach_place_details=False,
        )
    else:
        places, report = enricher._enrich_places(seed, plans, districts, corridors)

    assert seed.poi_candidate_budget == 25
    assert report["candidate_budget"] == 25
    assert report["eligible_deduplicated_candidates"] == 25
    assert report["stopped_at_candidate_budget"] is True
    assert len(places) == 25
    assert calls == [("North", 10), ("South", 9), ("corridor", 6)]


def test_poi_candidate_multiplier_is_limited_to_confirmed_range() -> None:
    assert _seed(multiplier=1.20).poi_candidate_budget == 24
    assert _seed(multiplier=4.0).poi_candidate_budget == 25


def test_existing_version_source_ids_do_not_consume_candidate_budget() -> None:
    seed = _seed()
    seed.excluded_place_ids = ["old_0", "old_1"]
    enricher = object.__new__(GoogleGeoEnricher)
    plans, districts, corridors = _plans()

    def district_search(_seed: CitySeed, plan: dict[str, Any], _district: dict[str, Any], *, candidate_limit: int) -> list[dict[str, Any]]:
        prefix = "old" if plan["district_name"] == "North" else "new"
        return [_place(prefix, index) for index in range(candidate_limit)]

    enricher._search_district_places = district_search
    enricher._enrich_corridor_places = lambda _seed, _corridors, *, candidate_limit: [
        _place("corridor", index) for index in range(candidate_limit)
    ]
    places, report = enricher._enrich_places(seed, plans, districts, corridors, attach_place_details=False)

    assert all(place["google_place_id"] not in {"old_0", "old_1"} for place in places)
    assert report["excluded_existing_source_ids"] == 2


def test_targeted_supplement_is_bounded_and_excludes_accepted_source_ids() -> None:
    seed = _seed()
    enricher = object.__new__(GoogleGeoEnricher)
    calls: list[str] = []

    def nearby(_lat: float, _lng: float, *, included_types: list[str], **_kwargs: Any) -> list[dict[str, Any]]:
        calls.extend(included_types)
        return [_place("old", 0), _place(included_types[0], 1)]

    enricher._nearby_search = nearby
    accepted = [_place("old", 0) for _ in range(18)]
    candidates, report = enricher.collect_targeted_place_supplement(
        seed,
        [{"center": {"lat": 10.0, "lng": 20.0}}],
        [],
        accepted,
        attempted_types={"restaurant"},
        round_number=1,
    )

    assert report["missing_before"] == 2
    assert report["request_budget"] == 3
    assert "restaurant" not in calls
    assert all(place["google_place_id"] != "old_0" for place in candidates)
