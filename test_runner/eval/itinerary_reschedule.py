"""Find a sufficient ordered schedule without choosing places for the agent.

Keep saved activity durations and fixed anchors. Earliest feasible placement
with waiting can prove a witness; failure is unknown, not global infeasibility.
"""
from copy import deepcopy

def rescheduled_certificate(result, gt, actual, facts_by_slot, places):
    from test_runner.eval.itinerary_alternatives import _clock, _route_bounds
    from test_runner.eval.itinerary_choice import _is_poi_choice
    supported = {'continuous_timeline_required', 'travel_is_explicit_timeline_block', 'departure_ends_task', 'daily_scenarios', 'lodging_is_stateful'}
    if set(gt.get('constraints', {})) - supported:
        return (None, 'additional_global_constraints_unverifiable')
    proof = []
    for (day_index, day) in enumerate(gt['correct_itinerary']['days']):
        events = [deepcopy(e) for e in day['events'] if e.get('event_type') != 'overnight_personal_activity']
        if not events:
            return (None, 'timeline_missing')
        slot = 0
        for event in events:
            event['_old_place'] = event.get('place_id')
            if _is_poi_choice(event):
                if slot >= len(actual[day_index]):
                    return (None, 'timeline_activity_alignment_unverifiable')
                event['place_id'] = actual[day_index][slot]
                event['_slot'] = slot
                slot += 1
        if slot != len(actual[day_index]):
            return (None, 'timeline_activity_alignment_unverifiable')
        current = _clock(events[0].get('start'))
        deadline = _clock(events[-1].get('end'))
        if current is None or deadline is None:
            return (None, 'timeline_time_alignment_unverifiable')
        last_place = (gt.get('hotel') or {}).get('place_id') if day_index else None
        schedule = []
        departed = False
        for (index, event) in enumerate(events):
            (begin, end) = (_clock(event.get('start')), _clock(event.get('end')))
            if begin is None or end is None or end < begin:
                return (None, 'timeline_time_alignment_unverifiable')
            if departed and gt.get('constraints', {}).get('departure_ends_task'):
                return (None, 'activity_after_departure_unverifiable')
            duration = end - begin
            kind = event.get('block_kind')
            record = {'event_index': index, 'block_kind': kind}
            if kind == 'transit':
                following = next((e for e in events[index + 1:] if e.get('place_id')), None)
                origin = last_place or event.get('from_place_id')
                destination = following['place_id'] if following else event.get('to_place_id')
                if not origin or not destination:
                    return (None, 'timeline_route_continuity_unverifiable')
                unchanged = origin == event.get('from_place_id') and destination == event.get('to_place_id')
                bounds = (duration, duration) if unchanged else _route_bounds(result, gt, places, origin, destination, event.get('mode'))
                if bounds is None:
                    return (None, 'timeline_route_evidence_missing')
                duration = bounds[1]
                start = current
                last_place = destination
                record.update(origin=origin, destination=destination, mode=event.get('mode'), duration_bounds_s=bounds)
            elif event.get('place_id'):
                pid = event['place_id']
                if last_place is not None and pid != last_place:
                    return (None, 'timeline_location_jump_unverifiable')
                last_place = pid
                record['place_id'] = pid
                if kind == 'fixed_event' or event.get('is_fixed') is True:
                    if current > begin:
                        return (None, 'rescheduled_fixed_anchor_not_fitting')
                    start = begin
                elif '_slot' in event:
                    (fact, weekday) = facts_by_slot[day_index, event['_slot']]
                    weekly = fact.get('weekly_opening_intervals') or {}
                    spans = [(a * 60, b * 60) for (a, b) in weekly.get(str(weekday), [])]
                    spans += [(a * 60 - 86400, b * 60 - 86400) for (a, b) in weekly.get(str((weekday - 1) % 7), []) if b > 1440]
                    candidates = [max(current, a) for (a, b) in spans if max(current, a) + duration <= b]
                    if not candidates:
                        return (None, 'rescheduled_opening_not_fitting')
                    start = min(candidates)
                elif kind == 'personal_activity':
                    start = current
                else:
                    return (None, 'rescheduled_unknown_block_kind')
            else:
                return (None, 'rescheduled_unknown_block_kind')
            if start > current:
                schedule.append({'block_kind': 'waiting', 'start_s': current, 'end_s': start, 'place_id': last_place})
            current = start + duration
            if current > deadline:
                return (None, 'rescheduled_day_boundary_not_fitting')
            record.update(start_s=start, end_s=current)
            schedule.append(record)
            departed = event.get('event_type') == 'departure'
        if day_index + 1 < len(actual) and last_place != (gt.get('hotel') or {}).get('place_id'):
            return (None, 'overnight_lodging_continuity_unverifiable')
        proof.append({'day': day_index + 1, 'method': 'fixed_selection_rescheduled', 'events': schedule})
    return (proof, '')
