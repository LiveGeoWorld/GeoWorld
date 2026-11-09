"""Typed, deterministic added C. No natural-language interpretation."""
import math
import re

MODES = ('WALK', 'TRANSIT', 'DRIVE')


def validate_constraints(constraints, participants):
    if not isinstance(constraints, list):
        raise ValueError('additional_constraints is not a list')
    people = {'all_participants'} | {p['name'] for p in participants}
    objects = {'activity', 'place'} | people
    for index, c in enumerate(constraints):
        if not isinstance(c, dict) or set(c) != {'object', 'attribute', 'relation', 'value'}:
            raise ValueError(f'Constraint {index}: invalid object/attribute/relation/value structure')
        if c['object'] not in objects:
            raise ValueError(f'Constraint {index}: unknown object {c["object"]}')
        attribute, relation, value = c['attribute'], c['relation'], c['value']
        if attribute == 'start_time' and c['object'] == 'activity':
            if relation not in ('le', 'ge', 'eq') or not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
                raise ValueError(f'Constraint {index}: invalid activity start_time relation or HH:MM value')
        elif attribute == 'google_types' and c['object'] == 'place':
            if relation != 'contains' or not isinstance(value, str) or not value.strip():
                raise ValueError(f'Constraint {index}: invalid google_types membership')
        elif attribute == 'commute_min' and c['object'] in people:
            if relation not in ('le', 'ge', 'eq') or type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError(f'Constraint {index}: invalid commute_min relation or finite minute value')
        elif attribute == 'travel_mode' and c['object'] in people:
            if relation != 'in' or not isinstance(value, list) or not value or any(v not in MODES for v in value):
                raise ValueError(f'Constraint {index}: invalid travel_mode relation or mode list')
        else:
            raise ValueError(f'Constraint {index}: computation unsupported for object={c["object"]}, attribute={attribute}')


def compare(actual, relation, expected):
    return {'le': actual <= expected, 'ge': actual >= expected, 'eq': actual == expected}[relation]


def route_options(person, activity, constraints, available):
    relevant = [c for c in constraints if c['object'] in ('all_participants', person['name'])]
    allowed = set(activity.get('allowed_travel_modes', MODES))
    for c in relevant:
        if c['attribute'] == 'travel_mode':
            allowed &= set(c['value'])
    values = {mode: value for mode, value in available.items() if mode in allowed}
    if not values:
        return {}, {'participant': person['name'], 'required_modes': sorted(allowed),
                    'available_route_facts': available}, []
    numeric = [c for c in relevant if c['attribute'] == 'commute_min']
    if 'max_commute_min' in activity:
        numeric.append({'object': 'all_participants', 'attribute': 'commute_min', 'relation': 'le',
                        'value': activity['max_commute_min']})
    eligible = {mode: value for mode, value in values.items()
                if all(compare(value, c['relation'], c['value']) for c in numeric)}
    failures = [] if eligible else [{'constraint': 'commute_min', 'participant': person['name'],
                                     'conditions': numeric, 'actual_route_minutes': values}]
    return eligible, None, failures


def start_interval(constraints, earliest, latest):
    requirements = [c for c in constraints if c['object'] == 'activity']
    for c in requirements:
        hour, minute = map(int, c['value'].split(':'))
        bound = hour * 60 + minute
        if c['relation'] in ('ge', 'eq'):
            earliest = max(earliest, bound)
        if c['relation'] in ('le', 'eq'):
            latest = min(latest, bound)
    return earliest, latest, requirements
