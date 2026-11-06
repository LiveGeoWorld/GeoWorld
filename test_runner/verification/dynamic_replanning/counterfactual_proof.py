from __future__ import annotations
from typing import Any
PROOF_SCHEMA_VERSION = 'geoword.dynamic_counterfactual_proof.v3'
LEGACY_PROOF_SCHEMA_VERSION = 'geoword.dynamic_counterfactual_proof.v2'

def validate_proof(request: dict[str, Any], proof: dict[str, Any]) -> dict[str, Any]:
    """Validate identity and P0-to-P1 evidence completeness."""
    errors: list[str] = []
    schema_version = request.get('schema_version')
    legacy = schema_version == LEGACY_PROOF_SCHEMA_VERSION
    if schema_version not in {PROOF_SCHEMA_VERSION, LEGACY_PROOF_SCHEMA_VERSION}:
        errors.append('invalid_proof_request_schema')
    if proof.get('schema_version') != schema_version:
        errors.append('invalid_proof_schema')
    for key in ('scenario_family_id', 'dynamic_level', 'materialized_event_graph_sha256'):
        if proof.get(key) != request.get(key):
            errors.append(f'mismatched_{key}')
    if proof.get('source') != request.get('source'):
        errors.append('mismatched_source')
    required_event_ids = set(request.get('materialized_event_ids') or [])
    impacts = proof.get('event_impacts') or []
    impact_ids = {str(item.get('event_id') or '') for item in impacts if isinstance(item, dict)}
    if impact_ids != required_event_ids:
        errors.append('incomplete_event_impact_coverage')
    route_boolean = str((request.get('source') or {}).get('task_type') or '') == 'route_planning'
    if any((not isinstance(item, dict) or (item.get('causally_changes_route_state') is not True if route_boolean else item.get('invalidates_or_changes_decision') is not True) or (not item.get('public_observation_path')) or (not isinstance(item.get('ablation'), dict)) or (item['ablation'].get('state_changed') is not True if route_boolean else item['ablation'].get('decision_changed') is not True if legacy else item['ablation'].get('state_changed') is not True and item['ablation'].get('decision_changed') is not True) for item in impacts)):
        errors.append('invalid_event_impact')
    if not proof.get('baseline_solution'):
        errors.append('missing_baseline_solution')
    if not proof.get('post_event_solution'):
        errors.append('missing_post_event_solution')
    baseline_identity = _proof_solution_identity(str((request.get('source') or {}).get('task_type') or ''), proof.get('baseline_solution') or {}, legacy=legacy)
    post_identity = _proof_solution_identity(str((request.get('source') or {}).get('task_type') or ''), proof.get('post_event_solution') or {}, legacy=legacy)
    selection = proof.get('target_selection') or {}
    selected_identity = selection.get('selected_p1_identity') if isinstance(selection, dict) else None
    if not isinstance(selected_identity, dict) or not selected_identity or selection.get('fixed_before_delta') is not True or (selection.get('selection_mode') != 'random_fixed_before_constraint_delta' if legacy else not isinstance(selection.get('selection_mode'), str) or not selection.get('selection_mode')):
        errors.append('invalid_fixed_p1_selection')
    difference = proof.get('dependency_difference') or {}
    if not isinstance(difference, dict) or difference.get('p1_identity') != selected_identity or difference.get('p0_identity') != baseline_identity or (legacy and difference.get('p0_identity') == selected_identity and (not route_boolean)) or (not isinstance(difference.get('changed_owned_fields'), list)) or (not difference.get('changed_owned_fields')):
        errors.append('invalid_dependency_difference')
    transition = proof.get('constraint_transition') or {}
    impact_deltas = [item.get('constraint_delta') for item in impacts if isinstance(item, dict) and isinstance(item.get('constraint_delta'), dict)]
    if not isinstance(transition.get('c0'), dict) or not isinstance(transition.get('c1'), dict) or (not isinstance(transition.get('deltas'), list)) or (not transition.get('deltas')) or (transition.get('deltas') != impact_deltas) or (difference.get('changed_owned_fields') != impact_deltas) or (transition.get('unmentioned_constraints_preserved') is not True):
        errors.append('invalid_constraint_transition')
    forward = proof.get('forward_verification') or {}
    if forward.get('fixed_p1_identity') != selected_identity or forward.get('solver_result_identity') != selected_identity or post_identity != selected_identity or (forward.get('matches_fixed_p1') is not True) or (forward.get('distinct_from_p0') is not True and (not route_boolean) if legacy else forward.get('distinct_from_p0') is not (baseline_identity != selected_identity)):
        errors.append('invalid_forward_verification')
    claims = proof.get('claims') or {}
    if any((claims.get(key) is not True for key in request.get('required_claims', {}))):
        errors.append('missing_required_claim')
    return {'valid': not errors, 'errors': sorted(set(errors))}

def _proof_solution_identity(base_task_type: str, solution: dict[str, Any], *, legacy: bool=False) -> dict[str, Any]:
    if base_task_type == 'multi_entity':
        field = 'service_id' if legacy else 'place_id'
        value = str(solution.get(field) or '')
        return {field: value} if value else {}
    if base_task_type == 'route_planning':
        feasible = solution.get('expected_feasible')
        return {'expected_feasible': feasible} if type(feasible) is bool else {}
    if base_task_type in {'itinerary', 'region_aware'}:
        values = [str(value) for value in solution.get('place_ids', []) or [] if str(value)]
        return {'place_ids': values} if values else {}
    if base_task_type == 'safety_planning':
        value = str(solution.get('place_id') or '')
        return {'place_id': value} if value else {}
    return {}
