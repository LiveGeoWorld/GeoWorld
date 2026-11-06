from __future__ import annotations

from task_module.task_types.place_grounding.batch_generation import streetview
from task_module.task_types.place_grounding.batch_generation.judge_templates import (
    build_grounding_judge_override,
)


class _FakeMapsClient:
    def street_view_metadata(self, *_args, **_kwargs):
        return {"status": "OK", "location": {"lat": 51.5, "lng": -0.1}}

    def download_street_view(self, *_args, **_kwargs):
        return b"image-bytes"

    def telemetry_snapshot(self):
        return {"requests_total": 3}


def test_streetview_reports_metadata_and_each_image_progress(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(streetview, "GoogleMapsClient", _FakeMapsClient)
    monkeypatch.setattr(streetview, "pipeline_root", lambda: tmp_path)
    events: list[tuple[str, str, str]] = []

    assets = streetview.fetch_streetview_assets(
        streetview.StreetViewRequest(
            lat=51.5,
            lng=-0.1,
            target_id="place_1",
            output_dir="assets/streetview",
            headings=[0, 90],
        ),
        progress_callback=lambda step, state, note: events.append((step, state, note)),
    )

    assert [(item["role"], item["quality"]) for item in assets] == [
        ("evaluation", "candidate"),
        ("prompt_reference", "candidate"),
    ]
    assert events == [
        ("streetview_metadata", "running", ""),
        ("streetview_metadata", "complete", ""),
        ("streetview_image", "running", "1/2"),
        ("streetview_image", "complete", "1/2"),
        ("streetview_image", "running", "2/2"),
        ("streetview_image", "complete", "2/2"),
    ]


def test_exact_poi_judge_ignores_legacy_radius_metadata() -> None:
    judge = build_grounding_judge_override({
        "example_id": "exact_poi_fixture",
        "acceptable_granularity": "exact_poi",
        "candidate_scope": {"city_id": "fixture_city"},
        "gold": {
            "candidate_id": "place_1", "place_id": "place_1", "place_name": "Exact Place",
            "coordinate": {"lat": 1.0, "lng": 2.0},
            # Historical field: it must not alter an exact-POI decision.
            "acceptable_radius_m": 300,
            "anchor_places": ["Exact Place"],
        },
        "evaluation": {"must_mention": []},
    })

    select_node = judge["stages"][1]["nodes"][0]
    checks = [*select_node["hard_checks"], *select_node["soft_checks"]]
    assert [item["check_type"] for item in checks] == ["action_executed", "field_equals"]
