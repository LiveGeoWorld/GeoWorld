"""Evaluation metrics for route planning tasks.

Constraint-based verification: each constraint dimension is a
separate metric with its own pass/fail result.
"""
from __future__ import annotations
from test_runner.schema import MetricResult, AgentRunResult

def _get_answer(result: AgentRunResult) -> dict | None:
    return result.final_answer

def _build_route_dir(result: AgentRunResult) -> dict[str, dict]:
    """Extract route metadata from agent trace (query_routes results)."""
    rd = {}
    for rec in result.trace:
        if rec.tool in {'query_routes', 'MapApp__query_routes'}:
            for r in rec.result.get('routes', []):
                rid = r.get('route_id', '')
                if not rid:
                    continue
                if rid not in rd:
                    rd[rid] = {'origin_id': rec.args.get('origin_id', ''), 'dest_id': rec.args.get('dest_id', ''), 'origin_name': r.get('origin_name', ''), 'dest_name': r.get('dest_name', ''), 'mode': r.get('mode', ''), 'distance_m': r.get('distance_m', 0), 'duration_s': r.get('duration_s', 0), **{key: r[key] for key in ('walking_distance_m', 'available') if key in r}}
    return rd

def _parse_plan(result: AgentRunResult, gt_data: dict) -> dict | None:
    """Extract route_ids and route_dir from agent's final answer.
    Returns None if agent didn't submit an answer (plan evaluation impossible).
    """
    ans = _get_answer(result)
    if not isinstance(ans, dict) or not ans:
        return None
    route_ids = ans.get('route_ids', [])
    if isinstance(route_ids, str):
        route_ids = [route_ids]
    if not route_ids:
        from test_runner.eval.route_plan_evidence import public_route_plan
        return public_route_plan(result, gt_data)
    return {'route_ids': route_ids, 'rd': _build_route_dir(result), 'core': {p.get('role'): p.get('place_id') for p in gt_data.get('core_places', [])}, 'wp_ids': [p.get('place_id') for p in gt_data.get('core_places', []) if p.get('role') == 'waypoint'], 'constraints': gt_data.get('constraints', {})}

def check_feasible_consistency(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Agent's feasible declaration must match oracle expected_feasible."""
    ans = _get_answer(result)
    if not isinstance(ans, dict) or not ans:
        return MetricResult(metric='feasible_consistency', label='可行性一致性', pass_=False, detail='Agent 未提交 final_answer')
    expected = gt_data.get('expected_feasible', True)
    agent_feasible = ans.get('feasible')
    if type(agent_feasible) is not bool:
        return MetricResult(metric='feasible_consistency', label='可行性一致性', pass_=False, detail='final_answer.feasible 缺失或不是布尔值')
    if expected and (not agent_feasible):
        return MetricResult(metric='feasible_consistency', label='可行性一致性', pass_=False, detail='任务实际可行，Agent 错误地声明不可行')
    if not expected and agent_feasible:
        min_dur = gt_data.get('minimum_possible_duration_min', 0)
        return MetricResult(metric='feasible_consistency', label='可行性一致性', pass_=False, detail=f'任务实际不可行（最短{min_dur}min），Agent 强行提交方案')
    return MetricResult(metric='feasible_consistency', label='可行性一致性', pass_=True, detail=f"一致：{('可行' if expected else '不可行')}")

def check_plan_structure(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Route chain: starts at origin, ends at dest, legs are connected."""
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='plan_structure', label='路线结构', pass_=True, detail='（不可行任务）')
    plan = _parse_plan(result, gt_data)
    if plan is None:
        return MetricResult(metric='plan_structure', label='路线结构', pass_=False, detail='未提交有效 plan')
    route_ids = plan['route_ids']
    rd = plan['rd']
    origin_id = plan['core'].get('origin', '')
    dest_id = plan['core'].get('destination', '')
    errors = []
    prev_dest_id = None
    for (i, rid) in enumerate(route_ids):
        info = rd.get(rid, {})
        roid = info.get('origin_id', '')
        rdid = info.get('dest_id', '')
        if info.get('available') is False:
            errors.append(f'leg[{i}] 已查询路线标记为不可用')
        if not roid:
            errors.append(f'leg[{i}] route_id={rid[:16]} 不在 trace 中')
            continue
        if i == 0 and origin_id and (roid != origin_id):
            errors.append(f"起点应为 {plan['core'].get('origin', '?')[:20]}，实际 {roid[:20]}")
        if prev_dest_id is not None and prev_dest_id != roid:
            errors.append(f'leg[{i}] 不连续: {prev_dest_id[:20]} ≠ {roid[:20]}')
        prev_dest_id = rdid
    if dest_id and prev_dest_id is not None and (prev_dest_id != dest_id):
        errors.append(f"终点应为 {plan['core'].get('destination', '?')[:20]}，实际 {prev_dest_id[:20]}")
    if errors:
        return MetricResult(metric='plan_structure', label='路线结构', pass_=False, detail='; '.join(errors))
    return MetricResult(metric='plan_structure', label='路线结构', pass_=True, detail=f'{len(route_ids)} 段路线连续，起终点正确')

def check_waypoint_coverage(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """All required waypoints must be visited."""
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='waypoint_coverage', label='途经点覆盖', pass_=True, detail='（不可行任务）')
    plan = _parse_plan(result, gt_data)
    if plan is None:
        return MetricResult(metric='waypoint_coverage', label='途经点覆盖', pass_=False, detail='未提交有效 plan')
    wp_ids = plan['wp_ids']
    if not wp_ids:
        return MetricResult(metric='waypoint_coverage', label='途经点覆盖', pass_=True, detail='（无途经点）')
    visited_ids = set()
    for rid in plan['route_ids']:
        info = plan['rd'].get(rid, {})
        visited_ids.add(info.get('origin_id', ''))
        visited_ids.add(info.get('dest_id', ''))
    missing = []
    place_list = gt_data.get('core_places', [])
    for wid in wp_ids:
        if wid not in visited_ids:
            name = next((p.get('name', wid) for p in place_list if p.get('place_id') == wid), wid)
            missing.append(name)
    if missing:
        return MetricResult(metric='waypoint_coverage', label='途经点覆盖', pass_=False, detail=f"未途经: {', '.join(missing)}")
    expected_order = [place.get('place_id') for place in sorted((p for p in place_list if p.get('role') == 'waypoint'), key=lambda p: p.get('sequence', 0))]
    visited_order = []
    for route_id in plan['route_ids']:
        info = plan['rd'].get(route_id, {})
        for place_id in (info.get('origin_id', ''), info.get('dest_id', '')):
            if place_id and (not visited_order or visited_order[-1] != place_id):
                visited_order.append(place_id)
    actual_order = [place_id for place_id in visited_order if place_id in expected_order]
    if actual_order != expected_order:
        return MetricResult(metric='waypoint_coverage', label='途经点覆盖', pass_=False, detail='途经点顺序不符合任务要求')
    return MetricResult(metric='waypoint_coverage', label='途经点覆盖', pass_=True, detail='所有途经点已覆盖')

def check_mode_valid(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """No leg uses a forbidden transport mode."""
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='mode_valid', label='交通方式合规', pass_=True, detail='（不可行任务）')
    plan = _parse_plan(result, gt_data)
    if plan is None:
        return MetricResult(metric='mode_valid', label='交通方式合规', pass_=False, detail='未提交有效 plan')
    forbidden = plan['constraints'].get('forbidden_modes', [])
    if not forbidden:
        return MetricResult(metric='mode_valid', label='交通方式合规', pass_=True, detail='（无模式限制）')
    errors = []
    for (i, rid) in enumerate(plan['route_ids']):
        info = plan['rd'].get(rid, {})
        rmode = info.get('mode', '')
        if rmode in forbidden:
            errors.append(f'leg[{i}] {rmode}')
    if errors:
        return MetricResult(metric='mode_valid', label='交通方式合规', pass_=False, detail=f"使用了被禁止的方式: {', '.join(errors)}")
    return MetricResult(metric='mode_valid', label='交通方式合规', pass_=True, detail='所有段均合规')

def check_time_constraint(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Total duration (including stop) must be ≤ time_budget."""
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='time_constraint', label='时间约束', pass_=True, detail='（不可行任务）')
    plan = _parse_plan(result, gt_data)
    if plan is None:
        return MetricResult(metric='time_constraint', label='时间约束', pass_=False, detail='未提交有效 plan')
    time_budget_min = plan['constraints'].get('time_budget_min', 0)
    stop_min = plan['constraints'].get('stop_duration_min', 0)
    if time_budget_min == 0:
        return MetricResult(metric='time_constraint', label='时间约束', pass_=True, detail='（无时间限制）')
    durations = [plan['rd'].get(rid, {}).get('duration_s') for rid in plan['route_ids']]
    if any((not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0 for v in durations)):
        return MetricResult(metric='time_constraint', label='时间约束', pass_=False, detail='无法从 trace 获取每一段的有效路线耗时', verification_status='unverifiable')
    total_dur_s = sum(durations)
    if total_dur_s == 0:
        return MetricResult(metric='time_constraint', label='时间约束', pass_=False, detail='无法从 trace 获取路线耗时')
    total_min = total_dur_s / 60 + stop_min
    passed = total_dur_s + stop_min * 60 <= time_budget_min * 60
    detail = f"{total_min:.3f}min {('≤' if passed else '>')} {time_budget_min}min"
    if stop_min > 0:
        detail += f'（含 {stop_min}min 停留）'
    return MetricResult(metric='time_constraint', label='时间约束', pass_=passed, detail=detail)

def check_walking_constraint(result: AgentRunResult, gt_data: dict) -> MetricResult:
    """Total walking distance must be ≤ max_walking_distance_m."""
    if not gt_data.get('expected_feasible', True):
        return MetricResult(metric='walking_constraint', label='步行限制', pass_=True, detail='（不可行任务）')
    plan = _parse_plan(result, gt_data)
    if plan is None:
        return MetricResult(metric='walking_constraint', label='步行限制', pass_=False, detail='未提交有效 plan')
    max_walk = plan['constraints'].get('max_walking_distance_m')
    if max_walk is None:
        return MetricResult(metric='walking_constraint', label='步行限制', pass_=True, detail='（无步行限制）')
    all_routes = {r.get('route_id', ''): r for r in gt_data.get('all_routes_in_task', [])}
    total_walk_m = 0
    missing = 0
    for rid in plan['route_ids']:
        observed = plan['rd'].get(rid, {})
        if 'walking_distance_m' in observed:
            total_walk_m += observed['walking_distance_m']
            continue
        if observed.get('mode') == 'WALK' and 'distance_m' in observed:
            total_walk_m += observed['distance_m']
            continue
        r = all_routes.get(rid, {})
        walk = r.get('route', {}).get('walking_distance_m', 0)
        if walk == 0 and r.get('route', {}).get('distance_m', 0) > 0:
            pass
        total_walk_m += walk
        if rid not in all_routes:
            missing += 1
    if missing > 0:
        return MetricResult(metric='walking_constraint', label='步行限制', pass_=False, detail='部分路线缺少可核验的步行距离', verification_status='unverifiable')
    passed = total_walk_m <= max_walk
    detail = f"步行 {total_walk_m}m {('≤' if passed else '>')} {max_walk}m"
    return MetricResult(metric='walking_constraint', label='步行限制', pass_=passed, detail=detail)

def check_efficiency(result: AgentRunResult) -> MetricResult:
    """Count tool calls vs optimal minimum."""
    call_count = len([r for r in result.trace if r.tool not in {'final_answer', 'UserInterfaceApp__final_answer'}])
    wp_queries = sum((1 for rec in result.trace if rec.tool in {'query_routes', 'MapApp__query_routes'} and rec.args.get('origin_id', rec.args.get('origin')) != rec.args.get('dest_id', rec.args.get('destination'))))
    optimal = max(3, wp_queries + 2)
    if call_count <= optimal:
        return MetricResult(metric='tool_efficiency', label='工具调用效率', pass_=True, detail=f'{call_count} 次（≤ 最优 {optimal} 次）')
    return MetricResult(metric='tool_efficiency', label='工具调用效率', pass_=False, detail=f'{call_count} 次（最优 {optimal} 次，多了 {call_count - optimal} 次）')
