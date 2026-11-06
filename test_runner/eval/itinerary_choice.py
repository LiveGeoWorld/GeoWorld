"""Static Itinerary judging for the public ordered selected_pois contract.

Accept frozen witnesses or verify alternatives against public frozen constraints.
"""
from __future__ import annotations
import math
from typing import Any
from test_runner.schema import AgentRunResult, MetricResult

def _name(value: str) -> str:
    return ' '.join(value.casefold().split())

def _location(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    coordinates = [value.get(key) for key in ('lat', 'lng')]
    if any((type(number) not in (int, float) or not math.isfinite(number) for number in coordinates)):
        return None
    (lat, lng) = coordinates
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return (lat, lng)

def _places(value: Any):
    if isinstance(value, dict):
        if value.get('place_id') and isinstance(value.get('name'), str) and _location(value.get('location')):
            yield value
        else:
            for child in value.values():
                yield from _places(child)
    elif isinstance(value, list):
        for child in value:
            yield from _places(child)

def public_output_errors(answer: Any) -> list[str]:
    """Validate only fields in the already published model output contract."""
    if not isinstance(answer, dict) or not answer:
        return ['missing_final_answer']
    errors = []
    if type(answer.get('feasible')) is not bool:
        errors.append('feasible_must_be_boolean')
    if not isinstance(answer.get('explanation'), str) or not answer['explanation'].strip():
        errors.append('missing_explanation')
    plan = answer.get('plan')
    days = plan.get('days') if isinstance(plan, dict) else None
    if not isinstance(days, list) or not days:
        return errors + ['missing_plan_days']
    for (index, day) in enumerate(days, 1):
        if not isinstance(day, dict):
            errors.append(f'day_{index}_must_be_object')
            continue
        if type(day.get('day')) is not int or day['day'] != index:
            errors.append(f'day_{index}_invalid_day_order')
        rows = day.get('selected_pois')
        if not isinstance(rows, list):
            errors.append(f'day_{index}_missing_selected_pois')
            continue
        for (order, row) in enumerate(rows, 1):
            prefix = f'day_{index}_activity_{order}'
            if not isinstance(row, dict):
                errors.append(prefix + '_must_be_object')
                continue
            if type(row.get('activity_order')) is not int or row['activity_order'] != order:
                errors.append(prefix + '_invalid_activity_order')
            selection = row.get('selection')
            if not isinstance(selection, dict) or not isinstance(selection.get('name'), str) or (not selection['name'].strip()) or (_location(selection.get('location')) is None):
                errors.append(prefix + '_invalid_public_selection')
    return errors

def _is_poi_choice(event: dict) -> bool:
    if event.get('block_kind'):
        return event['block_kind'] == 'poi_activity'
    if event.get('is_fixed') is True:
        return False
    if event.get('is_fixed') is False and event.get('slot_id') and event.get('place_id'):
        return True
    raise ValueError('Itinerary GT event lacks frozen fixed/choice classification')

def _frozen_choices(gt: dict) -> tuple[list[int], set[tuple[tuple[str, ...], ...]]]:
    days = (gt.get('correct_itinerary') or {}).get('days')
    if not isinstance(days, list) or not days:
        raise ValueError('Itinerary GT has no frozen day-aligned witness')
    events_by_day = []
    for (number, day) in enumerate(days, 1):
        if not isinstance(day, dict) or type(day.get('day')) is not int or day['day'] != number or (not isinstance(day.get('events'), list)) or any((not isinstance(event, dict) for event in day['events'])):
            raise ValueError('Itinerary GT has invalid day/event alignment')
        events_by_day.append(day['events'])
    if gt.get('num_days', len(days)) != len(days):
        raise ValueError('Itinerary GT num_days differs from witness')
    counts = [sum((_is_poi_choice(event) for event in events)) for events in events_by_day]
    all_events = [event for events in events_by_day for event in events]
    place_events = [event for event in all_events if event.get('place_id')]
    poi_events = [event for event in all_events if _is_poi_choice(event)]
    acceptable = gt.get('acceptable_itineraries')
    if acceptable is None or acceptable == []:
        acceptable = [{'selected_place_ids': [event.get('place_id') for event in poi_events]}]
    if not isinstance(acceptable, list):
        raise ValueError('Itinerary GT acceptable_itineraries must be a list')
    known = {str(place['place_id']) for place in _places(gt.get('all_pois', []))}
    choices = set()
    for candidate in acceptable:
        ids = candidate.get('selected_place_ids') if isinstance(candidate, dict) else None
        if not isinstance(ids, list) or any((not isinstance(pid, str) or not pid for pid in ids)):
            raise ValueError('Itinerary GT has an invalid acceptable place sequence')
        if len(ids) == len(all_events):
            projected = [pid for (pid, event) in zip(ids, all_events) if _is_poi_choice(event)]
        elif len(ids) == len(place_events):
            projected = [pid for (pid, event) in zip(ids, place_events) if _is_poi_choice(event)]
        elif len(ids) == len(poi_events):
            projected = ids
        else:
            raise ValueError('Itinerary GT acceptable sequence cannot be aligned to frozen activities')
        if any((pid not in known for pid in projected)):
            raise ValueError('Itinerary GT acceptable choice is missing its frozen public place facts')
        (grouped, offset) = ([], 0)
        for count in counts:
            grouped.append(tuple(projected[offset:offset + count]))
            offset += count
        choices.add(tuple(grouped))
    if not choices:
        raise ValueError('Itinerary GT has no acceptable choice witness')
    return (counts, choices)

def evaluate_itinerary_choice(result: AgentRunResult, gt: dict, *, task_dir=None) -> dict[str, Any]:
    if type(gt.get('expected_feasible')) is not bool:
        raise ValueError('Itinerary GT must contain boolean expected_feasible')
    (counts, choices) = _frozen_choices(gt) if gt['expected_feasible'] else ([], set())
    errors = public_output_errors(result.final_answer)
    report = {'success': False, 'errors': errors, 'expected_day_activity_counts': counts}
    if errors:
        return report
    answer = result.final_answer
    if answer['feasible'] != gt['expected_feasible']:
        errors.append('feasibility_mismatch')
        return report
    days = answer['plan']['days']
    if task_dir is not None:
        from test_runner.eval.itinerary_identity import load_identity_places
        (world_places, binding) = load_identity_places(task_dir)
        places = list(_places(world_places))
        result.evaluation_world_snapshot['itinerary_identity'] = binding
    else:
        places = list(_places(gt.get('all_pois', [])))
        if gt.get('hotel'):
            places.extend(_places(gt['hotel']))
    actual = []
    for (number, day) in enumerate(days, 1):
        selected = []
        for (order, row) in enumerate(day['selected_pois'], 1):
            selection = row['selection']
            (lat, lng) = _location(selection['location'])
            matches = {str(place['place_id']) for place in places if _name(place['name']) == _name(selection['name']) and abs(_location(place['location'])[0] - lat) <= 1e-05 and (abs(_location(place['location'])[1] - lng) <= 1e-05)}
            if len(matches) != 1:
                errors.append(f'day_{number}_activity_{order}_place_not_uniquely_resolved')
            else:
                selected.append(matches.pop())
        actual.append(tuple(selected))
    if gt['expected_feasible']:
        if len(days) != len(counts):
            errors.append('day_count_mismatch')
        elif [len(day['selected_pois']) for day in days] != counts:
            errors.append('activity_count_mismatch')
        if not errors and tuple(actual) not in choices:
            from test_runner.eval.itinerary_alternatives import verify_alternative
            alternative = verify_alternative(result, gt, actual, task_dir=task_dir)
            report['alternative'] = alternative
            result.evaluation_world_snapshot['itinerary_alternative'] = alternative
            if alternative['status'] != 'verified':
                report['verification_status'] = alternative['status']
                errors.extend(('alternative_' + i['status'] + ':' + i['code'] + (f"[day={i['day']},activity={i['activity_order']}]" if 'activity_order' in i else '') for i in alternative['issues']))
    report['success'] = not errors
    return report

def check_itinerary_choice(result: AgentRunResult, gt: dict, *, task_dir=None) -> MetricResult:
    report = evaluate_itinerary_choice(result, gt, task_dir=task_dir)
    return MetricResult('itinerary_selection_correct', '有序地点选择（主指标）', report['success'], ('替代行程通过公开约束与完整时间线核验' if report.get('alternative') else '公开选择匹配冻结可接受行程') if report['success'] else '; '.join(report['errors']), verification_status=report.get('verification_status', ''))
