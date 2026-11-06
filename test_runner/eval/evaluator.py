"""RouteTaskEvaluator — evaluates agent plan against constraint-based GT.

Reads route_sandbox.json (GT constraints only, not exposed to agent).
Seven fixed metrics; at least five passes and correct feasibility are required.
"""
from __future__ import annotations
from shared.io import read_json
from test_runner.schema import AgentRunResult, MetricResult
from test_runner.eval.metrics import check_feasible_consistency, check_plan_structure, check_waypoint_coverage, check_mode_valid, check_time_constraint, check_walking_constraint, check_efficiency

def evaluate(result: AgentRunResult, gt_path: str) -> list[MetricResult]:
    """Run constraint-based evaluation with independent per-constraint metrics."""
    gt_data = read_json(gt_path)
    if type(gt_data.get('expected_feasible')) is not bool:
        raise ValueError('Route GT must contain a boolean expected_feasible')
    metrics = [check_feasible_consistency(result, gt_data), check_plan_structure(result, gt_data), check_waypoint_coverage(result, gt_data), check_mode_valid(result, gt_data), check_time_constraint(result, gt_data), check_walking_constraint(result, gt_data), check_efficiency(result)]
    metrics[0].label = '可行性一致性（必过项）'
    for metric in metrics[1:]:
        if metric.detail == '（不可行任务）':
            metric.verification_status = 'not_applicable'
            metric.pass_ = metrics[0].pass_
            metric.detail = '本题 GT 不可行且最终判断正确，该路线约束不要求提交可行方案，按分支通过。' if metrics[0].pass_ else '本题 GT 不可行，但未正确提交不可行判断，该项不计通过。'
        elif metric.detail == '未提交有效 plan':
            metric.verification_status = 'unverifiable'
            metric.detail = '无法自动核验：最终答案没有能唯一关联到实际查询路线的逐段名称、坐标、方式或路线引用。不使用 GT 代选路线；本项保留在固定 7 项中，不计通过。'
    result.metrics = metrics
    result.total_metrics = 7
    result.passed_metrics = sum((m.pass_ for m in metrics))
    return metrics
