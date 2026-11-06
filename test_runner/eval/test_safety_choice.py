import copy
import json

import pytest

from test_runner.eval.safety_choice import evaluate_safety_choice, check_safety_choice
from test_runner.schema import AgentRunResult, ToolCallRecord


def fixture():
    places = [{"place_id": pid, "name": pid, "primary_type": "gym", "district_id": "d",
               "location": {"lat": 1.0, "lng": float(index)}} for index, pid in enumerate(("safe", "unsafe"))]
    window = {"start": "2026-08-04T15:00:00+08:00", "end": "2026-08-04T16:00:00+08:00"}
    event = {"event_kind": "mobility", "app": "CityNewsApp", "active_window": window,
             "effects": [{"kind": "delay", "where": [], "delay_minutes": 8,
                          "transportation_mode": "DRIVE", "reason": "Roadworks"}]}
    public = {"description": "Roadworks on Test Road add eight minutes for drivers from 14:00 to 17:00."}
    decision = {"target_place_id": "safe", "world": {"places": places, "districts": []},
                "character": {"character_id": "person"}, "activity_place_ids": ["safe", "unsafe"],
                "activity_window": window, "time_context": {"instruction_time": window["start"]},
                "public_place_constraints": [{"constraint_id": "commute", "type": "route_duration_range",
                                             "minimum": 0, "maximum": 20, "transportation_mode": "DRIVE"}],
                "profiles_by_place_id": {"safe": {}, "unsafe": {}},
                "events": [{"event": event, "public_event": public, "scope_place_ids": ["unsafe"]}],
                "route_cache": {"cache": [
                    {"origin_place_id": "character:person", "dest_place_id": pid, "mode": "DRIVE",
                     "route": {"available": True, "duration_s": 17 * 60}} for pid in ("safe", "unsafe")]}}
    result = AgentRunResult(task_id="fixture", user_prompt="test", gt_route_id="")
    result.final_answer = {"feasible": True, "plan": {
        "selection": {key: places[0][key] for key in ("name", "location")},
        "safety_information": [public["description"]]}, "explanation": "The chosen route avoids the roadworks."}
    result.trace = [ToolCallRecord(seq=1, timestamp="", tool="CityNewsApp__get_info", args={}, result=[public])]
    return result, {"safety_choice": decision}


def test_correct_poi_and_observed_information_pass():
    result, gt = fixture()
    assert check_safety_choice(result, gt).pass_


@pytest.mark.parametrize("changed", [
    "Between 14:00 and 17:00, roadworks on Test Road cause an eight-minute driving delay.",
    "Roadworks on Test Road add eight minutes for drivers from 14:00 to 17:00",
    "roadworks on Test Road add eight minutes for drivers from 14:00 to 17:00.",
    "Roadworks  on Test Road add eight minutes for drivers from 14:00 to 17:00.",
    "Roadworks on Test Road add eight minutes for drivers from 14:00 to 17:00.\n",
])
def test_notice_must_match_exactly_not_only_meaning(changed):
    result, gt = fixture()
    result.final_answer["plan"]["safety_information"] = [changed]
    report = evaluate_safety_choice(result, gt)
    assert report["poi_correct"]
    assert not report["grounding_correct"]
    assert not report["passed"]


def test_exact_copy_survives_json_escaping():
    result, gt = fixture()
    description = 'Roadworks on "Test Road".\nAllow 8 extra minutes.  Check before travel.'
    gt["safety_choice"]["events"][0]["public_event"]["description"] = description
    result.final_answer["plan"]["safety_information"] = [description]
    result.final_answer = json.loads(json.dumps(result.final_answer))
    assert evaluate_safety_choice(result, gt)["passed"]


def test_guessed_correct_poi_without_observation_fails():
    result, gt = fixture()
    result.trace = []
    report = evaluate_safety_choice(result, gt)
    assert report["poi_correct"]
    assert not report["passed"]
    assert "relevant_safety_information_not_observed" in report["errors"]


def test_wrong_poi_reports_event_caused_time_failure():
    result, gt = fixture()
    place = gt["safety_choice"]["world"]["places"][1]
    result.final_answer["plan"]["selection"] = {key: place[key] for key in ("name", "location")}
    report = evaluate_safety_choice(result, gt)
    assert not report["passed"]
    assert all(check["satisfied"] for check in report["constraint_checks"])
    route = next(check for check in report["safety_checks"] if check["reason"] == "event_adjusted_route")
    assert route["baseline_minutes"] == 17
    assert route["effective_minutes"] == 25
    assert route["satisfied"] is False


def test_ordinary_constraint_failure_is_separate():
    result, gt = fixture()
    gt["safety_choice"]["public_place_constraints"].append({
        "constraint_id": "area", "type": "district_name", "value": "another district"})
    report = evaluate_safety_choice(result, gt)
    assert not report["passed"]
    assert report["constraint_checks"][-1]["constraint_id"] == "area"
    assert report["constraint_checks"][-1]["satisfied"] is False


def test_information_must_be_submitted_not_merely_observed():
    result, gt = fixture()
    result.final_answer["plan"]["safety_information"] = []
    assert not evaluate_safety_choice(result, gt)["passed"]


def test_fabricated_information_and_self_echo_do_not_count():
    result, gt = fixture()
    result.final_answer["plan"]["safety_information"].append("invented")
    assert not evaluate_safety_choice(result, gt)["passed"]
    result, gt = fixture()
    result.trace[0].tool = "UserInterfaceApp__final_answer"
    assert not evaluate_safety_choice(result, gt)["passed"]


def test_missing_facts_do_not_become_safe_defaults():
    result, gt = fixture()
    original = copy.deepcopy(gt)
    del gt["safety_choice"]["profiles_by_place_id"]["safe"]
    assert not evaluate_safety_choice(result, gt)["passed"]
    assert "safe" in original["safety_choice"]["profiles_by_place_id"]


def test_notice_echo_from_unrelated_app_does_not_establish_grounding():
    result, gt = fixture()
    result.trace[0].tool = "MessagingApp__send_message"
    assert not evaluate_safety_choice(result, gt)["passed"]


def test_malformed_information_does_not_pass_by_ignoring_bad_entries():
    result, gt = fixture()
    result.final_answer["plan"]["safety_information"].append(5)
    assert not evaluate_safety_choice(result, gt)["passed"]


def test_runner_uses_single_joint_success_metric():
    from test_runner.runner import _evaluate_safety, _append_output_contract
    result, gt = fixture()
    result.trace = []
    _evaluate_safety(result, gt)
    _append_output_contract(result, "safety_planning", gt)
    assert result.total_metrics == 1
    assert result.passed_metrics == 0
