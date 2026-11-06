"""Judge sequence evaluator for GEOBench traces."""
from __future__ import annotations
import math
import re
import unicodedata
from typing import Any

def evaluate_judge_sequence(judge_sequence: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate a GEOBench judge sequence against recorded app events."""
    policy = judge_sequence.get('completion_policy', 'key_nodes')
    stage_reports = []
    key_node_reports = []
    for stage in judge_sequence.get('stages', []):
        node_reports = []
        for node in stage.get('nodes', []):
            event = _find_event_for_node(events, node)
            hard = [_eval_check(check, event, node) for check in node.get('hard_checks', [])]
            soft = [_eval_check(check, event, node) for check in node.get('soft_checks', [])]
            hard_pass = all((item['passed'] for item in hard)) if hard else True
            soft_pass = sum((1 for item in soft if item['passed']))
            report = {'node_id': node.get('node_id', ''), 'label_zh': node.get('label_zh', ''), 'role': node.get('role', ''), 'verdict_target': node.get('verdict_target', ''), 'is_key': bool(node.get('is_key')), 'agent_action': node.get('agent_action', ''), 'matched_event_step': event.get('step') if event else None, 'hard_pass': hard_pass, 'soft_pass_count': soft_pass, 'soft_total': len(soft), 'hard_checks': hard, 'soft_checks': soft}
            report['failure_type'] = _failure_type(report, event)
            node_reports.append(report)
            if report['is_key']:
                key_node_reports.append(report)
        stage_reports.append({'stage_id': stage.get('stage_id', ''), 'description': stage.get('description', ''), 'nodes': node_reports})
    if policy == 'key_nodes':
        success = bool(key_node_reports) and all((item['hard_pass'] for item in key_node_reports))
    else:
        all_nodes = [node for stage in stage_reports for node in stage['nodes']]
        success = bool(all_nodes) and all((item['hard_pass'] for item in all_nodes))
    hard_total = sum((len(node['hard_checks']) for stage in stage_reports for node in stage['nodes']))
    hard_passed = sum((1 for stage in stage_reports for node in stage['nodes'] for check in node['hard_checks'] if check['passed']))
    soft_total = sum((node['soft_total'] for stage in stage_reports for node in stage['nodes']))
    soft_passed = sum((node['soft_pass_count'] for stage in stage_reports for node in stage['nodes']))
    tool_nodes = [node for node in key_node_reports if node.get('verdict_target') == 'final_grounded_location']
    user_nodes = [node for node in key_node_reports if node.get('verdict_target') == 'final_user_response']
    missing_key_nodes = [node['node_id'] for node in key_node_reports if node.get('matched_event_step') is None]
    return {'success': success, 'score': 1.0 if success else 0.0, 'completion_policy': policy, 'key_node_count': len(key_node_reports), 'matched_key_count': sum((1 for node in key_node_reports if node.get('matched_event_step') is not None)), 'missing_key_nodes': missing_key_nodes, 'key_operation_success': bool(key_node_reports) and all((item['hard_pass'] for item in key_node_reports)), 'tool_grounded_success': _target_success(tool_nodes), 'user_facing_success': _target_success(user_nodes), 'hard_passed': hard_passed, 'hard_total': hard_total, 'soft_passed': soft_passed, 'soft_total': soft_total, 'stages': stage_reports, 'rationale': _rationale(success, key_node_reports)}

def _find_event_for_node(events: list[dict[str, Any]], node: dict[str, Any]) -> dict[str, Any] | None:
    node_id = node.get('node_id')
    action = node.get('agent_action')
    for event in events:
        if node_id and event.get('node_id') == node_id:
            return event
    candidates = [event for event in events if action and event.get('agent_action') == action]
    if not candidates:
        return None
    return max(candidates, key=lambda event: _event_match_score(event, node))

def _event_match_score(event: dict[str, Any], node: dict[str, Any]) -> tuple[int, int, int]:
    hard = [_eval_check(check, event, node)['passed'] for check in node.get('hard_checks', [])]
    soft = [_eval_check(check, event, node)['passed'] for check in node.get('soft_checks', [])]
    return (sum((1 for passed in hard if passed)), sum((1 for passed in soft if passed)), event.get('step', 0))

def _eval_check(check: dict[str, Any], event: dict[str, Any] | None, node: dict[str, Any]) -> dict[str, Any]:
    check_type = check.get('check_type') or check.get('field') or ''
    if check_type == 'action_executed':
        passed = event is not None and event.get('agent_action') == node.get('agent_action')
        return _check_report(check, passed, 'matched action' if passed else 'missing action')
    if event is None:
        return _check_report(check, False, 'no matching event')
    if check_type == 'field_not_empty':
        values = _field_values(event, check.get('field', ''))
        passed = any((_not_empty(value) for value in values))
        return _check_report(check, passed, values)
    if check_type == 'field_contains':
        values = _field_values(event, check.get('field', ''))
        expected = check.get('expected')
        expected_parts = _expected_parts(expected)
        haystack = '\n'.join((str(value) for value in values))
        passed = all((_text_contains(haystack, part) for part in expected_parts)) if expected_parts else True
        return _check_report(check, passed, {'expected': expected_parts, 'actual': values})
    if check_type == 'field_equals':
        values = _field_values(event, check.get('field', ''))
        expected = check.get('expected')
        passed = any((value == expected for value in values))
        return _check_report(check, passed, {'expected': expected, 'actual': values})
    if check_type == 'geo_within_radius':
        values = _field_values(event, check.get('field', ''))
        values.extend(_field_values(event, 'response.places[].location'))
        expected = check.get('expected') or {}
        radius = (check.get('tolerance') or {}).get('radius_m', 0)
        distances = [_distance_m(expected, value) for value in values if isinstance(value, dict) and 'lat' in value and ('lng' in value)]
        passed = any((distance <= radius for distance in distances)) if radius else False
        return _check_report(check, passed, {'radius_m': radius, 'distances_m': distances})
    return _check_report(check, True, f'unchecked check_type={check_type}')

def _target_success(nodes: list[dict[str, Any]]) -> bool | None:
    if not nodes:
        return None
    return all((node.get('hard_pass') for node in nodes))

def _failure_type(report: dict[str, Any], event: dict[str, Any] | None) -> str:
    if report.get('hard_pass'):
        return ''
    if event is None:
        action = report.get('agent_action', '')
        if action == 'MapApp.search_grounding_candidates':
            return 'map_recall_missing'
        if action == 'MapApp.select_grounded_location':
            return 'candidate_selection_missing'
        if action == 'UserInterfaceApp.send_message_to_user':
            return 'final_output_missing'
        return 'missing_key_operation' if report.get('is_key') else 'missing_operation'
    failed_checks = [check for check in report.get('hard_checks', []) if not check.get('passed')]
    failed_types = {check.get('check_type') for check in failed_checks}
    if report.get('verdict_target') == 'final_grounded_location':
        if 'field_equals' in failed_types:
            return 'candidate_selection_failure'
        if 'geo_within_radius' in failed_types:
            return 'spatial_grounding_failure'
    if report.get('verdict_target') == 'final_user_response':
        if 'field_contains' in failed_types:
            return 'final_output_failure'
    if 'field_not_empty' in failed_types:
        return 'map_recall_failure'
    return 'hard_check_failure'

def _field_values(event: dict[str, Any], field: str) -> list[Any]:
    if not field:
        return [event]
    parts = field.split('.')
    current: list[Any] = [event]
    for part in parts:
        next_values: list[Any] = []
        is_array = part.endswith('[]')
        key = part[:-2] if is_array else part
        for value in current:
            if isinstance(value, dict):
                child = value.get(key)
            else:
                child = None
            if is_array and isinstance(child, list):
                next_values.extend(child)
            elif child is not None:
                next_values.append(child)
        current = next_values
    return current

def _expected_parts(expected: Any) -> list[str]:
    if expected is None:
        return []
    if isinstance(expected, list):
        return [str(item) for item in expected if str(item)]
    return [part.strip() for part in str(expected).split(',') if part.strip()]

def _text_contains(haystack: str, expected: str) -> bool:
    actual = _norm_text(haystack)
    target = _norm_text(expected)
    if not target:
        return True
    if target in actual:
        return True
    relaxed = target.replace('的原因', '').replace('原因', '').replace('能', '')
    return bool(relaxed and relaxed in actual)

def _norm_text(value: str) -> str:
    text = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return re.sub('[\\s，。、“”\\"\'：:；;（）()\\[\\]【】\\-_/]+', '', text)

def _not_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict, str)):
        return len(value) > 0
    return True

def _check_report(check: dict[str, Any], passed: bool, observed: Any) -> dict[str, Any]:
    return {'check_type': check.get('check_type', ''), 'field': check.get('field', ''), 'expected': check.get('expected'), 'tolerance': check.get('tolerance'), 'passed': passed, 'observed': observed}

def _distance_m(a: dict[str, Any], b: dict[str, Any]) -> float:
    (lat1, lng1) = (math.radians(float(a.get('lat', 0))), math.radians(float(a.get('lng', 0))))
    (lat2, lng2) = (math.radians(float(b.get('lat', 0))), math.radians(float(b.get('lng', 0))))
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return round(6371000 * 2 * math.atan2(math.sqrt(h), math.sqrt(1 - h)), 3)

def _rationale(success: bool, key_node_reports: list[dict[str, Any]]) -> str:
    if success:
        return 'All key judge nodes passed hard checks.'
    failed = [node['node_id'] for node in key_node_reports if not node['hard_pass']]
    return f"Failed key judge nodes: {(', '.join(failed) if failed else '(none found)')}"
