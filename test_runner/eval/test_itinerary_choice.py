from copy import deepcopy

import pytest

from test_runner.eval.itinerary_choice import check_itinerary_choice
from test_runner.eval.structured_output import check_required_structured_output
from test_runner.runner import _evaluate_itinerary, _append_output_contract
from test_runner.schema import AgentRunResult


def _gt():
    return {
        'expected_feasible': True, 'num_days': 2,
        'constraints': {'max_daily_budget': 100},
        'all_pois': [
            {'place_id': 'a', 'name': 'Cafe A', 'location': {'lat': 1, 'lng': 2}},
            {'place_id': 'b', 'name': 'Park B', 'location': {'lat': 3, 'lng': 4}},
            {'place_id': 'c', 'name': 'Cafe C', 'location': {'lat': 5, 'lng': 6}},
            {'place_id': 'h', 'name': 'Hotel', 'location': {'lat': 7, 'lng': 8}},
        ],
        'correct_itinerary': {'days': [
            {'day': 1, 'events': [
                {'block_kind': 'fixed_event', 'place_id': 'h'},
                {'block_kind': 'transit'},
                {'block_kind': 'poi_activity', 'place_id': 'a'},
                {'block_kind': 'personal_activity', 'place_id': 'a'},
                {'block_kind': 'poi_activity', 'place_id': 'b'},
            ]},
            {'day': 2, 'events': [{'block_kind': 'poi_activity', 'place_id': 'c'}]},
        ]},
        # Full witness arrays also include transit destinations.
        'acceptable_itineraries': [{'selected_place_ids': ['h', 'a', 'a', 'a', 'b', 'c']}],
    }


def _answer(ids=('a', 'b', 'c')):
    gt = _gt();places = {p['place_id']: p for p in gt['all_pois']}
    choices = [{'activity_order': i + 1, 'selection': {
        'name': places[pid]['name'], 'location': places[pid]['location']}}
        for i, pid in enumerate(ids[:2])]
    second = [{'activity_order': 1, 'selection': {
        'name': places[ids[2]]['name'], 'location': places[ids[2]]['location']}}] if len(ids) > 2 else []
    return {'feasible': True, 'explanation': 'The complete itinerary.',
            'plan': {'days': [{'day': 1, 'selected_pois': choices}, {'day': 2, 'selected_pois': second}]}}


def _result(answer):
    return AgentRunResult('itinerary', '', '', final_answer=answer)


def test_public_contract_passes_without_events_or_daily_budgets():
    result = _result(_answer());gt = _gt();before = deepcopy(gt)
    assert check_required_structured_output(result, 'itinerary', gt).pass_
    _evaluate_itinerary(result, gt);_append_output_contract(result, 'itinerary', gt)
    assert result.grade == 'PASS'
    assert result.total_metrics == 1
    assert len(result.metrics) == 1
    assert gt == before


@pytest.mark.parametrize('projection', [
    ['h', 'a', 'a', 'b', 'c'],  # legacy events with place IDs only
    ['a', 'b', 'c'],           # POI-only witness
])
def test_frozen_witness_projection_compatibility(projection):
    gt = _gt();gt['acceptable_itineraries'] = [{'selected_place_ids': projection}]
    assert check_itinerary_choice(_result(_answer()), gt).pass_


def test_all_frozen_alternatives_allowed_but_not_arbitrary_mixes():
    gt = _gt();gt['acceptable_itineraries'].append({'selected_place_ids': ['b', 'a', 'c']})
    assert check_itinerary_choice(_result(_answer(('b', 'a', 'c'))), gt).pass_
    assert not check_itinerary_choice(_result(_answer(('a', 'a', 'c'))), gt).pass_


@pytest.mark.parametrize('answer', [None, {}, {'feasible': True, 'explanation': 'Here is a route.'}])
def test_missing_structured_submission_fails(answer):
    assert not check_itinerary_choice(_result(answer), _gt()).pass_


@pytest.mark.parametrize('mutation', [
    lambda a: a.update(feasible=False),
    lambda a: a.update(feasible=1),
    lambda a: a.update(explanation=''),
    lambda a: a['plan']['days'].pop(),
    lambda a: a['plan']['days'].reverse(),
    lambda a: a['plan']['days'][0].update(day=True),
    lambda a: a['plan']['days'][0]['selected_pois'].pop(),
    lambda a: a['plan']['days'][0]['selected_pois'].append(deepcopy(a['plan']['days'][0]['selected_pois'][0])),
    lambda a: a['plan']['days'][0]['selected_pois'].reverse(),
    lambda a: a['plan']['days'][0]['selected_pois'][0].update(activity_order=True),
    lambda a: a['plan']['days'][0]['selected_pois'][0]['selection'].update(name='OFFLINE_NONEXISTENT_PLACE'),
    lambda a: a['plan']['days'][0]['selected_pois'][0]['selection'].update(location={'lat': 0, 'lng': 0}),
    lambda a: a['plan']['days'][0]['selected_pois'][0]['selection'].update(location={'lat': True, 'lng': 2}),
    lambda a: a['plan']['days'][0]['selected_pois'][0]['selection'].update(location={'lat': float('nan'), 'lng': 2}),
    lambda a: a['plan']['days'][0]['selected_pois'][0]['selection'].update(location={'lat': 100, 'lng': 2}),
    lambda a: a['plan']['days'][0]['selected_pois'][0]['selection'].pop('location'),
    lambda a: a['plan']['days'][0].pop('selected_pois'),
])
def test_invalid_public_choices_cannot_be_outvoted(mutation):
    answer = deepcopy(_answer());mutation(answer);result = _result(answer)
    _evaluate_itinerary(result, _gt());_append_output_contract(result, 'itinerary', _gt())
    assert result.grade == 'FAIL'
    assert result.passed_metrics == 0


def test_same_number_of_choices_in_wrong_days_fails():
    answer = _answer(('a', 'c', 'b'))
    assert not check_itinerary_choice(_result(answer), _gt()).pass_


def test_explanation_and_old_events_cannot_fill_in_missing_public_choices():
    answer = _answer();answer['plan']['days'][0].pop('selected_pois')
    answer['plan']['days'][0]['events'] = _gt()['correct_itinerary']['days'][0]['events']
    answer['explanation'] = 'Choose Cafe A and Park B, then Cafe C.'
    assert not check_itinerary_choice(_result(answer), _gt()).pass_


def test_personal_only_day_requires_empty_choice_list():
    gt = _gt();gt['correct_itinerary']['days'][1]['events'][0]['block_kind'] = 'personal_activity'
    answer = _answer();answer['plan']['days'][1]['selected_pois'] = []
    assert check_itinerary_choice(_result(answer), gt).pass_


def test_legacy_fixed_and_slot_markers_project_same_public_choices():
    gt = _gt()
    for day in gt['correct_itinerary']['days']:
        day['events'] = [event for event in day['events'] if event['block_kind'] != 'transit']
        for index, event in enumerate(day['events']):
            kind = event.pop('block_kind')
            event['is_fixed'] = kind != 'poi_activity'
            event['slot_id'] = f'slot_{day["day"]}_{index}' if kind == 'poi_activity' else None
    gt['acceptable_itineraries'] = [{'selected_place_ids': ['h', 'a', 'a', 'b', 'c']}]
    assert check_itinerary_choice(_result(_answer()), gt).pass_


def test_missing_choice_classification_is_not_guessed_from_event_name():
    gt = _gt();gt['correct_itinerary']['days'][0]['events'][2] = {'event_type': 'restaurant', 'place_id': 'a'}
    with pytest.raises(ValueError, match='classification'):
        check_itinerary_choice(_result(_answer()), gt)


def test_duplicate_public_identity_is_unverifiable():
    gt = _gt();duplicate = deepcopy(gt['all_pois'][0]);duplicate['place_id'] = 'other';gt['all_pois'].append(duplicate)
    assert not check_itinerary_choice(_result(_answer()), gt).pass_


@pytest.mark.parametrize('change', [
    lambda g: g.update(expected_feasible='true'),
    lambda g: g.pop('correct_itinerary'),
    lambda g: g['acceptable_itineraries'][0].update(selected_place_ids=['a']),
    lambda g: g['all_pois'].pop(0),
])
def test_bad_frozen_evidence_raises_instead_of_blames_model(change):
    gt = _gt();change(gt)
    with pytest.raises(ValueError):check_itinerary_choice(_result(_answer()), gt)


def test_infeasible_boolean_gt_accepts_partial_public_choices():
    gt = _gt();gt['expected_feasible'] = False
    answer = _answer(('a',));answer['feasible'] = False;answer['explanation'] = 'Remaining requirements cannot be met.'
    assert check_itinerary_choice(_result(answer), gt).pass_
    answer['feasible'] = True
    assert not check_itinerary_choice(_result(answer), gt).pass_
