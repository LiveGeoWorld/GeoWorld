"""Private-rule evaluation for region-aware planning without rule-ID leakage."""
from __future__ import annotations
from test_runner.schema import AgentRunResult, MetricResult

def _same_location(left: object, right: object) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    try:
        return abs(float(left.get('lat')) - float(right.get('lat'))) <= 1e-05 and abs(float(left.get('lng')) - float(right.get('lng'))) <= 1e-05
    except (TypeError, ValueError):
        return False

def check_region_decision_success(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Score one contextual choice, never the private cultural rule itself.

    ``region_decision`` is a new private GT envelope.  Its allowed choices are
    the *outcome* of construction-side cultural reasoning, while provenance,
    rules and counterfactuals stay elsewhere in the sandbox.
    """
    decision = gt_data.get('region_decision')
    if not isinstance(decision, dict) or decision.get('schema_version') != 'geoword.region_contextual_choice.v1':
        return MetricResult(metric='region_decision_success', label='地域情境选择成功', pass_=False, detail='missing or unsupported region contextual-choice GT')
    answer = result.final_answer or {}
    plan = answer.get('plan') if isinstance(answer.get('plan'), dict) else {}
    selection = plan.get('selection') if isinstance(plan, dict) else None
    if answer.get('feasible') is not True or not isinstance(selection, dict):
        return MetricResult(metric='region_decision_success', label='地域情境选择成功', pass_=False, detail='missing feasible contextual choice')
    name = ' '.join(str(selection.get('name') or '').casefold().split())
    location = selection.get('location')
    if not name or not isinstance(location, dict):
        return MetricResult(metric='region_decision_success', label='地域情境选择成功', pass_=False, detail='selection requires a public name and App-returned coordinates')
    for option in decision.get('accepted_options', []):
        if not isinstance(option, dict):
            continue
        option_name = ' '.join(str(option.get('name') or '').casefold().split())
        if name == option_name and _same_location(location, option.get('location')):
            return MetricResult(metric='region_decision_success', label='地域情境选择成功', pass_=True, detail='selected public service belongs to the accepted contextual-choice set')
    return MetricResult(metric='region_decision_success', label='地域情境选择成功', pass_=False, detail='selected public service is outside the accepted contextual-choice set')

def _answer(result: AgentRunResult) -> dict:
    return result.final_answer or {}

def _plan(result: AgentRunResult) -> dict:
    plan = _answer(result).get('plan', {})
    return plan if isinstance(plan, dict) else {}

def _selected(plan: dict) -> set[str]:
    values = set(plan.get('selected_place_ids', []))
    values.update(plan.get('selected_place_names', []))
    for item in plan.get('events', []) or []:
        if not isinstance(item, dict):
            continue
        values.add(item.get('place_id', ''))
        values.add(item.get('place_name', item.get('name', '')))
    return {value for value in values if value}

def check_feasible_consistency(result: AgentRunResult, gt_data: dict) -> MetricResult:
    answer = _answer(result)
    if not answer:
        return MetricResult(metric='feasible_consistency', label='feasibility consistency', pass_=False, detail='missing final answer')
    expected = bool(gt_data.get('expected_feasible', True))
    actual = answer.get('feasible', expected)
    return MetricResult(metric='feasible_consistency', label='feasibility consistency', pass_=actual == expected, detail=f'expected={expected}, agent={actual}')

def check_cultural_time_valid(result: AgentRunResult, gt_data: dict) -> MetricResult:
    events = _plan(result).get('events', [])
    if not events:
        return MetricResult(metric='cultural_time_valid', label='cultural time validity', pass_=False, detail='missing plan.events')
    ranges = gt_data.get('cultural_requirements', {}).get('forbidden_time_ranges', [])
    for event in events:
        start = event.get('start', '')
        for item in ranges:
            if item.get('start', '') <= start < item.get('end', ''):
                return MetricResult(metric='cultural_time_valid', label='cultural time validity', pass_=False, detail=f'{start} falls in a private forbidden interval')
    return MetricResult(metric='cultural_time_valid', label='cultural time validity', pass_=True, detail='no private time violation')

def check_cultural_venue_valid(result: AgentRunResult, gt_data: dict) -> MetricResult:
    selected = _selected(_plan(result))
    if not selected:
        return MetricResult(metric='cultural_venue_valid', label='cultural venue validity', pass_=False, detail='no selected places')
    acceptable = {value for plan in gt_data.get('acceptable_plans', []) or [gt_data.get('expected_plan', {})] for value in _selected(plan if isinstance(plan, dict) else {})}
    if acceptable and (not selected.issubset(acceptable)):
        invalid = sorted(selected - acceptable)
        return MetricResult(metric='cultural_venue_valid', label='cultural venue validity', pass_=False, detail=f"not in private valid plan set: {', '.join(invalid)}")
    prohibited = {item.get('place_id', '') for item in gt_data.get('wrong_plans', [])}
    overlap = selected & prohibited
    return MetricResult(metric='cultural_venue_valid', label='cultural venue validity', pass_=not overlap, detail='no prohibited venue selected' if not overlap else f"prohibited IDs: {', '.join(sorted(overlap))}")

def check_cultural_decision_integrated(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Verify a concrete plan consequence, never rationale keyword recall."""
    contract = gt_data.get('decision_contract', {}) or {}
    if not contract.get('material'):
        return MetricResult(metric='cultural_decision_integrated', label='文化决策已落实', pass_=False, detail='task has no material private decision contract')
    expected = {str(action) for item in contract.get('material_constraints', []) or [] for action in (item.get('with_constraint') or {}).get('cultural_actions', []) or [] if action}
    if not expected:
        return MetricResult(metric='cultural_decision_integrated', label='文化决策已落实', pass_=True, detail='private contract has a verified place/time effect')
    submitted = _submitted_cultural_actions(_plan(result))
    if not submitted:
        return MetricResult(metric='cultural_decision_integrated', label='文化决策已落实', pass_=False, detail='missing plan.cultural_actions')
    missing = sorted(expected - submitted)
    return MetricResult(metric='cultural_decision_integrated', label='文化决策已落实', pass_=not missing, detail='all required plan actions present' if not missing else f"missing required plan actions: {', '.join(missing)}")

def _submitted_cultural_actions(plan: dict) -> set[str]:
    raw = plan.get('cultural_actions', plan.get('protocol_actions', [])) or []
    values: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            item = item.get('action') or item.get('action_id') or item.get('description') or ''
        normalized = _normalize_action(item)
        if normalized:
            values.add(normalized)
    return values

def _normalize_action(value: object) -> str:
    text = ' '.join(str(value or '').lower().replace('_', ' ').split())
    aliases = {'eat with right hand': 'eat_with_right_hand', '用右手吃': 'eat_with_right_hand', '右手用餐': 'eat_with_right_hand', 'place chopsticks horizontally': 'place_chopsticks_horizontally', '筷子平放': 'place_chopsticks_horizontally', 'remove shoes before entry': 'remove_shoes_before_entry', '进门前脱鞋': 'remove_shoes_before_entry', '脱鞋入内': 'remove_shoes_before_entry', 'avoid pointing at deities': 'avoid_pointing_at_deities', '避免指向神像': 'avoid_pointing_at_deities', 'avoid public display of affection': 'avoid_public_display_of_affection', '避免公开亲密行为': 'avoid_public_display_of_affection', 'use designated smoking area only': 'use_designated_smoking_area_only', '仅在指定吸烟区': 'use_designated_smoking_area_only'}
    return aliases.get(text, text.replace(' ', '_') if text in aliases.values() else '')

def check_wrong_plans_avoided(result: AgentRunResult, gt_data: dict) -> MetricResult:
    forbidden = {item.get('place_id', '') for item in gt_data.get('wrong_plans', [])}
    overlap = _selected(_plan(result)) & forbidden
    return MetricResult(metric='wrong_plans_avoided', label='wrong plans avoided', pass_=not overlap, detail='avoided private wrong plans' if not overlap else f"selected: {', '.join(sorted(overlap))}")

def check_explanation_cultural(result: AgentRunResult, gt_data: dict) -> MetricResult:
    return check_cultural_decision_integrated(result, gt_data)

def check_tool_efficiency(result: AgentRunResult, gt_data=None) -> MetricResult:
    calls = len([record for record in result.trace if record.tool not in {'final_answer', 'UserInterfaceApp__final_answer'}])
    return MetricResult(metric='tool_efficiency', label='tool efficiency', pass_=calls <= 12, detail=f'{calls} calls')

def check_constraint_ids_applied(result: AgentRunResult, gt_data: dict) -> MetricResult:
    return MetricResult(metric='constraint_ids_applied', label='private rule IDs', pass_=True, detail='not evaluated: private identifiers are not exposed to agents')
