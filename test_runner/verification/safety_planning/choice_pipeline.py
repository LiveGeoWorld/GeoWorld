from __future__ import annotations
from typing import Any
from test_runner.verification.safety_planning.event_contract import apply_event
from test_runner.verification.safety_planning.construction_errors import ConstructionRejected

def prove_choice(*, target_ref: str, ordinary_refs: list[str], event: dict[str, Any], profiles: list[dict[str, Any]], scope_members: set[str], activity_window: dict[str, str], constraints: list[dict[str, Any]], route_minutes: dict[str, dict[str, float]]) -> dict[str, Any]:
    if target_ref not in ordinary_refs or len(set(ordinary_refs)) != len(ordinary_refs) or len(ordinary_refs) < 2:
        raise ValueError('Ordinary C must retain the fixed target and a distinct competitor')
    by_ref = {profile['candidate_ref']: profile for profile in profiles}
    if set(by_ref) != set(ordinary_refs):
        raise ValueError('Every ordinary candidate needs a factual exposure profile')
    effects = apply_event(event, profiles, scope_members=scope_members, activity_window=activity_window)
    accepted = []
    for ref in ordinary_refs:
        effect = effects[ref]
        if effect['unknown_effects']:
            raise ValueError('Unknown exposure cannot prove a safe or excluded candidate')
        failures = []
        if effect['unavailable']:
            failures.append({'reason': 'activity_unavailable', 'effect_indices': effect['matched_effects']})
        for constraint in constraints:
            if constraint['type'] != 'route_duration_range':
                continue
            mode = constraint['transportation_mode']
            baseline = route_minutes.get(ref, {}).get(mode)
            if baseline is None:
                raise ValueError('Ordinary route requirement lacks a frozen route')
            if not constraint['minimum'] <= baseline <= constraint['maximum']:
                raise ValueError('Candidate did not satisfy ordinary C before the event')
            adjusted = baseline + effect['delay_minutes'].get(mode, 0)
            if not constraint['minimum'] <= adjusted <= constraint['maximum']:
                failures.append({'reason': 'route_duration_outside_C', 'constraint_id': constraint['constraint_id'], 'baseline_minutes': baseline, 'effective_minutes': adjusted})
        effect['failures'] = failures
        if not failures:
            accepted.append(ref)
    if accepted != [target_ref]:
        raise ConstructionRejected('C plus the event must uniquely retain the original fixed target')
    return {'ordinary_candidates': ordinary_refs, 'accepted_candidates': accepted, 'candidate_effects': effects, 'decision_changed': True}
