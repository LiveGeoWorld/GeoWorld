from __future__ import annotations
import math
from pathlib import Path
from shared.io import read_json
from test_runner.verification.region_aware.generator import _region_constraint_matches_place, _region_district_names, _region_weekday_index, _stage3_route_for, _stage3_route_index
from test_runner.verification.safety_planning.choice_pipeline import prove_choice
from test_runner.verification.safety_planning.evidence_index import SafetyEvidenceIndex
from test_runner.verification.safety_planning.event_contract import public_event
from test_runner.verification.safety_planning.event_view import read_event_view
from test_runner.verification.safety_planning.task_time import moment, time_fields
from test_runner.verification.safety_planning.opening_hours import open_at
from test_runner.verification.safety_planning.event_bundle import events

def verify_decision(decision: dict) -> dict:
    """Reproduce current-task C and event arithmetic, never solve prior tasks."""
    places = {p['place_id']: p for p in decision['world']['places']}
    identities = decision['identities']
    if len(set(identities.values())) != len(identities) or not set(identities.values()) <= places.keys():
        raise ValueError('Safety identity map must bind distinct frozen places')
    activity = decision['activity_place_ids']
    if len(set(activity)) != len(activity) or not set(activity) <= places.keys():
        raise ValueError('Safety activity candidates must be distinct frozen places')
    constraints = decision['public_place_constraints']
    context = dict(character=decision['character'], route_index=_stage3_route_index(decision['route_cache']), district_names=_region_district_names(decision['world']), weekday_index=_region_weekday_index(decision['time_context']))
    ordinary = {pid for pid in activity if all((_region_constraint_matches_place(places[pid], c, **context) for c in constraints))}
    if 'task_time' in decision:
        if decision['time_context']['instruction_time'] != decision['task_time']:
            raise ValueError('Safety task instant differs from its time context')
        ordinary = {pid for pid in ordinary if open_at(places[pid], decision['task_time']) is True}
    refs = decision['proof']['ordinary_candidates']
    if set(refs) != set(identities) or {identities[r] for r in refs} != ordinary:
        raise ValueError('Safety proof omits or adds candidates retained by ordinary C')
    target = next((ref for (ref, pid) in identities.items() if pid == decision['target_place_id']), None)
    if target is None:
        raise ValueError('Safety fixed target is missing from ordinary candidates')
    records = decision['events']
    multi = decision.get('event_contract') in {'safety_events.v1', 'safety_events.v2'}
    if decision.get('event_contract') == 'safety_events.v2':
        if len(records) != len(identities) - 1 or not records:
            raise ValueError('Event count must equal distractor count')
    elif not 1 <= len(records) <= (2 if multi else 1):
        raise ValueError('Safety choice has an invalid event count')
    record = records[0]
    scope = set(record['scope_place_ids'])
    if any((not set(r['scope_place_ids']) <= ordinary for r in records)):
        raise ValueError('Safety scope mapping references non-candidates')
    routes = {}
    for (ref, pid) in identities.items():
        routes[ref] = {}
        for c in constraints:
            if c['type'] == 'route_duration_range':
                mode = c['transportation_mode']
                entry = _stage3_route_for(context['route_index'], decision['character'], places[pid], mode)
                raw = ((entry or {}).get('route') or {}).get('duration_s')
                if raw is None:
                    raise ValueError('Safety route constraint has no frozen route')
                routes[ref][mode] = max(1, math.ceil(float(raw) / 60))
    proof = prove_choice(target_ref=target, ordinary_refs=refs, event={'events': [r['event'] for r in records]} if multi else record['event'], profiles=[{'candidate_ref': ref, 'facts': decision['profiles_by_place_id'][pid]} for (ref, pid) in identities.items()], scope_members=[{ref for (ref, pid) in identities.items() if pid in r['scope_place_ids']} for r in records] if multi else {ref for (ref, pid) in identities.items() if pid in scope}, activity_window=moment(decision), constraints=constraints, route_minutes=routes)
    if proof != decision['proof']:
        raise ValueError('Safety stored proof differs from its frozen current-task facts')
    return proof

def validate_choice_folder(task_dir: Path, task: dict, private: dict, static_state: dict) -> list[str]:
    try:
        decision = private['safety_choice']
        verify_decision(decision)
        if decision['time_context'] != task['time_context']:
            raise ValueError('Safety task time context differs from the frozen decision')
        binding = private['safety_event_view']
        relative = Path(binding['path'])
        view_path = (task_dir / relative).resolve()
        if relative.is_absolute() or not view_path.is_relative_to(task_dir.resolve()):
            raise ValueError('Safety event view must stay task-local')
        apps = read_event_view(read_json(view_path), world_binding=binding['world_binding'])
        index = SafetyEvidenceIndex.from_dict(read_json(task_dir / 'safety_evidence_history.json'), world_binding=binding['world_binding'])
        entry = next((r for r in index.entries if r['owner'] == task['task_id']))
        context = entry['context']
        if time_fields(context) != time_fields(decision) or context['public_place_constraints'] != decision['public_place_constraints']:
            raise ValueError('Safety decision differs from its source-bound history')
        for row in context['candidate_profiles']:
            if decision['profiles_by_place_id'][decision['identities'][row['candidate_ref']]] != row['facts']:
                raise ValueError('Safety exposure facts differ from history')
        items = events(entry['event'])
        if len(items) != len(decision['events']):
            raise ValueError('Decision event count differs from history')
        for (item, record) in zip(items, decision['events']):
            anchor = next((a for a in context['geographic_anchors'] if a['scope_ref'] == item['scope_ref']))
            exposure_key = 'route_exposure' if anchor.get('exposure_basis') == 'route' else 'place_exposure'
            if exposure_key in item:
                expected_members = {decision['identities'][r['candidate_ref']] for r in item[exposure_key] if r['status'] == 'intersects'}
                if set(record['scope_place_ids']) != expected_members:
                    raise ValueError('Decision scope membership differs from verified event exposure')
            expected = public_event(item, anchor)
            if item != record['event'] or record['public_event'] != expected:
                raise ValueError('Safety public notice differs from the frozen event')
            app = item['app']
            if expected not in apps[app] or expected not in static_state['apps'][app]['entries']:
                raise ValueError('Safety task-defining notice is absent from the public App data')
        return []
    except (ValueError, KeyError, TypeError, StopIteration, OSError) as exc:
        return [f'safety choice contract: {exc}']
