from test_runner.eval.region_metrics import check_cultural_decision_integrated, check_region_decision_success
from test_runner.schema import AgentRunResult


def _result(actions):
    result = AgentRunResult(task_id="t", user_prompt="u", gt_route_id="")
    result.final_answer = {
        "feasible": True,
        "plan": {"events": [{"place_name": "Venue", "start": "10:00"}],
                 "rationale": "A concrete plan", "cultural_actions": actions},
    }
    return result


def _gt():
    return {"decision_contract": {
        "material": True,
        "material_constraints": [{
            "with_constraint": {"cultural_actions": ["remove_shoes_before_entry"]},
            "without_constraint": {"cultural_actions": []},
        }],
    }}


def test_region_metric_requires_plan_action_not_explanation_keyword() -> None:
    result = _result([])
    result.final_answer["explanation"] = "Remember to remove shoes before entry."
    assert check_cultural_decision_integrated(result, _gt()).pass_ is False


def test_region_metric_accepts_public_natural_language_action() -> None:
    assert check_cultural_decision_integrated(_result(["脱鞋入内"]), _gt()).pass_ is True


def _contextual_choice_result(name: str, location: dict) -> AgentRunResult:
    result = AgentRunResult(task_id="contextual-choice", user_prompt="u", gt_route_id="")
    result.final_answer = {
        "feasible": True,
        "explanation": "This is the most suitable option.",
        "plan": {"selection": {"name": name, "location": location}},
    }
    return result


def _contextual_choice_gt() -> dict:
    return {
        "region_decision": {
            "schema_version": "geoword.region_contextual_choice.v1",
            "decision_object": "local_service_selection",
            "accepted_options": [
                {"name": "Evening Table", "location": {"lat": 1.301, "lng": 103.858}},
            ],
        },
    }


def test_contextual_choice_scores_public_selection_not_rule_recall() -> None:
    result = _contextual_choice_result("Evening Table", {"lat": 1.301, "lng": 103.858})
    result.final_answer["explanation"] = "I know a cultural rule."
    assert check_region_decision_success(result, _contextual_choice_gt()).pass_ is True


def test_contextual_choice_rejects_real_but_unaccepted_selection() -> None:
    result = _contextual_choice_result("Other Table", {"lat": 1.301, "lng": 103.858})
    assert check_region_decision_success(result, _contextual_choice_gt()).pass_ is False
