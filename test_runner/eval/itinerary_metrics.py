"""Itinerary evaluation metrics — 8 independent constraint verification metrics.

Metrics:
  1. feasible_consistency — agent feasible flag matches GT
  2. itinerary_completeness — all required event types covered
  3. time_window_valid — events within POI operating hours
  4. event_order_valid — events chronological per day
  5. spatial_coherence — consecutive event commute times feasible
  6. budget_valid — daily and total budget respected
  7. constraint_satisfaction — all GT constraints satisfied
  8. tool_efficiency — reasonable number of tool calls
"""
from __future__ import annotations
from test_runner.schema import MetricResult, AgentRunResult

def _get_answer(result: AgentRunResult) -> dict | None:
    return result.final_answer

def _time_to_min(s: str) -> int:
    try:
        (h, m) = map(int, s.split(':'))
        return h * 60 + m
    except (ValueError, AttributeError):
        return -1

def check_feasible_consistency(result: AgentRunResult, gt_data: dict) -> MetricResult:
    ans = _get_answer(result)
    if not ans:
        return MetricResult(metric='feasible_consistency', label='可行性一致', pass_=False, detail='未提交 final_answer')
    expected = gt_data.get('expected_feasible', True)
    agent = ans.get('feasible', True)
    if expected == agent:
        return MetricResult(metric='feasible_consistency', label='可行性一致', pass_=True, detail=f"一致：{('可行' if expected else '不可行')}")
    return MetricResult(metric='feasible_consistency', label='可行性一致', pass_=False, detail=f"GT={('可行' if expected else '不可行')} Agent={('可行' if agent else '不可行')}")

def check_itinerary_completeness(result: AgentRunResult, gt_data: dict) -> MetricResult:
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='itinerary_completeness', label='行程完整性', pass_=True, detail='（不可行任务）')
    ans = _get_answer(result)
    if not ans:
        return MetricResult(metric='itinerary_completeness', label='行程完整性', pass_=False, detail='未提交 final_answer')
    plan = ans.get('plan', {})
    agent_days = plan.get('days', [])
    if not agent_days:
        return MetricResult(metric='itinerary_completeness', label='行程完整性', pass_=False, detail='未提交 days 数据')
    gt_days = gt_data.get('correct_itinerary', {}).get('days', [])
    gt_event_types = set()
    for d in gt_days:
        for e in d.get('events', []):
            gt_event_types.add(e.get('event_type', ''))
    agent_event_types = set()
    for d in agent_days:
        for e in d.get('events', []):
            agent_event_types.add(e.get('event_type', ''))
    if not gt_event_types:
        return MetricResult(metric='itinerary_completeness', label='行程完整性', pass_=True, detail='（GT无事件类型）')
    coverage = len(agent_event_types & gt_event_types) / len(gt_event_types)
    passed = coverage >= 0.7
    return MetricResult(metric='itinerary_completeness', label='行程完整性', pass_=passed, detail=f'覆盖 {coverage:.0%} 事件类型')

def check_time_window_valid(result: AgentRunResult, gt_data: dict) -> MetricResult:
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='time_window_valid', label='时间窗口', pass_=True, detail='（不可行任务）')
    ans = _get_answer(result)
    if not ans:
        return MetricResult(metric='time_window_valid', label='时间窗口', pass_=False, detail='未提交 final_answer')
    plan = ans.get('plan', {})
    agent_days = plan.get('days', [])
    if not agent_days:
        return MetricResult(metric='time_window_valid', label='时间窗口', pass_=True, detail='（未提供days数据，跳过）')
    all_pois_list = gt_data.get('all_pois', [])
    if isinstance(all_pois_list, dict):
        all_pois_list = [p for v in all_pois_list.values() for p in v]
    all_pois = {p.get('place_id', ''): p for p in all_pois_list}
    hotel = gt_data.get('hotel', {})
    if hotel.get('place_id'):
        all_pois[hotel['place_id']] = hotel
    violations = 0
    total_checked = 0
    for day in agent_days:
        for evt in day.get('events', []):
            pid = evt.get('place_id', '')
            start = evt.get('start', '')
            if not pid or not start:
                continue
            poi = all_pois.get(pid)
            if not poi:
                continue
            cat = poi.get('category', '')
            if cat in ('transit', 'hotel'):
                continue
            opening = str(poi.get('opening') or '').split('-', 1)
            open_time = poi.get('opening_open') or (opening[0] if len(opening) == 2 else '')
            close_time = poi.get('opening_close') or (opening[1] if len(opening) == 2 else '')
            if not open_time:
                continue
            total_checked += 1
            start_min = _time_to_min(start)
            end_min = _time_to_min(evt.get('end') or start)
            open_min = _time_to_min(open_time)
            close_min = _time_to_min(close_time)
            if close_min >= 0 and close_min < open_min:
                close_min += 24 * 60
                if 0 <= start_min < open_min:
                    start_min += 24 * 60
                if 0 <= end_min < open_min:
                    end_min += 24 * 60
            if start_min < open_min or end_min < start_min or (close_min >= 0 and end_min > close_min):
                violations += 1
    if total_checked == 0:
        return MetricResult(metric='time_window_valid', label='时间窗口', pass_=True, detail='（无营业时间数据，跳过）')
    passed = violations == 0
    return MetricResult(metric='time_window_valid', label='时间窗口', pass_=passed, detail=f'{violations}/{total_checked} 违规')

def check_event_order_valid(result: AgentRunResult, gt_data: dict) -> MetricResult:
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='event_order_valid', label='事件顺序', pass_=True, detail='（不可行任务）')
    ans = _get_answer(result)
    if not ans:
        return MetricResult(metric='event_order_valid', label='事件顺序', pass_=False, detail='未提交 final_answer')
    plan = ans.get('plan', {})
    agent_days = plan.get('days', [])
    if not agent_days:
        return MetricResult(metric='event_order_valid', label='事件顺序', pass_=True, detail='（无days数据，跳过）')
    order_violations = 0
    total_days = 0
    for day in agent_days:
        events = day.get('events', [])
        if len(events) < 2:
            continue
        total_days += 1
        for i in range(1, len(events)):
            prev_end = _time_to_min(events[i - 1].get('end', events[i - 1].get('start', '00:00')))
            curr_start = _time_to_min(events[i].get('start', '99:99'))
            if prev_end < 0 or curr_start < 0:
                continue
            if curr_start < prev_end:
                order_violations += 1
    if total_days == 0:
        return MetricResult(metric='event_order_valid', label='事件顺序', pass_=True, detail='（无多事件天，跳过）')
    passed = order_violations == 0
    return MetricResult(metric='event_order_valid', label='事件顺序', pass_=passed, detail=f'{order_violations}次顺序错误 / {total_days}天')

def check_spatial_coherence(result: AgentRunResult, gt_data: dict) -> MetricResult:
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='spatial_coherence', label='地理相干', pass_=True, detail='（不可行任务）')
    route_data = gt_data.get('route_data', {})
    if not route_data:
        return MetricResult(metric='spatial_coherence', label='地理相干', pass_=True, detail='（无通勤数据，跳过）')
    ans = _get_answer(result)
    if not ans:
        return MetricResult(metric='spatial_coherence', label='地理相干', pass_=False, detail='未提交 final_answer')
    plan = ans.get('plan', {})
    agent_days = plan.get('days', [])
    if not agent_days:
        return MetricResult(metric='spatial_coherence', label='地理相干', pass_=True, detail='（无days数据，跳过）')
    violations = 0
    total_pairs = 0
    for day in agent_days:
        events = day.get('events', [])
        for i in range(1, len(events)):
            prev_name = events[i - 1].get('place_name', '?')[:15]
            curr_name = events[i].get('place_name', '?')[:15]
            gap = events[i - 1].get('end', '')
            start = events[i].get('start', '')
            if not gap or not start:
                continue
            key_fwd = f'{prev_name}→{curr_name}'
            key_bwd = f'{curr_name}→{prev_name}'
            min_commute = None
            for key in (key_fwd, key_bwd):
                if key in route_data:
                    modes = route_data[key]
                    times = [t for t in modes.values() if isinstance(t, (int, float))]
                    if times:
                        min_commute = min(times)
                    break
            if min_commute is None:
                min_commute = 30
            real_gap = _time_to_min(start) - _time_to_min(gap)
            total_pairs += 1
            if real_gap >= 0 and real_gap < min_commute:
                violations += 1
    if total_pairs == 0:
        return MetricResult(metric='spatial_coherence', label='地理相干', pass_=True, detail='（无可验证事件对，跳过）')
    passed = violations == 0
    return MetricResult(metric='spatial_coherence', label='地理相干', pass_=passed, detail=f'{violations}/{total_pairs} 通勤不足')

def check_budget_valid(result: AgentRunResult, gt_data: dict) -> MetricResult:
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='budget_valid', label='预算合规', pass_=True, detail='（不可行任务）')
    constraints = gt_data.get('constraints', {})
    max_daily = constraints.get('max_daily_budget', 0)
    if not max_daily:
        return MetricResult(metric='budget_valid', label='预算合规', pass_=True, detail='（无预算限制）')
    ans = _get_answer(result)
    if not ans:
        return MetricResult(metric='budget_valid', label='预算合规', pass_=False, detail='未提交 final_answer')
    plan = ans.get('plan', {})
    daily_budgets = plan.get('daily_budgets', [])
    if not daily_budgets:
        return MetricResult(metric='budget_valid', label='预算合规', pass_=True, detail='（Agent未提供预算，跳过）')
    violations = sum((1 for b in daily_budgets if b > max_daily))
    passed = violations == 0
    return MetricResult(metric='budget_valid', label='预算合规', pass_=passed, detail=f'{violations}天超支 / {len(daily_budgets)}天')

def check_constraint_satisfaction(result: AgentRunResult, gt_data: dict) -> MetricResult:
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='constraint_satisfaction', label='约束满足', pass_=True, detail='（不可行任务）')
    constraints = gt_data.get('constraints', {})
    if not constraints:
        return MetricResult(metric='constraint_satisfaction', label='约束满足', pass_=True, detail='（无明确约束）')
    ans = _get_answer(result)
    if not ans:
        return MetricResult(metric='constraint_satisfaction', label='约束满足', pass_=True, detail='（未提交，跳过）')
    plan = ans.get('plan', {})
    agent_days = plan.get('days', [])
    constraint_checks = []
    max_gap = constraints.get('max_event_gap_min')
    if max_gap and agent_days:
        gap_violations = 0
        for day in agent_days:
            events = day.get('events', [])
            for i in range(1, len(events)):
                prev_end = events[i - 1].get('end', '')
                curr_start = events[i].get('start', '')
                if prev_end and curr_start:
                    gap = _time_to_min(curr_start) - _time_to_min(prev_end)
                    if gap > max_gap:
                        gap_violations += 1
        constraint_checks.append(f'间隔违规{gap_violations}次(max={max_gap}min)')
    if not constraint_checks:
        return MetricResult(metric='constraint_satisfaction', label='约束满足', pass_=True, detail='约束已验证')
    passed = all(('违规0' in c for c in constraint_checks))
    return MetricResult(metric='constraint_satisfaction', label='约束满足', pass_=passed, detail='; '.join(constraint_checks))

def check_tool_efficiency(result: AgentRunResult, gt_data: dict | None=None) -> MetricResult:
    call_count = len([r for r in result.trace if r.tool not in {'final_answer', 'UserInterfaceApp__final_answer'}])
    if call_count <= 15:
        return MetricResult(metric='tool_efficiency', label='工具效率', pass_=True, detail=f'{call_count} 次')
    return MetricResult(metric='tool_efficiency', label='工具效率', pass_=False, detail=f'{call_count} 次（偏多）')
