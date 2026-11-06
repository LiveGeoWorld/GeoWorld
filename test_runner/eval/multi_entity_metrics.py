"""Multi-entity evaluation metrics.

Place-selection correctness and seven auxiliary coordination diagnostics.
"""
from __future__ import annotations
from typing import Any
import math
from test_runner.eval.multi_entity_evidence import participant_id, participant_records, observed_commute_seconds
from test_runner.schema import MetricResult, AgentRunResult

def _get_answer(result: AgentRunResult) -> dict | None:
    return result.final_answer

def selected_place(result: AgentRunResult, gt_data: dict[str, Any]) -> dict[str, Any] | None:
    """Privately normalize an Agent's public place selection.

    Formal Agents submit a public name and optional coordinates observed from
    LocalServiceApp. Frozen service IDs are only used after this boundary to
    compare the selected fact with private acceptable plans.
    """
    answer = _get_answer(result) or {}
    plan = answer.get('plan', {}) if isinstance(answer.get('plan'), dict) else {}
    selection = plan.get('selection') if isinstance(plan.get('selection'), dict) else {}
    name = str(selection.get('name') or plan.get('restaurant_name') or answer.get('restaurant_name') or '').strip()
    location = selection.get('location') or plan.get('restaurant_location') or answer.get('restaurant_location')
    candidates = list(gt_data.get('candidate_places') or gt_data.get('candidate_restaurants') or [])
    if name:
        matches = [item for item in candidates if str(item.get('name') or '').strip().casefold() == name.casefold()]
        if isinstance(location, dict):
            matches = [item for item in matches if same_location(location, item.get('location', {}))]
        return matches[0] if len(matches) == 1 else None
    legacy_id = str(plan.get('restaurant_service_id') or answer.get('restaurant_service_id') or '')
    return next((item for item in candidates if item.get('service_id') == legacy_id), None)

def same_location(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        return abs(float(left.get('lat')) - float(right.get('lat'))) <= 1e-05 and abs(float(left.get('lng')) - float(right.get('lng'))) <= 1e-05
    except (TypeError, ValueError):
        return False

def check_feasible_match(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Agent feasible declaration must match expected."""
    ans = _get_answer(result)
    if not ans:
        return MetricResult(metric='feasible_match', label='可行性一致', pass_=False, detail='未提交 final_answer')
    expected = gt_data.get('expected_feasible', True)
    agent = ans.get('feasible', True)
    if expected == agent:
        return MetricResult(metric='feasible_match', label='可行性一致', pass_=True, detail=f"一致：{('可行' if expected else '不可行')}")
    return MetricResult(metric='feasible_match', label='可行性一致', pass_=False, detail=f"GT={('可行' if expected else '不可行')} Agent={('可行' if agent else '不可行')}")

def check_restaurant_match(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Selected public place must resolve to the private GT."""
    if not gt_data.get('expected_feasible', True):
        answer = _get_answer(result)
        correct = isinstance(answer, dict) and answer.get('feasible') is False
        return MetricResult(metric='place_match', label='地点选择', pass_=correct, detail='（不可行任务，判断正确）' if correct else '未正确提交不可行判断')
    ans = _get_answer(result)
    if not ans:
        return MetricResult(metric='place_match', label='地点选择', pass_=False, detail='未提交 final_answer')
    selected = selected_place(result, gt_data)
    selected_ids = {str(identifier) for identifier in ((selected or {}).get('place_id'), (selected or {}).get('service_id')) if identifier}
    expected = str(gt_data.get('expected_place_id') or gt_data.get('expected_restaurant_service_id') or '')
    legacy_acceptable = {str(plan.get('restaurant_service_id') or '') for plan in gt_data.get('acceptable_plans', [])}
    if selected_ids and (expected in selected_ids or (not expected and bool(selected_ids & legacy_acceptable))):
        return MetricResult(metric='place_match', label='地点选择', pass_=True, detail=str(selected.get('name') or sorted(selected_ids)[0])[:40])
    return MetricResult(metric='place_match', label='地点选择', pass_=False, detail='提交的公开地点名称与坐标未解析到预期地点')

def check_budget_valid(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Per-person cost must be ≤ max_budget_pp."""
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='budget_valid', label='预算合规', pass_=True, detail='（不可行任务）')
    constraints = gt_data.get('constraints', {})
    max_budget = constraints.get('max_budget_pp', 0)
    if not max_budget:
        return MetricResult(metric='budget_valid', label='预算合规', pass_=True, detail='（无预算限制）')
    ans = _get_answer(result)
    if not ans:
        return MetricResult(metric='budget_valid', label='预算合规', pass_=False, detail='未提交 final_answer')
    r = selected_place(result, gt_data)
    if not r:
        return MetricResult(metric='budget_valid', label='预算合规', pass_=False, detail='未找到对应餐厅')
    pp = r.get('price_pp', 0)
    passed = pp <= max_budget
    return MetricResult(metric='budget_valid', label='预算合规', pass_=passed, detail=f"¥{pp} {('≤' if passed else '>')} ¥{max_budget}")

def check_dietary_satisfied(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Restaurant must accommodate all participants' dietary needs."""
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='dietary_satisfied', label='饮食兼容', pass_=True, detail='（不可行任务）')
    constraints = gt_data.get('constraints', {})
    if not constraints.get('must_accommodate_all_dietary'):
        return MetricResult(metric='dietary_satisfied', label='饮食兼容', pass_=True, detail='（无饮食要求）')
    ans = _get_answer(result)
    if not ans:
        return MetricResult(metric='dietary_satisfied', label='饮食兼容', pass_=False, detail='未提交 final_answer')
    r = selected_place(result, gt_data)
    if not r:
        return MetricResult(metric='dietary_satisfied', label='饮食兼容', pass_=False, detail='未找到对应餐厅')
    dietary_needs = {p.get('dietary', '') for p in gt_data.get('participants', []) if p.get('dietary') and str(p['dietary']).strip().casefold() not in {'', '无', 'none'}}
    if not dietary_needs:
        return MetricResult(metric='dietary_satisfied', label='饮食兼容', pass_=True, detail='（参与者无特殊饮食限制）')
    options = set(r.get('dietary_options', []))
    missing = dietary_needs - options
    if missing:
        return MetricResult(metric='dietary_satisfied', label='饮食兼容', pass_=False, detail=f"不满足: {', '.join(missing)}")
    return MetricResult(metric='dietary_satisfied', label='饮食兼容', pass_=True, detail='所有饮食限制已满足')

def check_time_window_valid(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """All participants must be within their available time windows."""
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='time_window_valid', label='时间窗口', pass_=True, detail='（不可行任务）')
    ans = _get_answer(result)
    if not ans:
        return MetricResult(metric='time_window_valid', label='时间窗口', pass_=True, detail='（未提交时间信息，跳过）')
    plan = ans.get('plan', {}) if isinstance(ans.get('plan'), dict) else {}
    start = str(plan.get('meeting_start') or '')
    end = str(plan.get('meeting_end') or '')
    window = gt_data.get('constraints', {}).get('common_time_window', {})
    passed = bool(start and end and (window.get('start') <= start < end <= window.get('end')))
    return MetricResult(metric='time_window_valid', label='时间窗口', pass_=passed, detail=f"{start or '未提交'}-{end or '未提交'}")

def check_commute_feasible(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Total commute time must be ≤ max_commute_min."""
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='commute_feasible', label='通勤可行', pass_=True, detail='（不可行任务）')
    constraints = gt_data.get('constraints', {})
    max_commute = constraints.get('max_commute_min', 0)
    if not max_commute:
        return MetricResult(metric='commute_feasible', label='通勤可行', pass_=True, detail='（无通勤限制）')
    ans = _get_answer(result)
    if not ans:
        return MetricResult(metric='commute_feasible', label='通勤可行', pass_=False, detail='未提交 final_answer')
    plan = ans.get('plan') if isinstance(ans.get('plan'), dict) else {}
    total = plan.get('total_commute_min', 0) or ans.get('total_commute_min', 0)
    if total:
        if isinstance(total, bool) or not isinstance(total, (int, float)) or (not math.isfinite(total)) or (total < 0):
            return MetricResult('commute_feasible', '通勤可行', False, '提交的通勤估算不是有效非负数', verification_status='unverifiable')
        passed = total <= max_commute
        return MetricResult('commute_feasible', '通勤可行', passed, f"提交估算 {total}min {('≤' if passed else '>')} {max_commute}min")
    (seconds, status, reason) = observed_commute_seconds(result, gt_data)
    if seconds is None:
        return MetricResult('commute_feasible', '通勤可行', False, reason, verification_status=status)
    passed = seconds <= max_commute * 60
    return MetricResult('commute_feasible', '通勤可行', passed, f"逐人所选路线累计 {seconds}s {('≤' if passed else '>')} {max_commute * 60}s")

def check_notification_complete(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Agent must notify all participants."""
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='notification_complete', label='通知完整', pass_=True, detail='（不可行任务）')
    participants = gt_data.get('participants', [])
    notified = set()
    for rec in result.trace:
        if rec.tool in {'send_message', 'MessagingApp__send_message'}:
            if not isinstance(rec.result, dict) or rec.result.get('status') != 'sent':
                continue
            members = [rec.args.get('recipient_id')]
        elif rec.tool in {'create_group', 'MessagingApp__create_group'}:
            if not isinstance(rec.result, dict) or rec.result.get('status') != 'created':
                continue
            members = rec.args.get('member_ids', [])
            if not isinstance(members, list):
                continue
        else:
            continue
        for member in members:
            identity = participant_id(member, participants)
            if identity:
                notified.add(identity)
    expected = {p['character_id'] for p in participants}
    missing = expected - notified
    if missing:
        names = [p.get('name', p['character_id']) for p in participants if p['character_id'] in missing]
        return MetricResult('notification_complete', '通知完整', False, '未通知: ' + ', '.join(names[:3]))
    return MetricResult('notification_complete', '通知完整', True, f'已通知 {len(notified & expected)}/{len(expected)} 人')

def check_assignment_complete(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Explicit public participant lists, exact coverage, no prose inference."""
    answer = _get_answer(result) or {}
    plan = answer.get('plan') if isinstance(answer.get('plan'), dict) else {}
    (records, status, reason) = participant_records(plan, gt_data.get('participants', []))
    if records is None:
        return MetricResult('assignment_complete', '人物安排', False, reason, verification_status=status)
    return MetricResult('assignment_complete', '人物安排', True, f'{len(records)}/{len(records)} 人（公开姓名或兼容 ID）')

def check_efficiency(result: AgentRunResult) -> MetricResult:
    """Count tool calls vs reasonable minimum."""
    call_count = len([r for r in result.trace if r.tool not in {'final_answer', 'UserInterfaceApp__final_answer'}])
    if call_count <= 10:
        return MetricResult(metric='tool_efficiency', label='工具效率', pass_=True, detail=f'{call_count} 次')
    return MetricResult(metric='tool_efficiency', label='工具效率', pass_=False, detail=f'{call_count} 次（偏多）')
