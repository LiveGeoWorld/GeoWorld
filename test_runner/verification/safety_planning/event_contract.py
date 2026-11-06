from __future__ import annotations
import copy
import math
from itertools import combinations
from typing import Any
from test_runner.verification.safety_planning.spacetime import TimeWindow
from test_runner.verification.safety_planning.task_time import applies, moment

def validate_event(event: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if 'events' in event:
        from test_runner.verification.safety_planning.event_bundle import events, materialize
        materialize(event, context)
        for item in events(event):
            validate_event(item, context)
            modes = [e.get('transportation_mode') for e in item['effects'] if e['kind'] == 'delay']
            if len(modes) != len(set(modes)):
                raise ValueError('One event cannot duplicate a mode delay')
        for (first, second) in combinations(events(event), 2):
            if first['scope_ref'] == second['scope_ref']:
                kinds = {e['kind'] for item in (first, second) for e in item['effects']}
                if kinds == {'unavailable', 'delay'}:
                    raise ValueError('Same scope cannot be closed and passable with delay')
        return copy.deepcopy(event)
    if event.get('cannot_construct') is True:
        raise ValueError('Event Agent could not construct a grounded distinction')
    family = event.get('event_kind')
    if family not in {'weather', 'mobility', 'infrastructure', 'environmental', 'public_safety'}:
        raise ValueError('Unknown Safety event family')
    if event.get('app') not in {'WeatherApp', 'CityNewsApp'}:
        raise ValueError('Event must use an available public information App')
    anchors = {item['scope_ref']: item for item in context['geographic_anchors']}
    if event.get('scope_ref') not in anchors:
        raise ValueError('Event must reference a frozen geographic anchor')
    scope = anchors[event['scope_ref']]
    if not scope.get('source_refs'):
        raise ValueError('Event scope lacks geographic evidence')
    window = TimeWindow(**event['active_window'])
    if not applies(event['active_window'], moment(context)):
        raise ValueError('Event does not overlap the activity')
    if any((not isinstance(event.get(field), str) or not event[field].strip() for field in ('title', 'description'))):
        raise ValueError('Event needs public title and description')
    profiles = context['candidate_profiles']
    fields = {field for profile in profiles for field in profile['facts']}
    effects = event.get('effects')
    if not isinstance(effects, list) or not effects:
        raise ValueError('Event must specify its observable effect')
    for effect in effects:
        if not isinstance(effect, dict) or effect.get('kind') not in {'unavailable', 'delay'}:
            raise ValueError('Event effect is unsupported')
        conditions = effect.get('where')
        if not isinstance(conditions, list):
            raise ValueError('Exposure conditions must be a list')
        for condition in conditions:
            if not isinstance(condition, dict) or condition.get('field') not in fields or 'equals' not in condition:
                raise ValueError('Exposure must reference a supplied non-identity fact')
            field = condition['field']
            if not any((field in profile['facts'] and profile['facts'][field] == condition['equals'] for profile in profiles)):
                raise ValueError('Exposure value is absent from supplied facts')
        if not isinstance(effect.get('reason'), str) or not effect['reason'].strip():
            raise ValueError('Event effect requires an explanation')
        if effect['kind'] == 'delay':
            delay = effect.get('delay_minutes')
            if type(delay) not in (int, float) or not math.isfinite(delay) or delay <= 0:
                raise ValueError('Delay must be a positive finite duration')
            mode = effect.get('transportation_mode')
            if mode not in context.get('constrained_modes', []):
                raise ValueError('Delay mode must participate in ordinary C')
            if scope.get('exposure_basis') != 'route':
                raise ValueError('Delay requires resolved route scope, not destination membership')
    return copy.deepcopy(event)

def apply_event(event: dict[str, Any], profiles: list[dict[str, Any]], *, scope_members: set[str], activity_window: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Project explicit author effects using already-resolved private membership."""
    if 'events' in event:
        from test_runner.verification.safety_planning.event_bundle import events
        merged = {p['candidate_ref']: {'unavailable': False, 'delay_minutes': {}, 'matched_effects': [], 'unknown_effects': []} for p in profiles}
        for (index, item) in enumerate(events(event)):
            rows = apply_event(item, profiles, scope_members=scope_members[index], activity_window=activity_window)
            for (ref, row) in rows.items():
                dest = merged[ref]
                dest['unavailable'] |= row['unavailable']
                for (mode, delay) in row['delay_minutes'].items():
                    dest['delay_minutes'][mode] = dest['delay_minutes'].get(mode, 0) + delay
                for key in ('matched_effects', 'unknown_effects'):
                    dest[key].extend(({'event_index': index, 'effect_index': number} for number in row[key]))
        return merged
    active = applies(event['active_window'], activity_window)
    result = {}
    for profile in profiles:
        ref = profile['candidate_ref']
        row: dict[str, Any] = {'unavailable': False, 'delay_minutes': {}, 'matched_effects': [], 'unknown_effects': []}
        if active and ref in scope_members:
            for (index, effect) in enumerate(event['effects']):
                if any((condition['field'] in profile['facts'] and profile['facts'][condition['field']] != condition['equals'] for condition in effect['where'])):
                    continue
                if any((condition['field'] not in profile['facts'] for condition in effect['where'])):
                    row['unknown_effects'].append(index)
                    continue
                row['matched_effects'].append(index)
                if effect['kind'] == 'unavailable':
                    row['unavailable'] = True
                else:
                    mode = effect['transportation_mode']
                    row['delay_minutes'][mode] = row['delay_minutes'].get(mode, 0) + effect['delay_minutes']
        result[ref] = row
    return result

def public_event(event: dict[str, Any], anchor: dict[str, Any]) -> dict[str, Any]:
    """Explicit public whitelist: no candidate references, evidence, or impact map."""
    return {'info_type': 'notice', 'category': event['event_kind'], 'title': event['title'], 'description': event['description'], 'scope': copy.deepcopy(anchor['public_scope']), 'active_window': copy.deepcopy(event['active_window'])}
