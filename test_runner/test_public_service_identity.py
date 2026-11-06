"""Regression tests for the public LocalServiceApp identity boundary."""

from __future__ import annotations

from app.runtime.local_service import LocalServiceAppRuntime
from test_runner.eval.multi_entity_metrics import check_restaurant_match
from test_runner.runner import _append_output_contract, _evaluate_multi_entity
from test_runner.schema import AgentRunResult


def _runtime() -> LocalServiceAppRuntime:
    return LocalServiceAppRuntime(
        services=[{
            "service_id": "private_service",
            "place_id": "private_place",
            "service_type": "restaurant_reservation",
            "state_model": {"price_band": "medium", "open_status": "open"},
        }],
        places=[{
            "place_id": "private_place",
            "name": "Harbor Table",
            "city_id": "private_city",
            "location": {"lat": 1.2, "lng": 3.4},
            "map_projection": {"rating": 4.7, "types": ["restaurant"]},
        }],
        cities=[{"city_id": "private_city", "name": "Example City"}],
    )


def test_service_runtime_exposes_name_and_coordinates_not_storage_ids() -> None:
    runtime = _runtime()
    result = runtime.call("search_services", {"service_type": "restaurant_reservation"})["results"]

    assert len(result) == 1
    assert result[0]["name"] == "Harbor Table"
    assert result[0]["location"] == {"lat": 1.2, "lng": 3.4}
    assert result[0]["city"] == "Example City"
    assert "service_id" not in result[0]
    assert "place_id" not in result[0]
    detail = runtime.call("get_service_detail", {"name": "Harbor Table", "location": {"lat": 1.2, "lng": 3.4}})
    assert detail["status"] == "ok"
    assert "service_id" not in detail["detail"]
    assert "place_id" not in detail["detail"]


def test_multi_entity_judge_normalizes_public_name_and_coordinates() -> None:
    result = AgentRunResult(
        task_id="public-selection",
        user_prompt="",
        gt_route_id="",
        final_answer={
            "feasible": True,
            "plan": {
                "restaurant_name": "Harbor Table",
                "restaurant_location": {"lat": 1.2, "lng": 3.4},
            },
        },
    )
    gt = {
        "expected_feasible": True,
        "candidate_restaurants": [{
            "service_id": "private_service",
            "name": "Harbor Table",
            "location": {"lat": 1.2, "lng": 3.4},
        }],
        "acceptable_plans": [{"restaurant_service_id": "private_service"}],
    }

    assert check_restaurant_match(result, gt).pass_


def test_multi_entity_judge_resolves_generic_public_place_selection() -> None:
    result = AgentRunResult(
        task_id="generic-public-selection", user_prompt="", gt_route_id="",
        final_answer={"feasible": True, "plan": {
            "selection": {"name": "Harbor Park", "location": {"lat": 1.2, "lng": 3.4}},
        }},
    )
    gt = {
        "expected_feasible": True,
        "expected_place_id": "private_park",
        "candidate_places": [{
            "place_id": "private_park", "name": "Harbor Park",
            "location": {"lat": 1.2, "lng": 3.4}, "primary_type": "park",
        }],
    }
    assert check_restaurant_match(result, gt).pass_


def test_multi_entity_judge_matches_legacy_service_id_after_public_resolution() -> None:
    result = AgentRunResult(
        task_id="legacy-public-selection", user_prompt="", gt_route_id="",
        final_answer={"feasible": True, "plan": {
            "selection": {
                "name": "Bootlegger Bree Street",
                "location": {"lat": -33.9245321, "lng": 18.4147633},
            },
        }},
    )
    gt = {
        "expected_feasible": True,
        "expected_restaurant_service_id": "service_bootlegger",
        "candidate_restaurants": [{
            "service_id": "service_bootlegger",
            "place_id": "place_bootlegger",
            "name": "Bootlegger Bree Street",
            "location": {"lat": -33.9245321, "lng": 18.4147633},
        }],
    }
    assert check_restaurant_match(result, gt).pass_


def test_multi_entity_poi_match_is_primary_while_other_metrics_remain_diagnostic() -> None:
    result = AgentRunResult(
        task_id="poi-primary", user_prompt="", gt_route_id="",
        final_answer={"feasible": True, "plan": {
            "selection": {
                "name": "Harbor Table",
                "location": {"lat": 1.2, "lng": 3.4},
            },
        }},
    )
    gt = {
        "expected_feasible": True,
        "expected_restaurant_service_id": "private_service",
        "candidate_restaurants": [{
            "service_id": "private_service",
            "place_id": "private_place",
            "name": "Harbor Table",
            "location": {"lat": 1.2, "lng": 3.4},
        }],
        "required_output_fields": [
            "restaurant_service_id", "assignments", "total_commute_min",
        ],
        "participants": [{"character_id": "private_character", "name": "Person"}],
        "constraints": {"max_commute_min": 30},
    }

    _evaluate_multi_entity(result, gt)
    _append_output_contract(result, "multi_entity", gt)

    assert result.grade == "PASS"
    assert result.passed_metrics == result.total_metrics == 1
    assert next(metric for metric in result.metrics if metric.metric == "place_match").pass_
    assert any(not metric.pass_ for metric in result.metrics if metric.metric != "place_match")
    assert not any(metric.metric == "required_structured_output" for metric in result.metrics)
