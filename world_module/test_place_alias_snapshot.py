from __future__ import annotations

from pathlib import Path

from shared.io import read_json, write_json
from world_module.place_alias_snapshot import verify_query_aliases, write_place_alias_snapshot


class _MapsClient:
    def place_details(self, place_id, fields=None, language="zh-CN"):
        assert place_id == "google-1"
        assert fields == ["id", "displayName"]
        assert language == "en"
        return {"id": place_id, "displayName": {"text": "New World City", "languageCode": "en"}}

    def telemetry_snapshot(self):
        return {"requests_total": 1}


def test_place_alias_snapshot_uses_exact_google_id_and_english_display_name(tmp_path: Path):
    world_path = tmp_path / "world.json"
    write_json(world_path, {
        "cities": [{"city_id": "shanghai", "name": "Shanghai"}],
        "places": [{
            "place_id": "place-1",
            "name": "上海新世界城",
            "map_projection": {"google_place_id": "google-1"},
        }],
    })

    result = write_place_alias_snapshot(world_path, client=_MapsClient())
    artifact = read_json(result["path"])

    assert artifact["source"] == {
        "provider": "google_places_new",
        "operation": "place_details",
        "field": "displayName",
        "language_code": "en",
    }
    assert artifact["aliases"][0]["place_id"] == "place-1"
    assert artifact["aliases"][0]["google_place_id"] == "google-1"
    assert artifact["aliases"][0]["english_display_name"] == "New World City"


def test_verified_query_alias_requires_existing_google_place_id(tmp_path: Path):
    world_path = tmp_path / "world.json"
    write_json(world_path, {
        "cities": [{"city_id": "city", "center": {"lat": 1.0, "lng": 2.0}}],
        "places": [{
            "place_id": "place-1",
            "name": "Current Name",
            "map_projection": {"google_place_id": "google-1"},
        }],
    })

    class _SearchClient(_MapsClient):
        def text_search(self, query, **_kwargs):
            place_id = "google-1" if query == "Historical Name" else "outside-world"
            return [{"id": place_id, "displayName": {"text": "Current Name"}}]

    result = verify_query_aliases(
        world_path,
        ["Historical Name", "Different Place"],
        client=_SearchClient(),
    )

    records = result["payload"]["verified_query_aliases"]
    assert records[0]["status"] == "available"
    assert records[0]["place_id"] == "place-1"
    assert records[1]["status"] == "not_in_frozen_world"
    assert records[1]["place_id"] == ""
