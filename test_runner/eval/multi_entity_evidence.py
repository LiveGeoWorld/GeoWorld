"""Resolve explicit coordination evidence; never infer a plan from prose or GT."""
from __future__ import annotations
from dataclasses import replace
import math

def normalized_name(value):
    return ' '.join(str(value).casefold().split())

def participant_id(value, participants):
    """Public unique names and legacy IDs; conflicting/ambiguous identities fail closed."""
    if isinstance(value, dict):
        tokens = [value[k] for k in ('character_id', 'participant_name', 'name', 'participant') if k in value]
        if not tokens:
            return None
        resolved = [participant_id(token, participants) for token in tokens]
        return resolved[0] if resolved[0] and len(set(resolved)) == 1 else None
    if not isinstance(value, str) or not value.strip():
        return None
    matches = {p['character_id'] for p in participants if p.get('character_id') and (value == p['character_id'] or normalized_name(value) == normalized_name(p.get('name', '')))}
    return next(iter(matches)) if len(matches) == 1 else None

def participant_records(plan, participants):
    """Accept explicit lists only; all present lists must agree and cover everyone once."""
    expected = {p['character_id'] for p in participants if p.get('character_id')}
    chosen = None
    for field in ('assignments', 'participants', 'pickup_order'):
        if field not in plan:
            continue
        entries = plan[field]
        if not isinstance(entries, list) or not entries:
            return (None, 'unverifiable', f'{field} 缺少可解析的人员列表')
        ids = [participant_id(entry, participants) for entry in entries]
        if any((identity is None for identity in ids)):
            return (None, 'unverifiable', f'{field} 含未知、重名或非结构化人物描述')
        if len(ids) != len(set(ids)) or set(ids) != expected:
            return (None, '', f'{field} 人员缺失、重复或包含额外人物')
        if chosen is None:
            chosen = dict(zip(ids, entries))
    if not expected or chosen is None:
        return (None, 'unverifiable', '没有可核验的逐人安排列表')
    return (chosen, '', '')

def observed_commute_seconds(result, gt_data):
    """Sum explicit per-person selected legs, uniquely matched to route observations.

    Uses the existing public route resolver; GT never supplies missing routes or
    chooses among alternatives. No natural-language extraction or shortest-path fallback.
    """
    from test_runner.eval.route_plan_evidence import public_route_plan
    answer = result.final_answer or {}
    plan = answer.get('plan') if isinstance(answer.get('plan'), dict) else {}
    (records, status, reason) = participant_records(plan, gt_data.get('participants', []))
    if records is None:
        return (None, status or 'unverifiable', reason)
    total = 0
    for (identity, entry) in records.items():
        if not isinstance(entry, dict):
            return (None, 'unverifiable', '人员列表没有逐人提交路线')
        leg_plan = entry.get('commute', entry.get('plan', entry))
        if not isinstance(leg_plan, dict):
            return (None, 'unverifiable', '逐人路线结构无效')
        leg_plan = dict(leg_plan)
        if not leg_plan.get('mode'):
            leg_plan['mode'] = plan.get('mode') or answer.get('mode')
        per_person = replace(result, final_answer={'plan': leg_plan})
        resolved = public_route_plan(per_person, {})
        if resolved is None:
            return (None, 'unverifiable', f'{identity} 的所选路段不能唯一关联到 Observation')
        previous = None
        for rid in resolved['route_ids']:
            leg = resolved['rd'][rid]
            if previous is not None and previous != leg['origin_id']:
                return (None, '', '提交的逐人路线不连续')
            previous = leg['dest_id']
            if leg.get('available') is False:
                return (None, '', '提交的逐人路线包含不可用路段')
            seconds = leg.get('duration_s')
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or (not math.isfinite(seconds)) or (seconds <= 0):
                return (None, 'unverifiable', '路线 Observation 缺少有效耗时')
            total += seconds
    return (total, '', '')
