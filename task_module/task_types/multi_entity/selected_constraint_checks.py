"""Read-only per-C comparison using original public coordination predicates.

No sampling, C construction, fixed-GT equality or uniqueness test.
"""
from copy import deepcopy
from .type_facts import matches_place_type

def compare_selected_C(facts, venue, routes, *, legacy=False):
    """Continue all selected conditions; never let an attribute failure skip time."""
    import math
    from task_module.task_types.multi_entity.solver import _clock_minutes
    from task_module.task_types.multi_entity.constraint_protocol import route_options, start_interval, validate_constraints
    from task_module.task_types.multi_entity.legacy_choice_verification import LEGACY_BUDGET_CAP
    checks = []
    def add(c, actual, passed):
        checks.append({'place_id': venue['place_id'], 'constraint': deepcopy(c),
                       'actual': deepcopy(actual), 'satisfied': passed})
    people = facts['participants']
    activity = facts['meeting'] if legacy else facts['activity']
    fact = next((v for v in facts.get('candidate_venues', []) if v['name'] == venue['name']), {}) if legacy else venue
    added = facts.get('additional_constraints', [])
    validate_constraints(added, people)
    if legacy:
        if 'max_party' in fact:
            capacity = fact['max_party']
            add({'type': 'party_size', 'minimum': len(people)}, capacity,
                capacity >= len(people) if type(capacity) in (int, float) else None)
        dietary = {p.get('dietary') for p in people} - {None, '', 'none', '无'}
        if dietary:
            options = fact.get('dietary_options')
            add({'type': 'dietary', 'required': sorted(dietary)}, options,
                dietary <= set(options) if isinstance(options, list) else None)
        for person in people:
            budget = person.get('budget')
            cap = budget if type(budget) in (int, float) else LEGACY_BUDGET_CAP.get(budget)
            if cap is not None:
                price = fact.get('price_per_person')
                add({'type': 'budget', 'participant': person['name'], 'maximum': cap,
                     'original_budget': budget, 'rule_source': 'original_legacy_budget_grade'}, price,
                    price <= cap if type(price) in (int, float) else None)
    else:
        expected = activity['required_place_type']
        add({'type': 'required_place_type', 'value': expected},
            {'primary_type': venue.get('primary_type'), 'types': venue.get('types', [])},
            matches_place_type(venue, expected))
        for c in added:
            if c['object'] == 'place':
                types = venue.get('types')
                add(c, types, c['value'] in types if isinstance(types, list) and types else None)
    arrivals, travel_evidence = [], []
    for person in people:
        key = f"{person['departure_area']}→{venue['name']}"
        available = {mode: value for mode, value in routes.get(key, {}).items()
                     if type(value) in (int, float) and math.isfinite(value) and value >= 0
                     and (legacy or mode in ('WALK', 'TRANSIT', 'DRIVE'))}
        eligible = available
        if not legacy:
            eligible, gap, failures = route_options(person, activity, added, available)
            relevant = [c for c in added if c['object'] in ('all_participants', person['name'])]
            if 'allowed_travel_modes' in activity:
                add({'type': 'allowed_travel_modes', 'participant': person['name'], 'value': activity['allowed_travel_modes']},
                    available, bool(set(available) & set(activity['allowed_travel_modes'])) if available else None)
            if 'max_commute_min' in activity:
                relevant = relevant + [{'object': person['name'], 'attribute': 'commute_min',
                                         'relation': 'le', 'value': activity['max_commute_min']}]
            from task_module.task_types.multi_entity.constraint_protocol import compare
            for c in relevant:
                if c['attribute'] == 'travel_mode':
                    passed = bool(set(available) & set(c['value'])) if available else None
                else:
                    allowed = set(activity.get('allowed_travel_modes', ('WALK', 'TRANSIT', 'DRIVE')))
                    for mode_c in relevant:
                        if mode_c['attribute'] == 'travel_mode':
                            allowed &= set(mode_c['value'])
                    values = [v for mode, v in available.items() if mode in allowed]
                    passed = any(compare(v, c['relation'], c['value']) for v in values) if values else None
                add(c, available, passed)
        if eligible:
            minutes = min(eligible.values())
            arrivals.append(_clock_minutes(person['available_from']) + minutes)
            travel_evidence.append({'participant': person['name'], 'route_key': key,
                                    'route_options': available, 'travel_min': minutes})
        else:
            travel_evidence.append({'participant': person['name'], 'route_key': key, 'route_options': available})
    duration = activity.get('duration_min')
    earliest = latest = base_earliest = base_latest = None
    if len(arrivals) == len(people):
        span = duration if duration is not None else 0
        earliest = max(arrivals)
        latest = min(_clock_minutes(p['must_leave_by']) - span for p in people)
        if legacy and fact.get('open_until'):
            latest = min(latest, _clock_minutes(fact['open_until']) - span)
        base_earliest, base_latest = earliest, latest
        earliest, latest, _ = start_interval(added, earliest, latest)
    condition = {'type': 'joint_time_window', 'participants': deepcopy(people)}
    if duration is not None:
        condition['duration_min'] = duration
    if legacy and fact.get('open_until'):
        condition['open_until'] = fact['open_until']
    add(condition, {'earliest_start_min': earliest, 'latest_start_min': latest,
                    'travel_evidence': travel_evidence}, earliest <= latest if earliest is not None else None)
    for c in added:
        if c['object'] == 'activity':
            one_earliest, one_latest, _ = start_interval([c], base_earliest, base_latest) if base_earliest is not None else (None, None, [])
            add(c, {'earliest_start_min': one_earliest, 'latest_start_min': one_latest},
                one_earliest <= one_latest if one_earliest is not None else None)
    return checks
