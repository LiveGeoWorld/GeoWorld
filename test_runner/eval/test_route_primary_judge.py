from __future__ import annotations

import json

import pytest

from test_runner.eval.evaluator import evaluate
from test_runner.eval.metrics import _build_route_dir, check_efficiency
from test_runner.runner import _result_to_dict
from test_runner.schema import AgentRunResult, ToolCallRecord


def _judge(tmp_path, expected, answer, termination=None):
    path = tmp_path / "route_sandbox.json"
    path.write_text(json.dumps({"expected_feasible": expected}), encoding="utf-8")
    result = AgentRunResult("route_test", "Is this feasible?", "",
                            final_answer=answer, termination=termination or {})
    evaluate(result, str(path))
    return result


@pytest.mark.parametrize("expected", [False, True])
@pytest.mark.parametrize("answer", [None, {}, {"feasible": "false"}, {"feasible": 0},
                                     {"feasible": None}, {"explanation": "possible"}])
def test_missing_or_nonboolean_answer_cannot_pass(tmp_path, expected, answer):
    result = _judge(tmp_path, expected, answer)
    assert result.grade == "FAIL"
    assert result.total_metrics == 7
    assert len(result.metrics) == 7


@pytest.mark.parametrize("expected", [False, True])
def test_wrong_feasibility_cannot_be_outvoted_by_auxiliary_metrics(tmp_path, expected):
    result = _judge(tmp_path, expected, {"feasible": not expected})
    assert result.grade == "FAIL"
    assert result.metrics[0].pass_ is False


@pytest.mark.parametrize("expected", [False, True])
def test_correct_decision_still_requires_five_of_seven(tmp_path, expected):
    result = _judge(tmp_path, expected, {"feasible": expected})
    assert result.grade == ("FAIL" if expected else "PASS")
    assert result.total_metrics == 7
    assert len(result.metrics) == 7
    assert result.metrics[0].metric == "feasible_consistency"
    statuses = {m.verification_status for m in result.metrics[1:6]}
    assert statuses == ({"unverifiable"} if expected else {"not_applicable"})
    payload = _result_to_dict(result)
    assert payload["metrics"][1]["verification_status"] in statuses
    assert payload["final_answer"] == {"feasible": expected}


def test_api_failure_retains_infrastructure_classification(tmp_path):
    result = _judge(tmp_path, False, None,
                    {"reason": "evaluation_infrastructure_failure", "category": "model_api"})
    assert result.grade == "INFRASTRUCTURE_FAILURE"


@pytest.mark.parametrize("expected", [None, "false", 1])
def test_invalid_gt_is_not_invented(tmp_path, expected):
    with pytest.raises(ValueError, match="boolean expected_feasible"):
        _judge(tmp_path, expected, {"feasible": True})


def test_legacy_and_namespaced_tool_names_have_identical_diagnostics():
    traces = []
    for prefix in ["", "MapApp__"]:
        trace = [ToolCallRecord(i, "", prefix + "query_routes",
                               {"origin_id": f"p{i}", "dest_id": f"p{i+1}"},
                               {"routes": [{"route_id": f"r{i}", "duration_s": 60}]})
                 for i in range(4)]
        trace += [ToolCallRecord(4, "", "UserInterfaceApp__final_answer" if prefix else "final_answer")]
        traces.append(AgentRunResult("r", "", "", trace=trace))
    assert _build_route_dir(traces[0]) == _build_route_dir(traces[1])
    assert check_efficiency(traces[0]) == check_efficiency(traces[1])


def test_no_route_identifier_is_fabricated_from_an_observation():
    result = AgentRunResult("r", "", "", trace=[
        ToolCallRecord(1, "", "MapApp__query_routes", {},
                       {"routes": [{"mode": "DRIVE", "duration_s": 60}]})])
    assert _build_route_dir(result) == {}


def _public_result(answer=None, routes=None):
    route = {"mode": "DRIVE", "duration_s": 600, "distance_m": 4000,
             "walking_distance_m": 0, "available": True}
    origin = {"name": "Start", "location": {"lat": 1, "lng": 2}}
    dest = {"name": "End", "location": {"lat": 3, "lng": 4}}
    return AgentRunResult("r", "", "", final_answer=answer or {
        "feasible": True, "mode": "DRIVE",
        "plan": {"legs": [{"origin": origin, "destination": dest}]},
    }, trace=[ToolCallRecord(1, "", "MapApp__query_routes", {"origin": origin, "destination": dest},
                            {"routes": routes or [route]})])


def _public_gt(tmp_path, **constraints):
    path = tmp_path / "gt.json"
    path.write_text(json.dumps({"expected_feasible": True,
                               "core_places": [{"place_id": "a", "name": "Start", "role": "origin"},
                                               {"place_id": "b", "name": "End", "role": "destination"}],
                               "constraints": {"time_budget_min": 10, "max_walking_distance_m": 0, **constraints}}))
    return path


def test_public_name_coordinate_submission_is_fully_verifiable_without_route_ids(tmp_path):
    result = _public_result()
    evaluate(result, str(_public_gt(tmp_path)))
    assert result.grade == "PASS"
    assert result.passed_metrics == result.total_metrics == 7


def test_querying_a_route_does_not_select_it(tmp_path):
    result = _public_result({"feasible": True, "mode": "DRIVE", "explanation": "I queried a route"})
    evaluate(result, str(_public_gt(tmp_path)))
    assert result.grade == "FAIL"
    assert result.metrics[1].verification_status == "unverifiable"


def test_ambiguous_observed_routes_are_not_selected_using_gt(tmp_path):
    result = _public_result(routes=[
        {"mode": "DRIVE", "duration_s": 600, "distance_m": 4000, "walking_distance_m": 0},
        {"mode": "DRIVE", "duration_s": 900, "distance_m": 5000, "walking_distance_m": 0},
    ])
    evaluate(result, str(_public_gt(tmp_path)))
    assert result.grade == "FAIL"
    assert result.metrics[1].verification_status == "unverifiable"


def test_time_is_not_rounded_down_and_zero_walking_limit_is_enforced(tmp_path):
    result = _public_result(routes=[{"mode": "DRIVE", "duration_s": 601, "distance_m": 4000,
                                   "walking_distance_m": 1}])
    evaluate(result, str(_public_gt(tmp_path)))
    assert not result.metrics[4].pass_
    assert not result.metrics[5].pass_
    assert result.passed_metrics == 5
    assert result.grade == "PASS"


def test_four_of_seven_is_fail_despite_correct_feasibility(tmp_path):
    result = _public_result(routes=[{"mode": "DRIVE", "duration_s": 601, "distance_m": 4000,
                                   "walking_distance_m": 1}])
    evaluate(result, str(_public_gt(tmp_path, forbidden_modes=["DRIVE"])))
    assert result.passed_metrics == 4
    assert result.grade == "FAIL"


def test_name_only_submission_rejects_same_name_different_coordinates(tmp_path):
    result = _public_result({"feasible": True, "mode": "DRIVE", "plan": {"pickup_order": ["Start", "End"]}})
    import copy
    other = copy.deepcopy(result.trace[0])
    other.args["origin"]["location"]["lat"] = 5
    result.trace.append(other)
    evaluate(result, str(_public_gt(tmp_path)))
    assert result.metrics[1].verification_status == "unverifiable"


def test_protocol_failure_cannot_pass_on_infeasible_branch_credit(tmp_path):
    result = _judge(tmp_path, False, None, {"reason": "agent_protocol_failure", "category": "no_action"})
    assert result.grade == "FAIL"
    assert not any(m.pass_ for m in result.metrics[:6])
