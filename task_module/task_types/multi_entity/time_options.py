"""Verified, task-local schedule options for the existing construction model."""
from copy import deepcopy
from math import ceil, floor

from .choice_verification import verify_candidate_choice
from .constraint_protocol import MODES, start_interval
from .solver import _clock_minutes, _format_clock


def schedule_options(original, venues, routes, target, baseline):
    if baseline['valid'] or target not in baseline['accepted_place_ids']:
        return []
    relevant = set(baseline['accepted_place_ids'])
    _, latest, _ = start_interval(
        original.get('additional_constraints', []), 0,
        min(_clock_minutes(p['must_leave_by']) for p in original['participants'])
        - original['activity']['duration_min'],
    )
    target_venue = next(v for v in venues if v['place_id'] == target)
    if target_venue.get('activity_opening_intervals'):
        latest = min(latest, max(closed for _, closed in target_venue['activity_opening_intervals'])
                     - original['activity']['duration_min'])
    options = []
    for index, person in enumerate(original['participants']):
        name = person['name']
        for mode in MODES:
            times = {
                v['place_id']: routes.get(f"{person['departure_area']}→{v['name']}", {}).get(mode)
                for v in venues if v['place_id'] in relevant
            }
            gt = times[target]
            if gt is None or any(t is None for t in times.values()) or gt in [
                t for place, t in times.items() if place != target
            ]:
                continue
            nearer = max((t for t in times.values() if t < gt), default=None)
            farther = min((t for t in times.values() if t > gt), default=None)
            lower = floor(gt * 2) / 2 if nearer is not None else None
            upper = ceil(gt * 2) / 2 if farther is not None else None
            option = deepcopy(original)
            additions = option.setdefault('additional_constraints', [])
            additions.append({'object': name, 'attribute': 'travel_mode', 'relation': 'in', 'value': [mode]})
            if (lower is None or lower > nearer) and (upper is None or upper < farther):
                if lower is not None:
                    additions.append({'object': name, 'attribute': 'commute_min', 'relation': 'ge', 'value': lower})
                if upper is not None:
                    additions.append({'object': name, 'attribute': 'commute_min', 'relation': 'le', 'value': upper})
                    departure = floor(latest - gt)
                    converted = deepcopy(option)
                    converted['participants'][index]['available_from'] = _format_clock(departure)
                    converted['additional_constraints'].pop()
                    if (departure > _clock_minutes(person['available_from']) and
                            verify_candidate_choice(converted, venues, routes, target)['valid']):
                        options.append(converted)
                if verify_candidate_choice(option, venues, routes, target)['valid']:
                    options.append(option)
                continue
            # A narrow gap with no half-minute boundary can use an explicit preference.
            for anchor in (None, *sorted({floor(gt * 2) / 2, ceil(gt * 2) / 2},
                                         key=lambda value: (abs(value - gt), value))):
                ranked = {**option, 'commute_preference': {'participant': name, 'mode': mode}}
                if anchor is not None:
                    ranked['commute_preference']['target_minutes'] = anchor
                if verify_candidate_choice(ranked, venues, routes, target)['valid']:
                    options.append(ranked)
                    break
    return options
