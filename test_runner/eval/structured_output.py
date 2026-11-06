"""Shared hard gate for structured answers in compositional tasks."""
from __future__ import annotations
from typing import Any
from test_runner.schema import AgentRunResult, MetricResult

def check_required_structured_output(result: AgentRunResult, task_type: str, gt_data: dict[str, Any]) -> MetricResult:
    """Fail closed when an agent omits the fields needed for verification."""
    answer = result.final_answer or {}
    plan = answer.get('plan')
    missing: list[str] = []
    if not isinstance(plan, dict):
        missing.append('plan')
        plan = {}
    if task_type == 'multi_entity':
        required = gt_data.get('required_output_fields') or ['assignments', 'schedule']
        missing.extend((field for field in required if not plan.get(field)))
    elif task_type == 'itinerary':
        from test_runner.eval.itinerary_choice import public_output_errors
        missing.extend(public_output_errors(answer))
    elif task_type == 'safety_planning':
        if isinstance(gt_data.get('safety_choice'), dict):
            if not isinstance(plan.get('selection'), dict):
                missing.append('plan.selection')
            information = plan.get('safety_information')
            if not isinstance(information, list) or not information or any((not isinstance(item, str) or not item.strip() for item in information)):
                missing.append('plan.safety_information')
            return MetricResult('required_structured_output', 'Structured output', not missing, ', '.join(missing) if missing else 'Complete')
        if not plan.get('recommendation'):
            missing.append('plan.recommendation')
        if not bool(gt_data.get('original_plan_safe', gt_data.get('expected_safe', True))) and (not plan.get('rationale')):
            missing.append('plan.rationale')
        if gt_data.get('constraints', {}).get('required_safe_alternative') and (not plan.get('alternatives')):
            missing.append('plan.alternatives')
        if gt_data.get('constraints', {}).get('required_safety_checks') and (not plan.get('safety_checks')):
            missing.append('plan.safety_checks')
    elif task_type == 'region_aware':
        if isinstance(gt_data.get('region_decision'), dict):
            selection = plan.get('selection')
            if not isinstance(selection, dict) or not selection.get('name') or (not isinstance(selection.get('location'), dict)):
                missing.append('plan.selection{name,location}')
        else:
            if not plan.get('events') and (not plan.get('selected_place_ids')):
                missing.append('plan.events_or_selected_place_ids')
            if not plan.get('rationale'):
                missing.append('plan.rationale')
            contract = gt_data.get('decision_contract', {}) or {}
            requires_action = any(((item.get('with_constraint') or {}).get('cultural_actions') for item in contract.get('material_constraints', []) or []))
            if requires_action and (not plan.get('cultural_actions')):
                missing.append('plan.cultural_actions')
    elif task_type == 'dynamic_replanning':
        if not result.initial_plan:
            missing.append('initial_plan')
        if not answer:
            missing.append('final_answer')
    return MetricResult(metric='required_structured_output', label='结构化输出完整性', pass_=not missing, detail='完整' if not missing else f"缺少必填字段: {', '.join(missing)}")
