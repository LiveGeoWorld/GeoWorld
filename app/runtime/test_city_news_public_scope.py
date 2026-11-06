import json
import hashlib

from app.apps.city_news import CityNewsApp


def _write_task(tmp_path):
    (tmp_path / "world_slice.json").write_text(json.dumps({
        "districts": [{
            "district_id": "district_cape_town_rondebosch",
            "name": "Rondebosch",
        }],
    }), encoding="utf-8")
    (tmp_path / "static_app_state.json").write_text(json.dumps({
        "apps": {"CityNewsApp": {"entries": [{
            "info_type": "notice",
            "risk_variant": "area_safety_advisory",
            "scope": {"type": "district", "id": "district_cape_town_rondebosch"},
        }]}},
    }), encoding="utf-8")


def _write_safety_task_without_world_districts(tmp_path):
    (tmp_path / "world_slice.json").write_text(json.dumps({"districts": []}), encoding="utf-8")
    (tmp_path / "task_sandbox.json").write_text(json.dumps({
        "risky_target": {
            "district_id": "district_cape_town_rondebosch",
            "district_name": "Rondebosch",
        },
        "alternatives": [],
        "distractors": [],
    }), encoding="utf-8")
    (tmp_path / "static_app_state.json").write_text(json.dumps({
        "apps": {"CityNewsApp": {"entries": [{
            "risk_variant": "area_safety_advisory",
            "scope": {"type": "district", "id": "district_cape_town_rondebosch"},
        }]}},
    }), encoding="utf-8")


def test_city_news_resolves_public_district_name_without_leaking_private_id(tmp_path):
    _write_task(tmp_path)
    result = CityNewsApp(str(tmp_path)).get_info(district="  rondebosch  ")
    assert result[0]["risk_variant"] == "area_safety_advisory"
    assert result[0]["scope"] == {"type": "district", "name": "Rondebosch"}
    assert "district_cape_town_rondebosch" not in str(result)


def test_city_news_distinguishes_unknown_district_from_empty_results(tmp_path):
    _write_task(tmp_path)
    assert CityNewsApp(str(tmp_path)).get_info(district="Unknown") == {
        "status": "district_not_found", "district": "Unknown", "candidates": [], "items": [],
    }


def test_city_news_resolves_task_bound_public_district_when_world_slice_is_empty(tmp_path):
    _write_safety_task_without_world_districts(tmp_path)
    result = CityNewsApp(str(tmp_path)).get_info(district="Rondebosch")
    assert result[0]["scope"] == {"type": "district", "name": "Rondebosch"}


def test_city_news_schema_requests_a_public_district_name():
    schema = CityNewsApp().get_tool_schemas()[0]["parameters"]
    assert "district" in schema["properties"]
    assert "district_id" not in schema["properties"]


def test_dynamic_district_keeps_private_binding_and_public_name(tmp_path):
    _write_task(tmp_path)
    app = CityNewsApp(str(tmp_path))
    app._register_district({'district_id': 'other', 'name': 'Other'}, name_field='name')
    public = {'type': 'district', 'name': 'Rondebosch', 'city_name': 'Cape Town'}
    app.inject_entry({'scope': {'type': 'district', 'id': 'district_cape_town_rondebosch'},
                      'public_scope': public, 'public_message': 'Temporary clearance',
                      'state_field': 'city_news.risk_status', 'risk_status': 'clear',
                      'timestep': '2026-10-05T12:00:00+02:00',
                      'end_time': '2026-10-05T13:00:00+02:00'})
    app.refresh_dynamic_state('2026-10-05T12:30:00+02:00')
    rows = app.get_info(district='Rondebosch')
    assert len(rows) == 1  # Later state supersedes only the matching static risk.
    assert rows[0]['scope'] == public
    assert rows[0]['risk_status'] == 'clear'
    assert app.get_info(district='Other') == []
    assert 'district_cape_town_rondebosch' not in str(app.get_info())
    app.refresh_dynamic_state('2026-10-05T13:00:00+02:00')
    restored = app.get_info(district='Rondebosch')
    assert len(restored) == 1
    assert restored[0]['risk_variant'] == 'area_safety_advisory'
    assert app._event_history[0]['public_scope'] == public


def test_city_news_accepts_natural_district_phrase_and_combined_info_types(tmp_path):
    _write_task(tmp_path)
    result = CityNewsApp(str(tmp_path)).get_info(
        district="Cape Town Rondebosch area",
        info_type="alerts notices events safety transport",
    )
    assert result[0]["risk_variant"] == "area_safety_advisory"


def test_city_news_returns_feedback_for_unknown_info_type(tmp_path):
    _write_task(tmp_path)
    result = CityNewsApp(str(tmp_path)).get_info(district="Rondebosch", info_type="restaurants")
    assert result["status"] == "invalid_info_type"
    assert result["allowed"] == ["notice", "traffic", "event", "news"]


def test_safety_district_query_finds_local_and_citywide_notices_without_widening_scope(tmp_path):
    world = {
        "cities": [{"name": "Cape Town"}],
        "districts": [
            {"district_id": "d1", "name": "Rondebosch"},
            {"district_id": "d2", "name": "Gardens"},
            {"district_id": "d3", "name": "Victoria & Alfred Waterfront"},
        ],
        "places": [
            {"name": "Cafe A", "location": {"lat": 1.0, "lng": 2.0}, "district_id": "d1"},
            {"name": "Cafe A", "location": {"lat": 3.0, "lng": 4.0}, "district_id": None},
            {"name": "Cafe B", "location": {"lat": 5.0, "lng": 6.0}, "district_id": None},
            {"name": "Cafe C", "location": {"lat": 7.0, "lng": 8.0}, "district_id": "d2"},
            {"name": "Cafe D", "location": {"lat": 9.0, "lng": 10.0}, "district_id": "d3"},
        ],
    }
    rows = [
        {"info_type": "notice", "title": "Local", "scope": {
            "name": "Cafe A", "city_name": "Cape Town", "center": {"lat": 1.0, "lng": 2.0},
        }, "active_window": {"start": "2026-03-20T21:00:00+02:00", "end": "2026-03-21T01:00:00+02:00"}},
        {"info_type": "traffic", "title": "Citywide", "scope": {"city_name": "Cape Town"},
         "active_window": {"start": "2026-03-20T21:00:00+02:00", "end": "2026-03-21T01:00:00+02:00"}},
        {"info_type": "notice", "title": "Unknown district", "scope": {
            "name": "Cafe B", "city_name": "Cape Town", "center": {"lat": 5.0, "lng": 6.0},
        }, "active_window": {"start": "2026-03-20T21:00:00+02:00", "end": "2026-03-21T01:00:00+02:00"}},
        {"info_type": "notice", "title": "Gardens", "scope": {
            "name": "Cafe C", "city_name": "Cape Town", "center": {"lat": 7.0, "lng": 8.0},
        }, "active_window": {"start": "2026-03-20T21:00:00+02:00", "end": "2026-03-21T01:00:00+02:00"}},
        {"info_type": "notice", "title": "Waterfront", "scope": {
            "name": "Cafe D", "city_name": "Cape Town", "center": {"lat": 9.0, "lng": 10.0},
        }, "active_window": {"start": "2026-03-20T21:00:00+02:00", "end": "2026-03-21T01:00:00+02:00"}},
    ]
    payload = {"schema_version": "safety_public_event_view.v1", "world_binding": "test-city:v1",
               "apps": {"CityNewsApp": rows, "WeatherApp": []}}
    payload["content_hash"] = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    (tmp_path / "task.json").write_text(json.dumps({"task_type": "safety_planning"}), encoding="utf-8")
    (tmp_path / "task_sandbox.json").write_text(json.dumps({
        "safety_event_view": {"path": "safety_event_view.json", "world_binding": "test-city:v1"},
        "safety_choice": {"world": world},
    }), encoding="utf-8")
    (tmp_path / "safety_event_view.json").write_text(json.dumps(payload), encoding="utf-8")

    app = CityNewsApp(str(tmp_path))
    result = app.get_info(district="Rondebosch", info_type="notice traffic", time_range="2026-03-20")
    assert [row["title"] for row in result] == ["Local", "Citywide"]
    assert result[0]["scope"] == rows[0]["scope"]
    assert [row["title"] for row in app.get_info(district="Rondebosch, Cape Town")] == ["Local", "Citywide"]
    assert [row["title"] for row in app.get_info(district="Cape Town Rondebosch")] == ["Local", "Citywide"]
    assert app.get_info(district="Huangpu District Shanghai") == []
    assert [row["title"] for row in app.get_info(district="Cafe B")] == ["Unknown district"]
    assert [row["title"] for row in app.get_info(district="V&A Waterfront")] == ["Citywide", "Waterfront"]
    assert [row["title"] for row in app.get_info(district="Victoria and Alfred Waterfront")] == ["Citywide", "Waterfront"]
    assert [row["title"] for row in app.get_info(district="Rondebosch and Gardens, Cape Town")] == ["Local", "Citywide", "Gardens"]
    assert app.get_info(district="Rondebosch and Unknown, Cape Town") == []
    assert len(app.get_info(district="Cape Town")) == 5
