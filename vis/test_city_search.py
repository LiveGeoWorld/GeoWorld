"""Contract tests for the World Manager city autocomplete flow."""

from __future__ import annotations

from pathlib import Path

import pytest

from vis import server


@pytest.fixture(autouse=True)
def city_search_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "CITY_SEARCH_LOG_ROOT", tmp_path / "city_search")
    monkeypatch.setattr(server.VisHandler, "google_maps_api_key", "test-key")


def test_google_city_autocomplete_returns_partial_input_suggestion(monkeypatch: pytest.MonkeyPatch) -> None:
    observed_urls: list[str] = []

    def fake_json(url: str, *, headers=None):
        observed_urls.append(url)
        return {
            "status": "OK",
            "predictions": [{
                "place_id": "place-singapore",
                "description": "Singapore",
                "structured_formatting": {"main_text": "Singapore", "secondary_text": "Singapore"},
            }],
        }

    monkeypatch.setattr(server, "_city_search_json", fake_json)
    response = server._city_search_suggestions("Singa", "session-1")

    assert response["provider"] == "google_places_autocomplete"
    assert response["suggestions"][0]["label"] == "Singapore"
    assert response["suggestions"][0]["requires_resolve"] is True
    assert "place/autocomplete/json" in observed_urls[0]
    assert "types=%28cities%29" in observed_urls[0]


def test_selected_google_suggestion_resolves_normalized_seed_city(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_json(url: str, *, headers=None):
        assert "place/details/json" in url
        return {
            "status": "OK",
            "result": {
                "name": "Singapore",
                "formatted_address": "Singapore",
                "address_components": [{"long_name": "Singapore", "short_name": "SG", "types": ["country"]}],
                "geometry": {
                    "location": {"lat": 1.3521, "lng": 103.8198},
                    "viewport": {"northeast": {"lat": 1.47, "lng": 104.0}, "southwest": {"lat": 1.2, "lng": 103.6}},
                },
            },
        }

    monkeypatch.setattr(server, "_city_search_json", fake_json)
    city = server._resolve_city_suggestion("place-singapore", "session-1")

    assert city["city_name"] == "Singapore"
    assert city["country"] == "Singapore"
    assert city["timezone"] == "Asia/Singapore"
    assert city["city_id"] == "singapore_singapore"
    assert city["source"] == "google_places_details"


def test_google_failure_falls_back_to_nominatim_without_exposing_socket_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_json(url: str, *, headers=None):
        if "maps.googleapis.com" in url:
            raise RuntimeError("城市搜索服务暂时不可用")
        return [{
            "lat": "1.3521", "lon": "103.8198", "display_name": "Singapore",
            "boundingbox": ["1.2", "1.47", "103.6", "104.0"],
            "address": {"city": "Singapore", "country": "Singapore", "country_code": "sg"},
        }]

    monkeypatch.setattr(server, "_city_search_json", fake_json)
    response = server._city_search_suggestions("Singapore", "session-2")

    assert response["fallback_used"] is True
    assert response["suggestions"][0]["source"] == "nominatim"
    assert response["suggestions"][0]["resolved"]["city_name"] == "Singapore"


def test_normal_generation_does_not_expose_force_checkbox_or_default_true() -> None:
    source = Path(server.__file__).read_text(encoding="utf-8")
    frontend = (Path(server.__file__).parent / "static" / "world-manager.js").read_text(encoding="utf-8")
    html = (Path(server.__file__).parent / "static" / "index.html").read_text(encoding="utf-8")

    assert 'payload.get("force", False)' in source
    assert 'id="mgr-force"' not in html
    assert 'force: force' not in frontend
    assert "/api/jobs/${encodeURIComponent(Mgr.currentJobId)}/retry" in frontend
