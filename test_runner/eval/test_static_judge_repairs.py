"""Regression cases for fact-preserving static evaluator implementation repairs."""
import pytest
from test_runner.schema import AgentRunResult, ToolCallRecord
from test_runner.eval.itinerary_metrics import check_time_window_valid, check_tool_efficiency
from test_runner.eval.multi_entity_metrics import check_efficiency
from test_runner.eval.safety_metrics import check_tool_efficiency as safety_efficiency
from test_runner.eval.region_metrics import check_tool_efficiency as region_efficiency
from test_runner.eval.multi_entity_metrics import check_restaurant_match
from test_runner.eval.metrics import check_walking_constraint, check_plan_structure


def _result(answer=None, trace=None):
    return AgentRunResult('regression', '', '', final_answer=answer, trace=trace or [])


@pytest.mark.parametrize('opening', [
    {'opening_open': '09:00', 'opening_close': '17:00'},
    {'opening': '09:00-17:00'},
])
@pytest.mark.parametrize('start,end,passed', [
    ('08:30', '09:30', False), ('16:30', '17:30', False),
    ('09:00', '17:00', True), ('12:00', '13:00', True),
])
def test_opening_fields_and_closing_boundary(opening, start, end, passed):
    result = _result({'plan': {'days': [{'events': [{'place_id': 'p', 'start': start, 'end': end}]}]}})
    gt = {'all_pois': [{'place_id': 'p', **opening}]}
    metric = check_time_window_valid(result, gt)
    assert metric.pass_ is passed
    assert '跳过' not in metric.detail


@pytest.mark.parametrize('start,end,passed', [
    ('23:00', '01:00', True), ('01:00', '02:00', True),
    ('21:00', '23:00', False), ('01:00', '03:00', False),
])
def test_overnight_opening_interval(start, end, passed):
    result = _result({'plan': {'days': [{'events': [{'place_id': 'p', 'start': start, 'end': end}]}]}})
    gt = {'all_pois': [{'place_id': 'p', 'opening_open': '22:00', 'opening_close': '02:00'}]}
    assert check_time_window_valid(result, gt).pass_ is passed


@pytest.mark.parametrize('judge,limit', [(check_tool_efficiency, 15), (check_efficiency, 10), (safety_efficiency, 12), (region_efficiency, 12)])
def test_final_submission_namespace_does_not_change_efficiency(judge, limit):
    trace = [ToolCallRecord(i, '', 'MapApp__get_place_detail') for i in range(limit)]
    for name in ['final_answer', 'UserInterfaceApp__final_answer']:
        result = _result(trace=trace + [ToolCallRecord(limit, '', name)])
        assert judge(result).pass_ is True
    assert judge(_result(trace=trace + [ToolCallRecord(limit, '', 'MapApp__get_place_detail')])).pass_ is False


def test_legacy_route_reference_preserves_observed_walking_and_availability():
    result = _result({'feasible': True, 'route_ids': ['r']}, [
        ToolCallRecord(1, '', 'MapApp__query_routes', {'origin_id': 'a', 'dest_id': 'b'},
            {'routes': [{'route_id': 'r', 'mode': 'TRANSIT', 'distance_m': 5000,
                         'duration_s': 600, 'walking_distance_m': 800, 'available': False}]})])
    gt = {'constraints': {'max_walking_distance_m': 500}, 'core_places': []}
    walking = check_walking_constraint(result, gt)
    assert walking.pass_ is False
    assert '800m' in walking.detail
    assert check_plan_structure(result, gt).pass_ is False
    # A sufficient walking limit is verifiable from the trace, without GT route fill-in.
    gt['constraints']['max_walking_distance_m'] = 900
    assert check_walking_constraint(result, gt).pass_ is True


@pytest.mark.parametrize('answer,passed', [
    (None, False), ({}, False), ({'feasible': True}, False),
    ({'feasible': 0}, False), ({'feasible': 'false'}, False),
    ({'feasible': False}, True),
])
def test_legacy_infeasible_multi_entity_requires_actual_boolean_decision(answer, passed):
    assert check_restaurant_match(_result(answer), {'expected_feasible': False}).pass_ is passed
