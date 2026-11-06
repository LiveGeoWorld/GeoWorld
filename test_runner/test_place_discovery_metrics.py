"""Tests for answer-first place discovery scoring."""

from test_runner.eval.place_discovery_metrics import check_discovery_answer
from test_runner.schema import AgentRunResult


def _result(name: str = "Harbor Table", lat: float = 1.2, lng: float = 3.4) -> AgentRunResult:
    return AgentRunResult(
        task_id="discovery",
        user_prompt="",
        gt_route_id="",
        final_answer={
            "feasible": True,
            "plan": {"selection": {"name": name, "location": {"lat": lat, "lng": lng}}},
        },
    )


def _gold() -> dict:
    return {
        "gold": {
            "acceptable_top1": ["private_service"],
            "candidates": [{
                "service_id": "private_service",
                "name": "Harbor Table",
                "coordinate": {"lat": 1.2, "lng": 3.4},
                "rank": 1,
            }],
        }
    }


def test_discovery_answer_matches_public_name_and_coordinates() -> None:
    assert check_discovery_answer(_result(name="  harbor TABLE "), _gold()).pass_


def test_discovery_answer_rejects_wrong_location_and_missing_selection() -> None:
    assert not check_discovery_answer(_result(lat=1.3), _gold()).pass_
    empty = AgentRunResult(task_id="discovery", user_prompt="", gt_route_id="", final_answer={})
    assert not check_discovery_answer(empty, _gold()).pass_
