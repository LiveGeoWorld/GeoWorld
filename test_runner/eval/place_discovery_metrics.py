"""Answer-level evaluation for place discovery."""
from __future__ import annotations
from typing import Any
from test_runner.schema import AgentRunResult, MetricResult

def _normalized_name(value: object) -> str:
    return ' '.join(str(value or '').casefold().split())

def _same_location(left: object, right: object) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    try:
        return abs(float(left.get('lat')) - float(right.get('lat'))) <= 1e-05 and abs(float(left.get('lng')) - float(right.get('lng'))) <= 1e-05
    except (TypeError, ValueError):
        return False

def check_discovery_answer(result: AgentRunResult, gold_data: dict[str, Any]) -> MetricResult:
    """Match the submitted public identity against the private acceptable set."""
    answer = result.final_answer if isinstance(result.final_answer, dict) else {}
    plan = answer.get('plan') if isinstance(answer.get('plan'), dict) else {}
    selection = plan.get('selection') if isinstance(plan.get('selection'), dict) else {}
    submitted_name = _normalized_name(selection.get('name'))
    submitted_location = selection.get('location')
    if not submitted_name or not isinstance(submitted_location, dict):
        return MetricResult(metric='answer_correct', label='答案正确性（主指标）', pass_=False, detail='最终答案必须在 plan.selection 中提交公开地点名称和坐标。')
    gold = gold_data.get('gold') if isinstance(gold_data.get('gold'), dict) else gold_data
    acceptable_ids = {str(value) for value in gold.get('acceptable_top1', []) if value}
    candidates = [item for item in gold.get('candidates', []) if isinstance(item, dict)]
    acceptable = [item for item in candidates if str(item.get('service_id') or '') in acceptable_ids]
    if not acceptable_ids:
        acceptable = [item for item in candidates if item.get('rank') == 1]
    for candidate in acceptable:
        if submitted_name == _normalized_name(candidate.get('name')) and _same_location(submitted_location, candidate.get('coordinate')):
            return MetricResult(metric='answer_correct', label='答案正确性（主指标）', pass_=True, detail='最终提交的公开地点名称和坐标匹配可接受答案。')
    return MetricResult(metric='answer_correct', label='答案正确性（主指标）', pass_=False, detail='最终提交的公开地点名称和坐标未匹配可接受答案。')
