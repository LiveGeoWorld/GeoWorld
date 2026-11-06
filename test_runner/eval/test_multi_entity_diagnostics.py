"""Regression tests for the approved public coordination-evidence contract."""
import copy
import pytest
from test_runner.schema import AgentRunResult, ToolCallRecord
from test_runner.eval.multi_entity_metrics import (
    check_assignment_complete, check_commute_feasible, check_notification_complete,
)

GT = {'expected_feasible': True, 'participants': [
    {'character_id': 'a', 'name': 'Alice'}, {'character_id': 'b', 'name': 'Bob'}],
    'constraints': {'max_commute_min': 10}}


def result(plan=None, trace=None, **answer):
    return AgentRunResult('test', '', '', final_answer={'plan': plan or {}, **answer}, trace=trace or [])


@pytest.mark.parametrize('prefix', ['', 'MessagingApp__'])
@pytest.mark.parametrize('names', [('a', 'b'), ('Alice', ' bob ')])
def test_notification_namespaces_and_public_names(prefix, names):
    trace = [ToolCallRecord(i, '', prefix+'send_message', {'recipient_id': n}, {'status': 'sent'})
             for i, n in enumerate(names)]
    assert check_notification_complete(result(trace=trace), GT).pass_


def test_group_requires_success_and_ignores_wrong_app():
    for tool, status, passed in [('MessagingApp__create_group', 'created', True),
                                 ('create_group', 'error', False),
                                 ('OtherApp__create_group', 'created', False)]:
        r = result(trace=[ToolCallRecord(1, '', tool, {'member_ids': ['Alice', 'Bob']}, {'status': status})])
        assert check_notification_complete(r, GT).pass_ is passed


def test_ambiguous_notification_name_cannot_cover_two_people():
    gt = copy.deepcopy(GT); gt['participants'][1]['name'] = 'Alice'
    r = result(trace=[ToolCallRecord(1, '', 'send_message', {'recipient_id': 'Alice'}, {'status': 'sent'})])
    assert not check_notification_complete(r, gt).pass_


@pytest.mark.parametrize('field', ['assignments', 'participants', 'pickup_order'])
def test_assignment_accepts_explicit_public_lists(field):
    assert check_assignment_complete(result({field: ['Alice', {'name': 'Bob'}]}), GT).pass_


@pytest.mark.parametrize('entries', [[], ['Alice'], ['Alice', 'Alice'], ['Alice', 'Bob', 'Eve'],
                                     ['Alice will arrive', 'Bob'], [{'character_id': 'a', 'name': 'Bob'}, 'Bob']])
def test_incomplete_duplicate_extra_conflicting_and_prose_assignments_do_not_pass(entries):
    assert not check_assignment_complete(result({'assignments': entries}), GT).pass_


def test_assignment_does_not_fall_back_past_bad_primary_list_or_read_explanation():
    assert not check_assignment_complete(result({'assignments': [], 'participants': ['Alice', 'Bob']}), GT).pass_
    assert not check_assignment_complete(result(explanation='Alice and Bob are both arranged'), GT).pass_


def commute_case(seconds=300):
    destination = {'name': 'Cafe', 'location': {'lat': 3, 'lng': 4}}
    trace, entries = [], []
    for i, name in enumerate(['Alice', 'Bob']):
        origin = {'name': name+' home', 'location': {'lat': i, 'lng': 1}}
        leg = {'origin': origin, 'destination': destination, 'mode': 'DRIVE'}
        entries.append({'name': name, 'legs': [leg]})
        trace.append(ToolCallRecord(i, '', 'MapApp__query_routes',
                                    {'origin': origin, 'destination': destination},
                                    {'routes': [{'mode': 'DRIVE', 'duration_s': seconds}]}))
    return result({'participants': entries}, trace)


def test_observed_per_person_commute_sums_seconds_and_preserves_inputs():
    r = commute_case(); before = copy.deepcopy(r)
    assert check_commute_feasible(r, GT).pass_
    assert r == before
    assert not check_commute_feasible(commute_case(301), GT).pass_


def test_querying_routes_without_selecting_is_unverifiable():
    r = commute_case(); r.final_answer = {'plan': {}, 'explanation': 'Choose the shortest route for everyone'}
    m = check_commute_feasible(r, GT)
    assert not m.pass_ and m.verification_status == 'unverifiable'


def test_ambiguous_same_mode_routes_no_gt_or_shortest_fallback():
    r = commute_case(); r.trace[0].result['routes'].append({'mode': 'DRIVE', 'duration_s': 600})
    assert check_commute_feasible(r, GT).verification_status == 'unverifiable'


def test_duplicate_observation_is_not_a_second_route():
    r = commute_case(); r.trace.append(copy.deepcopy(r.trace[0]))
    assert check_commute_feasible(r, GT).pass_


@pytest.mark.parametrize('duration', [None, float('nan'), 0, -1, True])
def test_missing_or_invalid_route_duration_cannot_pass(duration):
    r = commute_case(); r.trace[0].result['routes'][0]['duration_s'] = duration
    assert not check_commute_feasible(r, GT).pass_


def test_unavailable_route_fails():
    r = commute_case(); r.trace[0].result['routes'][0]['available'] = False
    assert not check_commute_feasible(r, GT).pass_


@pytest.mark.parametrize('estimate,passed', [(10, True), (11, False), (-2, False), ('3', False), (True, False), (float('inf'), False)])
def test_legacy_commute_estimate(estimate, passed):
    assert check_commute_feasible(result({'total_commute_min': estimate}), GT).pass_ is passed


def test_missing_final_answer_and_infeasible_branch():
    r = AgentRunResult('t', '', '')
    assert not check_commute_feasible(r, GT).pass_
    assert not check_assignment_complete(r, GT).pass_
    assert check_commute_feasible(r, {'expected_feasible': False}).pass_


def test_explicit_shared_mode_is_available_to_person_legs():
    r = commute_case()
    r.final_answer['mode'] = 'DRIVE'
    for p in r.final_answer['plan']['participants']:
        del p['legs'][0]['mode']
    assert check_commute_feasible(r, GT).pass_


def test_disconnected_person_legs_fail_without_reordering():
    r = commute_case()
    # Bob's home does not connect to Alice's first leg destination.
    r.final_answer['plan']['participants'][0]['legs'].append(
        copy.deepcopy(r.final_answer['plan']['participants'][1]['legs'][0]))
    metric = check_commute_feasible(r, GT)
    assert not metric.pass_ and '不连续' in metric.detail


@pytest.mark.parametrize('sentinel', ['none', 'NONE', ' none ', '无', '', None])
def test_dietary_no_restriction_sentinel(sentinel):
    from test_runner.eval.multi_entity_metrics import check_dietary_satisfied
    gt = {'constraints': {'must_accommodate_all_dietary': True},
          'participants': [{'dietary': sentinel}, {'dietary': 'vegetarian'}],
          'candidate_places': [{'name': 'Cafe', 'dietary_options': ['vegetarian']}]}
    r = result({'selection': {'name': 'Cafe'}})
    assert check_dietary_satisfied(r, gt).pass_
    gt['candidate_places'][0]['dietary_options'] = []
    assert not check_dietary_satisfied(r, gt).pass_
    gt['participants'] = [{'dietary': sentinel}]
    assert check_dietary_satisfied(r, gt).pass_
    r.final_answer = None
    assert not check_dietary_satisfied(r, gt).pass_
    gt['constraints']['must_accommodate_all_dietary'] = False
    assert check_dietary_satisfied(r, gt).pass_
