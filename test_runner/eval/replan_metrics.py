"""Strict metrics for persistent dynamic replanning episodes."""
from __future__ import annotations
from typing import Any
from test_runner.verification.dynamic_replanning.public_evidence import public_event_message
from test_runner.schema import AgentRunResult, MetricResult

def _proof(gt_data: dict[str, Any]) -> dict[str, Any]:
    proof = gt_data.get('counterfactual_proof', {})
    return proof if isinstance(proof, dict) else {}

def _base_task_type(gt_data: dict[str, Any]) -> str:
    return str(_proof(gt_data).get('source', {}).get('task_type') or '')

def _dynamic_validation(gt_data: dict[str, Any]) -> dict[str, Any]:
    value = gt_data.get('_dynamic_proof_validation', {})
    return value if isinstance(value, dict) else {'valid': False, 'errors': ['missing_validation']}

def check_transition_proof_valid(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Fail closed if a Dynamic task lacks a verified private P0-to-P1 proof."""
    validation = _dynamic_validation(gt_data)
    if validation.get('valid') is True:
        return MetricResult('transition_proof_valid', 'Transition proof', True, f'verified {_base_task_type(gt_data)} P0-to-P1 transition')
    errors = validation.get('errors') or ['invalid_dynamic_counterfactual_proof']
    return MetricResult('transition_proof_valid', 'Transition proof', False, ', '.join((str(error) for error in errors[:3])))

def _answer(result: AgentRunResult) -> dict[str, Any] | None:
    return result.final_answer

def _events(gt_data: dict[str, Any]) -> list[dict[str, Any]]:
    events = gt_data.get('_dynamic_events') or gt_data.get('events') or gt_data.get('disruptions') or []
    return [event for event in events if isinstance(event, dict)]

def _base_gt(gt_data: dict[str, Any]) -> dict[str, Any]:
    return gt_data.get('base_gt') or gt_data.get('gt') or {}

def _has_plan(answer: dict[str, Any]) -> bool:
    return bool(answer.get('route_ids') or answer.get('plan'))

def _plan_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    plan = payload.get('plan', {})
    return plan if isinstance(plan, dict) else {}

def _selected_multi_entity_service(payload: dict[str, Any] | None, gt_data: dict[str, Any]) -> str:
    """Resolve a public name/location selection to a private frozen service ID."""
    plan = _plan_payload(payload)
    name = str(plan.get('restaurant_name') or (payload or {}).get('restaurant_name') or '').strip()
    location = plan.get('restaurant_location') or (payload or {}).get('restaurant_location')
    if not name:
        return ''
    matches = [item for item in gt_data.get('candidate_restaurants', []) if str(item.get('name') or '').strip().casefold() == name.casefold()]
    if isinstance(location, dict):
        matches = [item for item in matches if _same_location(location, item.get('location', {}))]
    return str(matches[0].get('service_id') or '') if len(matches) == 1 else ''

def _candidate_place_records(value: Any):
    """Yield frozen public place projections without depending on one source schema."""
    if isinstance(value, dict):
        if value.get('place_id') and value.get('name') and isinstance(value.get('location'), dict):
            yield value
        for child in value.values():
            yield from _candidate_place_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from _candidate_place_records(child)

def _selected_place_id(payload: dict[str, Any] | None, gt_data: dict[str, Any]) -> str:
    """Resolve the current public ``plan.selection`` contract to one frozen place ID."""
    selection = _plan_payload(payload).get('selection')
    if not isinstance(selection, dict):
        return ''
    name = str(selection.get('name') or '').strip()
    location = selection.get('location')
    if not name or not isinstance(location, dict):
        return ''
    identities = {str(item['place_id']) for item in _candidate_place_records(gt_data) if str(item.get('name') or '').strip().casefold() == name.casefold() and _same_location(location, item.get('location', {}))}
    return identities.pop() if len(identities) == 1 else ''

def _selected_route_ids(payload: dict[str, Any] | None) -> list[str]:
    values = (payload or {}).get('route_ids', []) if isinstance(payload, dict) else []
    return [str(value) for value in values] if isinstance(values, list) and all((isinstance(value, str) for value in values)) else []

def _selected_itinerary_place_ids(payload: dict[str, Any] | None, gt_data: dict[str, Any]) -> list[str] | None:
    """Resolve public itinerary place names to the private frozen IDs."""
    days = _plan_payload(payload).get('days', [])
    if not isinstance(days, list):
        return None
    places = [item for key in ('all_pois', 'itinerary_constraint_candidates') for item in gt_data.get(key, []) if isinstance(item, dict)]
    hotel = gt_data.get('hotel')
    if isinstance(hotel, dict):
        places.append(hotel)
    selected: list[str] = []
    modern = bool(days) and all((isinstance(day, dict) and 'selected_pois' in day for day in days))
    if modern:
        if [day.get('day') for day in days] != list(range(1, len(days) + 1)):
            return None
    for day in days:
        if modern:
            rows = day.get('selected_pois')
            if not isinstance(rows, list) or any((not isinstance(row, dict) for row in rows)):
                return None
            if [row.get('activity_order') for row in rows] != list(range(1, len(rows) + 1)):
                return None
            day = {'events': [row.get('selection') for row in rows]}
        if not isinstance(day, dict) or not isinstance(day.get('events'), list):
            return None
        for event in day['events']:
            if not isinstance(event, dict):
                return None
            name = str(event.get('place_name') or event.get('name') or '').strip()
            location = event.get('location')
            if not name:
                return None
            matches = [item for item in places if str(item.get('name') or '').strip().casefold() == name.casefold()]
            if isinstance(location, dict):
                matches = [item for item in matches if _same_location(location, item.get('location', {}))]
            identities = {str(item['place_id']) for item in matches if item.get('place_id')}
            if len(identities) != 1:
                return None
            selected.append(identities.pop())
    return selected

def _selected_region_place_ids(payload: dict[str, Any] | None, gt_data: dict[str, Any]) -> list[str] | None:
    events = _plan_payload(payload).get('events', [])
    if not isinstance(events, list):
        return None
    places = [item for item in gt_data.get('main_pois', []) if isinstance(item, dict)]
    selected: list[str] = []
    for event in events:
        if not isinstance(event, dict):
            return None
        name = str(event.get('place_name') or event.get('name') or '').strip()
        location = event.get('location')
        if not name:
            return None
        matches = [item for item in places if str(item.get('name') or '').strip().casefold() == name.casefold()]
        if isinstance(location, dict):
            matches = [item for item in matches if _same_location(location, item.get('location', {}))]
        if len(matches) != 1 or not matches[0].get('place_id'):
            return None
        selected.append(str(matches[0]['place_id']))
    return selected

def _same_location(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        return abs(float(left.get('lat')) - float(right.get('lat'))) <= 1e-05 and abs(float(left.get('lng')) - float(right.get('lng'))) <= 1e-05
    except (TypeError, ValueError):
        return False

def _solution_matches(payload: dict[str, Any] | None, gt_data: dict[str, Any], solution: dict[str, Any]) -> bool | None:
    """Judge only adapters with an explicit public-to-private resolver.

    Returning ``None`` is a system-contract failure, not a model failure.  It
    prevents a newly added adapter from silently inheriting the old permissive
    Dynamic metrics before its task-specific evaluator is implemented.
    """
    if _base_task_type(gt_data) == 'multi_entity':
        expected_place = str(solution.get('place_id') or '')
        if expected_place:
            return _selected_place_id(payload, gt_data) == expected_place
        expected_service = str(solution.get('service_id') or '')
        return bool(expected_service) and _selected_multi_entity_service(payload, gt_data) == expected_service
    if _base_task_type(gt_data) == 'safety_planning':
        expected_place = str(solution.get('place_id') or '')
        if expected_place:
            return _selected_place_id(payload, gt_data) == expected_place
        expected_recommendation = str(solution.get('recommendation') or '')
        actual = str(_plan_payload(payload).get('recommendation') or '')
        return bool(expected_recommendation) and actual == expected_recommendation
    if _base_task_type(gt_data) == 'route_planning':
        expected = solution.get('expected_feasible')
        actual = (payload or {}).get('feasible') if isinstance(payload, dict) else None
        return type(expected) is bool and type(actual) is bool and (actual == expected)
    if _base_task_type(gt_data) == 'itinerary':
        expected = [str(place_id) for place_id in solution.get('place_ids', []) or []]
        days = _plan_payload(payload).get('days') or []
        if days and all((isinstance(day, dict) and 'selected_pois' in day for day in days)):
            timeline = [event for day in (gt_data.get('correct_itinerary') or {}).get('days', []) for event in day.get('events', []) if event.get('place_id')]
            poi_count = sum((event.get('block_kind') == 'poi_activity' for event in timeline))
            if len(expected) == poi_count:
                pass
            elif len(expected) == len(timeline):
                expected = [pid for (pid, event) in zip(expected, timeline) if event.get('block_kind') == 'poi_activity']
            else:
                return None
            source_days = (gt_data.get('correct_itinerary') or {}).get('days', [])
            if len(days) != len(source_days) or any((len(day.get('selected_pois') or []) != sum((event.get('block_kind') == 'poi_activity' for event in source_day.get('events', []))) for (day, source_day) in zip(days, source_days))):
                return False
        actual = _selected_itinerary_place_ids(payload, gt_data)
        return bool(expected) and actual == expected
    if _base_task_type(gt_data) == 'region_aware':
        expected = [str(place_id) for place_id in solution.get('place_ids', []) or []]
        selected = _selected_place_id(payload, gt_data)
        if selected:
            return len(expected) == 1 and selected == expected[0]
        actual = _selected_region_place_ids(payload, gt_data)
        return bool(expected) and actual == expected
    return None

def check_base_constraints_still_met(result: AgentRunResult, gt_data: dict) -> MetricResult:
    if not result.initial_plan:
        return MetricResult('base_constraints_met', 'Initial plan', False, 'Missing initial plan checkpoint')
    return MetricResult('base_constraints_met', 'Initial plan', True, 'Initial plan checkpoint recorded; baseline identity is not graded')

def check_disruption_detected(result: AgentRunResult, gt_data: dict) -> MetricResult:
    events = _events(gt_data)
    if not events:
        return MetricResult('disruption_detected', 'Event C grounding', False, 'Dynamic task has no event artifact')
    initial_index = next((i for (i, record) in enumerate(result.trace) if record.tool.rsplit('__', 1)[-1] == 'submit_initial_plan' or (record.tool.rsplit('__', 1)[-1] == 'final_answer' and record.result.get('status') == 'intermediate_recorded')), None)
    if initial_index is None or not result.initial_plan:
        return MetricResult('disruption_detected', 'Event C grounding', False, 'No initial plan checkpoint before observation')
    submitted_value = _plan_payload(result.final_answer).get('event_information')
    if not isinstance(submitted_value, list) or not submitted_value or any((not isinstance(item, str) or not item.strip() for item in submitted_value)):
        return MetricResult('disruption_detected', 'Event C grounding', False, 'Final plan must submit nonempty plan.event_information')
    submitted = set(submitted_value)
    start = initial_index + 1
    observations = [record for record in result.trace[start:] if record.tool.rsplit('__', 1)[-1] != 'final_answer']
    delivered = _delivered_public_updates(result, result.trace[initial_index].seq)
    all_observed = {value for record in observations for value in _public_strings(record.result)}
    all_observed.update((update['message'] for update in delivered))
    if not submitted <= all_observed:
        return MetricResult('disruption_detected', 'Event C grounding', False, 'Submitted event information was not present in observed public evidence')
    required: set[str] = set()
    for event in events:
        message = public_event_message(event)
        path = (event.get('observability') or {}).get('public_observation_path') or {}
        app = str(path.get('app') or event.get('app') or '')
        tool = str(path.get('tool') or '')
        if not message or not app or (not tool):
            return MetricResult('disruption_detected', 'Event C grounding', False, 'Task event lacks a public message or owning App observation path')
        required.add(message)
        if not (any((record.tool.split('__', 1)[0] == app and message in set(_public_strings(record.result)) for record in observations)) or any((update.get('source_app') == app and update['message'] == message for update in delivered))):
            return MetricResult('disruption_detected', 'Event C grounding', False, 'A task-bound event was not observed through its owning App')
    if not required <= submitted:
        return MetricResult('disruption_detected', 'Event C grounding', False, 'Final plan did not submit all task-bound event information')
    return MetricResult('disruption_detected', 'Event C grounding', True, f'Observed and submitted {len(events)} task-bound event(s)')

def _delivered_public_updates(result: AgentRunResult, initial_seq: int) -> list[dict]:
    """New channel only: notices bound to a returned request after P0.

    Scheduling an event or preparing an unreturned API request is not evidence
    delivery. The receipt must agree with the persisted response-turn hash.
    """
    if result.dynamic_information_mode != 'explicit_update':
        return []
    from datetime import datetime
    turns = {t.get('step'): t for t in result.recovery_checkpoint.get('model_turns', [])}
    final_seq = next((r.seq for r in result.trace if r.tool.rsplit('__', 1)[-1] == 'final_answer' and r.result.get('status') == 'submitted'), float('inf'))
    delivered = []
    for receipt in result.explicit_update_deliveries:
        seq = receipt.get('after_tool_seq', -1)
        turn = turns.get(receipt.get('model_step'), {})
        if receipt.get('response_received') is not True or not receipt.get('system_sha256') or turn.get('system_sha256') != receipt['system_sha256'] or (not initial_seq <= seq < final_seq):
            continue
        try:
            observed_at = datetime.fromisoformat(receipt['virtual_time'].replace('Z', '+00:00'))
            if observed_at.tzinfo is None:
                continue
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        for update in receipt.get('updates', []):
            try:
                published = datetime.fromisoformat(update['published_at'].replace('Z', '+00:00'))
                if published.tzinfo is None or published > observed_at:
                    continue
            except (KeyError, ValueError, TypeError, AttributeError):
                continue
            if isinstance(update.get('message'), str) and update['message'].strip():
                delivered.append(update)
    return delivered

def _public_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _public_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _public_strings(item)

def check_replan_executed(result: AgentRunResult, gt_data: dict) -> MetricResult:
    events = _events(gt_data)
    answer = _answer(result)
    if not events or not answer:
        return MetricResult('replan_executed', 'Replanning', False, 'Dynamic events and final answer are both required')
    if not result.initial_plan:
        return MetricResult('replan_executed', 'Replanning', False, 'Dynamic task did not submit an initial plan before finalizing')
    if _base_task_type(gt_data) != 'route_planning' and (not answer.get('feasible', False)):
        return MetricResult('replan_executed', 'Replanning', False, 'Final answer declared the verified P1 state infeasible')
    proof = _proof(gt_data)
    matched = _solution_matches(answer, gt_data, proof.get('post_event_solution', {}))
    if matched is None:
        return MetricResult('replan_executed', 'Replanning', False, f"No Dynamic Judge resolver for {_base_task_type(gt_data) or 'unknown'}")
    if not matched:
        return MetricResult('replan_executed', 'Replanning', False, 'Final answer does not match the private P1 solution')
    return MetricResult('replan_executed', 'Replanning', True, 'Observed event and submitted the verified P1-compatible plan')

def check_event_progression(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Ensure scheduled absolute world changes were actually reached before finalizing."""
    absolute = [event for event in _events(gt_data) if event.get('activation', {}).get('mode', 'absolute') == 'absolute']
    if not absolute:
        return MetricResult('event_progression', 'Virtual event progression', True, 'No absolute event checkpoint required')
    pending = [event.get('event_id', '') for event in absolute if result.dynamic_event_states.get(event.get('event_id', '')) != 'applied']
    if pending:
        return MetricResult('event_progression', 'Virtual event progression', False, f"Finalized before scheduled events: {', '.join(pending)}")
    advances = sum((record.tool in {'submit_initial_plan', 'advance_simulation'} or (record.tool.rsplit('__', 1)[-1] == 'final_answer' and record.result.get('status') == 'intermediate_recorded') for record in result.trace))
    return MetricResult('event_progression', 'Virtual event progression', True, f'Reached all scheduled checkpoints with {advances} virtual advances')

def check_alternative_valid(result: AgentRunResult, gt_data: dict) -> MetricResult:
    answer = _answer(result)
    if not answer:
        return MetricResult('alternative_valid', 'Alternative validity', False, 'Missing final_answer')
    proof = _proof(gt_data)
    impacts = proof.get('event_impacts') or []
    route_boolean = _base_task_type(gt_data) == 'route_planning'
    if not impacts or any((not isinstance(impact, dict) or not impact.get('ablation', {}).get('state_changed' if route_boolean else 'decision_changed') for impact in impacts)):
        return MetricResult('alternative_valid', 'Alternative validity', False, 'Dynamic proof lacks a decision-changing ablation for every event')
    initial_service = _selected_multi_entity_service(result.initial_plan, gt_data)
    final_service = _selected_multi_entity_service(answer, gt_data)
    if _base_task_type(gt_data) == 'multi_entity' and initial_service == final_service:
        return MetricResult('alternative_valid', 'Alternative validity', False, 'Final selection did not change after the service event')
    if _base_task_type(gt_data) == 'itinerary':
        initial_places = _selected_itinerary_place_ids(result.initial_plan, gt_data)
        final_places = _selected_itinerary_place_ids(answer, gt_data)
        if initial_places is not None and initial_places == final_places:
            return MetricResult('alternative_valid', 'Alternative validity', False, 'Final itinerary did not change after the service update')
    if _base_task_type(gt_data) == 'region_aware':
        initial_places = _selected_region_place_ids(result.initial_plan, gt_data)
        final_places = _selected_region_place_ids(answer, gt_data)
        if initial_places is not None and initial_places == final_places:
            return MetricResult('alternative_valid', 'Alternative validity', False, 'Final regional plan did not change after the service update')
    if route_boolean:
        return MetricResult('alternative_valid', 'Alternative validity', True, 'Final Boolean is supported by the route-state event proof')
    return MetricResult('alternative_valid', 'Alternative validity', True, 'Final plan differs from P0 and is justified by the event proof')

def check_notification_complete(result: AgentRunResult, gt_data: dict) -> MetricResult:
    required = [event for event in _events(gt_data) if (event.get('observability') or {}).get('notify', False)]
    if not required:
        return MetricResult('notification_complete', 'Notification', True, 'No event requires participant notification')
    notified = any((record.tool.rsplit('__', 1)[-1] == 'send_message' for record in result.trace))
    if not notified:
        return MetricResult('notification_complete', 'Notification', False, 'Required notification event was not communicated')
    return MetricResult('notification_complete', 'Notification', True, 'Required participant notification was sent')

def check_tool_efficiency(result: AgentRunResult, gt_data: dict[str, Any] | None=None) -> MetricResult:
    event_count = len(_events(gt_data or {}))
    if event_count == 0:
        return MetricResult('tool_efficiency', 'Tool efficiency', False, 'Dynamic task has no event artifact')
    call_count = len(result.trace)
    threshold = 13 + event_count * 3
    if call_count <= threshold:
        return MetricResult('tool_efficiency', 'Tool efficiency', True, f'{call_count} calls within dynamic budget {threshold}')
    return MetricResult('tool_efficiency', 'Tool efficiency', False, f'{call_count} calls exceed dynamic budget {threshold}')
