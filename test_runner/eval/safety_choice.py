"""Private fixed-choice evaluation and per-constraint failure attribution."""
from __future__ import annotations
import json
import math
from typing import Any
from test_runner.verification.region_aware.generator import _region_constraint_matches_place, _region_district_names, _region_weekday_index, _stage3_route_for, _stage3_route_index
from test_runner.verification.safety_planning.event_contract import apply_event
from test_runner.verification.safety_planning.task_time import moment
from test_runner.verification.safety_planning.opening_hours import open_at
from test_runner.schema import AgentRunResult, MetricResult

def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)

def _contains_notice(value: Any, notice: dict) -> bool:
    if isinstance(value, dict):
        if all((value.get(key) == expected for (key, expected) in notice.items())):
            return True
        return any((_contains_notice(item, notice) for item in value.values()))
    if isinstance(value, list):
        return any((_contains_notice(item, notice) for item in value))
    return False

def _same_place(selection: Any, place: dict) -> bool:
    if not isinstance(selection, dict) or selection.get('name') != place.get('name'):
        return False
    (left, right) = (selection.get('location'), place.get('location'))
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return all((type(left.get(key)) in (int, float) and math.isfinite(left[key]) and (left[key] == right.get(key)) for key in ('lat', 'lng')))

def evaluate_safety_choice(result: AgentRunResult, gt_data: dict) -> dict[str, Any]:
    decision = gt_data['safety_choice']
    answer = result.final_answer or {}
    plan = answer.get('plan') if isinstance(answer.get('plan'), dict) else {}
    places = decision['world']['places']
    matches = [place for place in places if _same_place(plan.get('selection'), place)]
    report: dict[str, Any] = {'poi_correct': False, 'grounding_correct': False, 'constraint_checks': [], 'safety_checks': [], 'errors': []}
    if len(matches) != 1:
        report['errors'].append('selection_does_not_resolve_to_one_frozen_place')
    else:
        selected = matches[0]
        pid = selected['place_id']
        report['poi_correct'] = pid == decision['target_place_id']
        context = {'character': decision['character'], 'route_index': _stage3_route_index(decision['route_cache']), 'district_names': _region_district_names(decision['world']), 'weekday_index': _region_weekday_index(decision['time_context'])}
        report['constraint_checks'].append({'constraint_id': 'activity_intent', 'satisfied': pid in decision['activity_place_ids']})
        for constraint in decision['public_place_constraints']:
            report['constraint_checks'].append({'constraint_id': constraint['constraint_id'], 'condition': constraint, 'satisfied': _region_constraint_matches_place(selected, constraint, **context)})
        profiles = decision['profiles_by_place_id']
        if 'task_time' in decision:
            report['constraint_checks'].append({'constraint_id': 'open_at_task_time', 'satisfied': open_at(selected, decision['task_time']) is True})
        if pid not in profiles:
            report['safety_checks'].append({'reason': 'missing_exposure_facts', 'satisfied': None})
        else:
            event_records = decision['events']
            if decision.get('event_contract') in {'safety_events.v1', 'safety_events.v2'}:
                event_records = [{'event': {'events': [r['event'] for r in event_records]}, 'scope_place_ids': [set(r['scope_place_ids']) for r in event_records]}]
            for event_record in event_records:
                effects = apply_event(event_record['event'], [{'candidate_ref': pid, 'facts': profiles[pid]}], scope_members=event_record['scope_place_ids'] if 'events' in event_record['event'] else set(event_record['scope_place_ids']), activity_window=moment(decision))[pid]
                report['safety_checks'].append({'reason': 'activity_available', 'satisfied': None if effects['unknown_effects'] else not effects['unavailable'], 'effects': effects})
                for constraint in decision['public_place_constraints']:
                    if constraint['type'] != 'route_duration_range':
                        continue
                    mode = constraint['transportation_mode']
                    entry = _stage3_route_for(context['route_index'], decision['character'], selected, mode)
                    raw = ((entry or {}).get('route') or {}).get('duration_s')
                    baseline = max(1, math.ceil(float(raw) / 60)) if raw is not None else None
                    effective = baseline + effects['delay_minutes'].get(mode, 0) if baseline is not None else None
                    report['safety_checks'].append({'constraint_id': constraint['constraint_id'], 'reason': 'event_adjusted_route', 'baseline_minutes': baseline, 'effective_minutes': effective, 'minimum': constraint['minimum'], 'maximum': constraint['maximum'], 'satisfied': None if effective is None or effects['unknown_effects'] else constraint['minimum'] <= effective <= constraint['maximum']})
    information = plan.get('safety_information')
    valid_information = isinstance(information, list) and bool(information) and all((isinstance(item, str) and item.strip() for item in information))
    submitted = {item for item in information if isinstance(item, str) and item.strip()} if isinstance(information, list) else set()
    observed = {text for record in result.trace if not str(record.tool).endswith('final_answer') for text in _strings(record.result)}
    required = {row['public_event']['description'] for row in decision['events']}
    notices_observed = all((any((str(record.tool).startswith(row['event']['app'] + '__') and _contains_notice(record.result, row['public_event']) for record in result.trace)) for row in decision['events']))
    report['grounding_correct'] = valid_information and bool(required) and (required <= submitted) and (submitted <= observed) and notices_observed
    if not notices_observed:
        report['errors'].append('relevant_safety_information_not_observed')
    if not required <= submitted:
        report['errors'].append('relevant_safety_information_not_submitted')
    if not submitted <= observed:
        report['errors'].append('submitted_information_not_in_tool_results')
    checks = report['constraint_checks'] + report['safety_checks']
    report['passed'] = answer.get('feasible') is True and report['poi_correct'] and report['grounding_correct'] and bool(checks) and all((row['satisfied'] is True for row in checks))
    return report

def check_safety_choice(result: AgentRunResult, gt_data: dict) -> MetricResult:
    try:
        report = evaluate_safety_choice(result, gt_data)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        report = {'passed': False, 'error': 'invalid_frozen_safety_choice_data', 'detail': str(exc)}
    return MetricResult('safety_choice_success', 'Safety choice and grounding', report['passed'], json.dumps(report, ensure_ascii=False))
