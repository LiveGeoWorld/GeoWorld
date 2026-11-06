from __future__ import annotations

from types import SimpleNamespace

from shared.maps_client import GoogleMapsClient, _STREET_VIEW_TIMEOUT_SECONDS


def test_starts_google_rotation_at_configured_profile() -> None:
    config = SimpleNamespace(_raw={
        "google_maps_start_key": "google_maps_3",
        "google_maps": {"api_key": "primary"},
        "google_maps_2": {"api_key": "secondary"},
        "google_maps_3": {"api_key": "current"},
    })

    client = GoogleMapsClient(config=config)

    assert [name for name, _key in client._api_profiles] == [
        "google_maps_3", "google_maps", "google_maps_2",
    ]
    assert client.telemetry_snapshot()["start_profile"] == "google_maps_3"


def test_google_client_uses_environment_proxy_configuration(example_pipeline_config) -> None:
    client = GoogleMapsClient(config=example_pipeline_config)

    assert client._session.trust_env is True


def test_forward_geocode_zero_results_is_an_empty_result(monkeypatch, example_pipeline_config) -> None:
    client = GoogleMapsClient(config=example_pipeline_config)
    monkeypatch.setattr(client, "_request_with_api_keys", lambda *_args, **_kwargs: {"status": "ZERO_RESULTS"})

    result = client.geocode("an intentionally unmatched street")

    assert result == {
        "formatted_address": "", "address_components": [], "place_id": "",
        "types": [], "location": {}, "viewport": {},
    }


def test_reverse_geocode_zero_results_is_an_empty_result(monkeypatch, example_pipeline_config) -> None:
    client = GoogleMapsClient(config=example_pipeline_config)
    monkeypatch.setattr(client, "_request_with_api_keys", lambda *_args, **_kwargs: {"status": "ZERO_RESULTS"})

    result = client.reverse_geocode(0.0, 0.0)

    assert result == {"formatted_address": "", "address_components": [], "place_id": "", "types": []}


def test_street_view_requests_allow_slow_provider_responses(monkeypatch, example_pipeline_config) -> None:
    client = GoogleMapsClient(config=example_pipeline_config)
    metadata_call = {}
    image_call = {}

    def fake_request(*_args, **kwargs):
        metadata_call.update(kwargs)
        return {"status": "OK"}

    class FakeResponse:
        content = b"image"

        def raise_for_status(self) -> None:
            return None

    def fake_get(*_args, **kwargs):
        image_call.update(kwargs)
        return FakeResponse()

    monkeypatch.setattr(client, "_request_with_api_keys", fake_request)
    monkeypatch.setattr(client._session, "get", fake_get)

    client.street_view_metadata(1.0, 2.0)
    client.download_street_view(1.0, 2.0)

    assert metadata_call["timeout"] == _STREET_VIEW_TIMEOUT_SECONDS == 120
    assert image_call["timeout"] == _STREET_VIEW_TIMEOUT_SECONDS
