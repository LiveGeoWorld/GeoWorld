from __future__ import annotations

from test_runner.eval.replan_metrics import (
    check_alternative_valid,
    check_base_constraints_still_met,
    check_disruption_detected,
    check_replan_executed,
    check_transition_proof_valid,
    _solution_matches,
)
from test_runner.schema import AgentRunResult, ToolCallRecord


def _result() -> AgentRunResult:
    return AgentRunResult(
        task_id="dynamic_001",
        user_prompt="Arrange a meeting, then update the plan.",
        gt_route_id="",
        initial_plan={
            "feasible": True,
            "plan": {"restaurant_name": "Alpha"},
        },
        final_answer={
            "feasible": True,
            "plan": {"restaurant_name": "Beta", "event_information": ["Alpha is temporarily unavailable."]},
        },
        trace=[
            ToolCallRecord(1, "", "submit_initial_plan"),
            ToolCallRecord(2, "", "LocalServiceApp__get_service_detail", {"service_name": "Alpha"},
                           {"service_notices": [{"message": "Alpha is temporarily unavailable."}]}),
            ToolCallRecord(3, "", "final_answer"),
        ],
        dynamic_event_states={"closure_001": "applied"},
    )


def _gt(*, proof_valid: bool = True) -> dict:
    return {
        "candidate_restaurants": [
            {"service_id": "service_alpha", "name": "Alpha"},
            {"service_id": "service_beta", "name": "Beta"},
        ],
        "counterfactual_proof": {
            "source": {"task_type": "multi_entity"},
            "baseline_solution": {"service_id": "service_alpha"},
            "post_event_solution": {"service_id": "service_beta"},
            "event_impacts": [{
                "event_id": "closure_001",
                "invalidates_or_changes_decision": True,
                "public_observation_path": {
                    "app": "LocalServiceApp", "tool": "get_service_detail",
                },
                "ablation": {"decision_changed": True},
            }],
        },
        "_dynamic_events": [{
            "event_id": "closure_001",
            "app": "LocalServiceApp", "public_message": "Alpha is temporarily unavailable.",
            "activation": {"mode": "absolute"},
            "observability": {
                "public_observation_path": {"app": "LocalServiceApp", "tool": "get_service_detail"},
            },
        }],
        "_dynamic_proof_validation": {
            "valid": proof_valid,
            "errors": [] if proof_valid else ["incomplete_event_impact_coverage"],
        },
    }


def test_round_answers_require_and_certify_initial_p0():
    result = _result()
    result.initial_plan = None
    result.trace = result.trace[1:]
    result.answer_versions = [{"answer_type": "final", "answer": result.final_answer}]
    assert not check_disruption_detected(result, _gt()).pass_
    assert not check_replan_executed(result, _gt()).pass_
    from test_runner.runner import _evaluate_dynamic, _result_to_dict
    _evaluate_dynamic(result, _gt())
    assert not {"base_constraints_met", "event_progression", "alternative_valid"} & {m.metric for m in result.metrics}
    assert _result_to_dict(result)["answer_versions"] == result.answer_versions
    result.final_answer = None
    assert not check_replan_executed(result, _gt()).pass_


def test_round_final_still_requires_observed_events_and_correct_gt():
    result = _result()
    result.initial_plan = None
    result.answer_versions = [{"answer_type": "final"}]
    result.trace = []
    assert not check_disruption_detected(result, _gt()).pass_
    result.final_answer["plan"]["restaurant_name"] = "Alpha"
    assert not check_replan_executed(result, _gt()).pass_


def test_multi_entity_dynamic_metrics_require_the_verified_p0_to_p1_transition() -> None:
    result = _result()
    gt = _gt()
    assert check_transition_proof_valid(result, gt).pass_
    assert check_base_constraints_still_met(result, gt).pass_
    assert check_disruption_detected(result, gt).pass_
    assert check_replan_executed(result, gt).pass_
    assert check_alternative_valid(result, gt).pass_


def test_observation_round_intermediate_is_the_judge_p0_boundary() -> None:
    result = _result()
    result.trace[0] = ToolCallRecord(
        1, "", "UserInterfaceApp__final_answer",
        {"answer_type": "intermediate"},
        {"status": "intermediate_recorded", "answer_type": "intermediate"},
    )
    result.answer_versions = [
        {"round": 1, "answer_type": "intermediate", "answer": result.initial_plan},
        {"round": 2, "answer_type": "final", "answer": result.final_answer},
    ]
    assert check_disruption_detected(result, _gt()).pass_
    assert check_replan_executed(result, _gt()).pass_


def test_dynamic_metrics_fail_closed_when_the_task_proof_is_invalid() -> None:
    metric = check_transition_proof_valid(_result(), _gt(proof_valid=False))
    assert not metric.pass_
    assert "incomplete_event_impact_coverage" in metric.detail


def test_event_grounding_rejects_tool_only_and_unsubmitted_notices() -> None:
    result = _result()
    result.trace[1].result = {"found": True}
    assert not check_disruption_detected(result, _gt()).pass_
    result.trace[1].result = {"service_notices": [{"message": "Alpha is temporarily unavailable."}]}
    result.final_answer["plan"]["event_information"] = []
    assert not check_disruption_detected(result, _gt()).pass_


def test_event_grounding_rejects_wrong_app_and_invented_information() -> None:
    result = _result()
    result.trace[1].tool = "CityNewsApp__get_service_detail"
    assert not check_disruption_detected(result, _gt()).pass_
    result.trace[1].tool = "LocalServiceApp__get_service_detail"
    result.final_answer["plan"]["event_information"].append("Invented event")
    assert not check_disruption_detected(result, _gt()).pass_


def test_event_grounding_accepts_exact_message_from_another_public_tool_of_owning_app() -> None:
    result = _result()
    result.trace[1].tool = "LocalServiceApp__get_notice_history"
    assert check_disruption_detected(result, _gt()).pass_


def test_current_place_selection_contract_resolves_source_specific_dynamic_solutions() -> None:
    selection = {"name": "Target", "location": {"lat": 1.0, "lng": 2.0}}
    gt = {"places": [{"place_id": "place_target", **selection}]}
    payload = {"feasible": True, "plan": {"selection": selection}}

    for source, solution in [
        ("safety_planning", {"place_id": "place_target"}),
        ("multi_entity", {"place_id": "place_target"}),
        ("region_aware", {"place_ids": ["place_target"]}),
    ]:
        current_gt = {**gt, "counterfactual_proof": {"source": {"task_type": source}}}
        assert _solution_matches(payload, current_gt, solution)


def test_dynamic_grade_requires_both_gt_and_event_grounding() -> None:
    from test_runner.schema import MetricResult

    result = _result()
    result.metrics = [
        MetricResult("replan_executed", "GT", True, ""),
        MetricResult("disruption_detected", "Event C", False, ""),
        MetricResult("other", "Other", True, ""),
        MetricResult("other_2", "Other", True, ""),
    ]
    result.total_metrics = 4
    result.passed_metrics = 3
    assert result.grade == "FAIL"


def test_dynamic_metrics_reject_reuse_of_the_initial_solution() -> None:
    result = _result()
    result.final_answer = {"feasible": True, "plan": {"restaurant_name": "Alpha"}}
    assert not check_replan_executed(result, _gt()).pass_
    assert not check_alternative_valid(result, _gt()).pass_


def test_route_dynamic_metrics_use_boolean_gt_not_route_identity() -> None:
    result = AgentRunResult(
        task_id="route_dynamic", user_prompt="u", gt_route_id="",
        initial_plan={"feasible": True, "route_ids": ["r0"]},
        final_answer={"feasible": False, "plan": {"event_information": ["Road delay"]}},
        trace=[ToolCallRecord(1, "", "submit_initial_plan"), ToolCallRecord(2, "", "CityNewsApp__get_info",
                              result={"description": "Road delay"}), ToolCallRecord(3, "", "final_answer")],
        dynamic_event_states={"traffic_001": "applied"},
    )
    gt = {
        "counterfactual_proof": {"source": {"task_type": "route_planning"},
                                 "baseline_solution": {"expected_feasible": True, "route_ids": ["r0"]},
                                 "post_event_solution": {"expected_feasible": False, "route_ids": []},
                                 "event_impacts": [{"event_id": "traffic_001", "causally_changes_route_state": True,
                                                    "public_observation_path": {"tool": "get_info"}, "ablation": {"state_changed": True}}]},
        "_dynamic_events": [{"event_id": "traffic_001", "app": "CityNewsApp", "public_message": "Road delay",
                             "activation": {"mode": "absolute"},
                             "observability": {"public_observation_path": {"app": "CityNewsApp", "tool": "get_info"}}}],
        "_dynamic_proof_validation": {"valid": True, "errors": []},
    }
    assert check_base_constraints_still_met(result, gt).pass_
    assert check_disruption_detected(result, gt).pass_
    assert check_replan_executed(result, gt).pass_
    assert check_alternative_valid(result, gt).pass_


def test_itinerary_dynamic_metrics_resolve_public_place_names() -> None:
    result = AgentRunResult(
        task_id="itinerary_dynamic", user_prompt="u", gt_route_id="",
        initial_plan={"feasible": True, "plan": {"days": [{"day": 1, "events": [{"place_name": "Hotel"}, {"place_name": "Museum A"}]}]}},
        final_answer={"feasible": True, "plan": {"days": [{"day": 1, "events": [{"place_name": "Hotel"}, {"place_name": "Museum B"}]}],
                                                 "event_information": ["Museum A is temporarily unavailable."]}},
        trace=[ToolCallRecord(1, "", "submit_initial_plan"), ToolCallRecord(2, "", "LocalServiceApp__get_service_detail",
                              result={"service_notices": [{"message": "Museum A is temporarily unavailable."}]}),
               ToolCallRecord(3, "", "final_answer")],
        dynamic_event_states={"service_001": "applied"},
    )
    gt = {
        "hotel": {"place_id": "hotel", "name": "Hotel"},
        "all_pois": [{"place_id": "p0", "name": "Museum A"}, {"place_id": "p1", "name": "Museum B"}],
        "counterfactual_proof": {"source": {"task_type": "itinerary"},
                                 "baseline_solution": {"place_ids": ["hotel", "p0"]}, "post_event_solution": {"place_ids": ["hotel", "p1"]},
                                 "event_impacts": [{"event_id": "service_001", "invalidates_or_changes_decision": True,
                                                    "public_observation_path": {"tool": "get_service_detail"}, "ablation": {"decision_changed": True}}]},
        "_dynamic_events": [{"event_id": "service_001", "app": "LocalServiceApp",
                             "public_message": "Museum A is temporarily unavailable.", "activation": {"mode": "absolute"},
                             "observability": {"public_observation_path": {"app": "LocalServiceApp", "tool": "get_service_detail"}}}],
        "_dynamic_proof_validation": {"valid": True, "errors": []},
    }
    assert check_base_constraints_still_met(result, gt).pass_
    assert check_disruption_detected(result, gt).pass_
    assert check_replan_executed(result, gt).pass_
    assert check_alternative_valid(result, gt).pass_


def test_region_dynamic_metrics_keep_private_rules_out_of_the_resolver() -> None:
    result = AgentRunResult(
        task_id="region_dynamic", user_prompt="u", gt_route_id="",
        initial_plan={"feasible": True, "plan": {"events": [{"place_name": "Venue A"}, {"place_name": "Venue B"}]}},
        final_answer={"feasible": True, "plan": {"events": [{"place_name": "Venue B"}, {"place_name": "Venue C"}],
                                                 "event_information": ["Venue A is temporarily unavailable."]}},
        trace=[ToolCallRecord(1, "", "submit_initial_plan"), ToolCallRecord(2, "", "LocalServiceApp__get_service_detail",
                              result={"service_notices": [{"message": "Venue A is temporarily unavailable."}]}),
               ToolCallRecord(3, "", "final_answer")],
        dynamic_event_states={"service_001": "applied"},
    )
    gt = {
        "main_pois": [{"place_id": "p0", "name": "Venue A"}, {"place_id": "p1", "name": "Venue B"}, {"place_id": "p2", "name": "Venue C"}],
        "counterfactual_proof": {"source": {"task_type": "region_aware"},
                                 "baseline_solution": {"place_ids": ["p0", "p1"]}, "post_event_solution": {"place_ids": ["p1", "p2"]},
                                 "event_impacts": [{"event_id": "service_001", "invalidates_or_changes_decision": True,
                                                    "public_observation_path": {"tool": "get_service_detail"}, "ablation": {"decision_changed": True}}]},
        "_dynamic_events": [{"event_id": "service_001", "app": "LocalServiceApp",
                             "public_message": "Venue A is temporarily unavailable.", "activation": {"mode": "absolute"},
                             "observability": {"public_observation_path": {"app": "LocalServiceApp", "tool": "get_service_detail"}}}],
        "_dynamic_proof_validation": {"valid": True, "errors": []},
    }
    assert check_base_constraints_still_met(result, gt).pass_
    assert check_disruption_detected(result, gt).pass_
    assert check_replan_executed(result, gt).pass_
    assert check_alternative_valid(result, gt).pass_
def test_efficiency_includes_answer_actions():
    from test_runner.eval.replan_metrics import check_tool_efficiency
    from test_runner.schema import AgentRunResult, ToolCallRecord
    result = AgentRunResult(task_id="t", user_prompt="", gt_route_id="")
    result.trace = [ToolCallRecord(i, "", "MapApp__search_places") for i in range(16)]
    result.trace += [ToolCallRecord(17, "", "UserInterfaceApp__final_answer"),
                     ToolCallRecord(18, "", "final_answer")]
    metric = check_tool_efficiency(result, {"events": [{"event_id": "e"}]})
    assert not metric.pass_
    assert metric.detail == "18 calls exceed dynamic budget 16"


def test_initial_answer_content_does_not_gate_final_solution():
    from test_runner.runner import _evaluate_dynamic
    for versions in ([], [{"answer_type": "intermediate"}, {"answer_type": "final"}]):
        result = _result()
        result.initial_plan = {"feasible": False, "plan": {"restaurant_name": "Other"}}
        result.answer_versions = versions
        assert check_base_constraints_still_met(result, _gt()).pass_
        assert check_replan_executed(result, _gt()).pass_
        _evaluate_dynamic(result, _gt())
        assert result.grade == "PASS"
        # A correct initial checkpoint cannot excuse an incorrect final answer.
        result.final_answer["plan"]["restaurant_name"] = "Alpha"
        assert not check_replan_executed(result, _gt()).pass_
        result.initial_plan = None
        assert not check_base_constraints_still_met(result, _gt()).pass_
        assert not check_replan_executed(result, _gt()).pass_
