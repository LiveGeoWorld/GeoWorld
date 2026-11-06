"""Verify submitted alternatives using frozen public constraints and route evidence.

No candidate search, model calls, GT edits, or answer completion. A saved timeline
can certify an alternative; failure to reuse that timeline is not proof that no
other schedule exists and is therefore reported as unverifiable.
"""
from __future__ import annotations
from copy import deepcopy
import math
from pathlib import Path
from test_runner.verification.itinerary.sampler import ItinerarySampler, WEEKDAY_NAMES
from world_module.map._internal.factual_service_index import materialize_factual_services
from test_runner.eval.itinerary_identity import load_identity_places, _decode

def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)

def _norm(value):
    return ' '.join(str(value).casefold().split())

def _clock(value):
    if not isinstance(value, str):
        return None
    try:
        parts = value.split(':')
        if len(parts) not in (2, 3):
            return None
        (h, m) = (int(parts[0]), int(parts[1]))
        s = int(parts[2]) if len(parts) == 3 else 0
        return h * 3600 + m * 60 + s if 0 <= h <= 24 and 0 <= m < 60 and (0 <= s < 60) and (h < 24 or m == s == 0) else None
    except ValueError:
        return None

def frozen_facts(task_dir):
    (_, binding) = load_identity_places(task_dir)
    world = materialize_factual_services(_decode(Path(binding['path']).read_bytes()))
    sampler = ItinerarySampler(world, {}, None)
    facts = sampler.activity_candidates(routing_only=False)
    places = {p['place_id']: p for p in world['places'] if p.get('city_id') == binding['city_id']}
    return ([f for f in facts if f['place_id'] in places], places)

def _check_fact(fact, constraint, weekday):
    """Return (True/False/None, observed); missing facts never prove a violation."""
    kind = constraint.get('type')
    value = constraint.get('value')
    expected_ops = {'primary_type': 'equals', 'district_name': 'equals', 'price_maximum': 'at_most', 'price_target_amount': 'supports_spend', 'service_feature': 'includes', 'opens_by': 'at_most', 'closes_at_or_after': 'at_least'}
    if kind not in expected_ops or constraint.get('operator') != expected_ops[kind]:
        return (None, 'unsupported_constraint')
    if kind in ('primary_type', 'district_name'):
        actual = fact.get(kind)
        return (actual == value if actual else None, actual)
    if kind in ('price_maximum', 'price_target_amount'):
        price = fact.get('price_range') or {}
        (low, high) = (price.get('minimum'), price.get('maximum'))
        if not _finite(value) or not _finite(high) or (not price.get('currency')) or (price['currency'] != constraint.get('currency')):
            return (None, price)
        if kind == 'price_maximum':
            return (high <= value, price)
        return (low <= value <= high if _finite(low) else None, price)
    if kind == 'service_feature':
        raw = fact.get('_service_details', {})
        actual = raw.get(value)
        if type(actual) is bool:
            return (actual, actual)
        return (True if value in fact.get('service_features', []) else None, actual)
    intervals = (fact.get('weekly_opening_intervals') or {}).get(str(weekday))
    if not intervals or not _finite(value):
        return (None, intervals)
    return (min((x[0] for x in intervals)) <= value if kind == 'opens_by' else max((x[1] for x in intervals)) >= value, intervals)

def _route_bounds(result, gt, places, origin, destination, mode):
    """Use task-frozen route facts and saved observations, never a mutable cache."""
    if origin == destination:
        return (0.0, 0.0)
    mode = str(mode).upper()
    values = []
    unavailable = False
    minutes = (gt.get('route_data', {}).get(origin + '__TO__' + destination) or {}).get(mode)
    coarse = (max(0, float(minutes) - 1) * 60, (float(minutes) + 1) * 60) if _finite(minutes) and minutes > 0 else None

    def resolve(point):
        if not isinstance(point, dict) or not isinstance(point.get('location'), dict):
            return None
        loc = point['location']
        (lat, lng) = (loc.get('lat'), loc.get('lng'))
        if not _finite(lat) or not _finite(lng):
            return None
        matches = {pid for (pid, p) in places.items() if _norm(p.get('name')) == _norm(point.get('name')) and abs(p['location']['lat'] - lat) <= 1e-05 and (abs(p['location']['lng'] - lng) <= 1e-05)}
        return next(iter(matches)) if len(matches) == 1 else None
    for call in result.trace:
        if call.tool.split('__')[-1] != 'query_routes':
            continue
        args = call.args
        if resolve(args.get('origin')) != origin or resolve(args.get('destination')) != destination:
            continue
        for route in (call.result or {}).get('routes', []):
            duration = route.get('duration_s')
            if str(route.get('mode')).upper() == mode and route.get('available') is False:
                unavailable = True
            if str(route.get('mode')).upper() == mode and route.get('available') is not False and _finite(duration) and (duration > 0):
                values.append(float(duration))
    if unavailable:
        return None
    if values:
        (low, high) = (min(values), max(values))
        if low != high or (coarse and (not coarse[0] <= low <= coarse[1])):
            return None
        return (low, high)
    return coarse

def _anchor(requirement, relation, previous, gt, places):
    group = requirement.get('activity_group') or {}
    anchor = group.get('start_anchor' if relation == 'start_anchor_to_gt' else 'end_anchor') or {}
    if anchor.get('kind') == 'previous_activity':
        return previous
    name = anchor.get('place_name')
    if not name and anchor.get('kind') == 'hotel':
        name = (gt.get('hotel') or {}).get('name')
    matches = {pid for (pid, p) in places.items() if name and _norm(p.get('name')) == _norm(name)}
    return next(iter(matches)) if len(matches) == 1 else None

def _timeline_certificate(result, gt, actual, facts_by_slot, places):
    """Check a sufficient full-timeline witness, without imposing it as mandatory.

    Reuse saved timings only as a possible execution certificate. Missing or
    non-fitting evidence returns unknown, not a claim of global infeasibility.
    """
    from test_runner.eval.itinerary_choice import _is_poi_choice
    certificates = []
    supported = {'continuous_timeline_required', 'travel_is_explicit_timeline_block', 'departure_ends_task', 'daily_scenarios', 'lodging_is_stateful'}
    if set(gt.get('constraints', {})) - supported:
        return (None, 'additional_global_constraints_unverifiable')
    for (index, day) in enumerate(gt['correct_itinerary']['days']):
        events = deepcopy(day['events'])
        choices = [e for e in events if _is_poi_choice(e)]
        if len(choices) != len(actual[index]):
            return (None, 'timeline_activity_alignment_unverifiable')
        replacements = {}
        for (event, pid) in zip(choices, actual[index]):
            old = event['place_id']
            if old in replacements and replacements[old] != pid:
                return (None, 'repeated_place_substitution_unverifiable')
            replacements[old] = pid
        for e in events:
            if e.get('block_kind') == 'fixed_event' and replacements.get(e.get('place_id'), e.get('place_id')) != e.get('place_id'):
                return (None, 'fixed_place_substitution_unverifiable')
            if e.get('block_kind') == 'personal_activity' and replacements.get(e.get('place_id'), e.get('place_id')) != e.get('place_id'):
                return (None, 'personal_activity_location_unverifiable')
        previous_end = None
        last_place = (gt.get('hotel') or {}).get('place_id') if index else None
        order = 0
        proof = []
        departed = False
        for event in events:
            if event.get('event_type') == 'overnight_personal_activity':
                continue
            if departed and gt.get('constraints', {}).get('departure_ends_task'):
                return (None, 'activity_after_departure_unverifiable')
            departed = event.get('event_type') == 'departure'
            (start, end) = (_clock(event.get('start')), _clock(event.get('end')))
            if start is None or end is None or end < start or (previous_end is not None and start != previous_end):
                return (None, 'timeline_time_alignment_unverifiable')
            previous_end = end
            if event.get('block_kind') == 'transit':
                origin = replacements.get(event.get('from_place_id'), event.get('from_place_id'))
                dest = replacements.get(event.get('to_place_id'), event.get('to_place_id'))
                if not origin or not dest or (last_place is not None and origin != last_place):
                    return (None, 'timeline_route_continuity_unverifiable')
                unchanged = origin == event.get('from_place_id') and dest == event.get('to_place_id')
                bounds = (end - start, end - start) if unchanged else _route_bounds(result, gt, places, origin, dest, event.get('mode'))
                if bounds is None or bounds[1] > end - start:
                    return (None, 'timeline_route_evidence_missing_or_not_fitting')
                last_place = dest
                proof.append({'origin': origin, 'destination': dest, 'mode': event.get('mode'), 'duration_bounds_s': bounds, 'window_s': end - start})
            elif event.get('place_id'):
                pid = replacements.get(event['place_id'], event['place_id'])
                if last_place is not None and pid != last_place:
                    return (None, 'timeline_location_jump_unverifiable')
                last_place = pid
                if _is_poi_choice(event):
                    (fact, weekday) = facts_by_slot[index, order]
                    order += 1
                    intervals = fact.get('weekly_opening_intervals') or {}
                    spans = [(a * 60, b * 60) for (a, b) in intervals.get(str(weekday), [])]
                    spans += [(a * 60 - 86400, b * 60 - 86400) for (a, b) in intervals.get(str((weekday - 1) % 7), []) if b > 1440]
                    if not any((a <= start <= end <= b for (a, b) in spans)):
                        return (None, 'timeline_opening_evidence_missing_or_not_fitting')
        if index + 1 < len(actual):
            hotel = (gt.get('hotel') or {}).get('place_id')
            if not hotel or last_place != hotel:
                return (None, 'overnight_lodging_continuity_unverifiable')
        certificates.append({'day': index + 1, 'routes': proof})
    return (certificates, '')

def verify_alternative(result, gt, actual, *, task_dir=None, facts=None, places=None):
    """Return a tri-state assessment and per-constraint evidence."""
    issues = []
    checks = []
    facts_by_slot = {}

    def issue(status, code, **kwargs):
        issues.append({'status': status, 'code': code, **kwargs})
    requirements = gt.get('public_requirements')
    if not isinstance(requirements, list) or not requirements:
        return {'status': 'unverifiable', 'issues': [{'status': 'unverifiable', 'code': 'public_requirements_missing'}], 'checks': []}
    if facts is None or places is None:
        if task_dir is None:
            return {'status': 'unverifiable', 'issues': [{'status': 'unverifiable', 'code': 'frozen_world_binding_missing'}], 'checks': []}
        (facts, places) = frozen_facts(task_dir)
    grouped = {day: [] for day in range(1, len(actual) + 1)}
    for req in requirements:
        if req.get('day') not in grouped:
            issue('unverifiable', 'public_requirement_day_alignment')
        else:
            grouped[req['day']].append(req)
    for (day, ids) in enumerate(actual, 1):
        reqs = grouped[day]
        if len(reqs) != len(ids):
            issue('unverifiable', 'public_requirement_activity_alignment', day=day)
            continue
        previous = None
        for (order, (pid, req)) in enumerate(zip(ids, reqs), 1):
            slot = {'day': day, 'activity_order': order, 'place_id': pid}
            weekday = req.get('weekday')
            weekday = WEEKDAY_NAMES.index(weekday) if weekday in WEEKDAY_NAMES else None
            candidates = [f for f in facts if f['place_id'] == pid and f.get('service_type') == req.get('activity_direction')]
            if not candidates:
                known = [f for f in facts if f['place_id'] == pid and f.get('service_types')]
                issue('violated' if known else 'unverifiable', 'service_capabilities' if known else 'service_capability_evidence_missing', **slot)
                previous = pid
                continue
            fact = deepcopy(candidates[0])
            raw = (places[pid].get('map_projection') or {}).get('google_place_details') or {}
            fact['_service_details'] = raw
            facts_by_slot[day - 1, order - 1] = (fact, weekday)
            if weekday is None:
                issue('unverifiable', 'weekday_missing', **slot)
            required = req.get('service_types')
            if required:
                ok = set(required).issubset(set(fact.get('service_types', [])))
            elif req.get('primary_service_type'):
                ok = fact.get('primary_service_type') == req['primary_service_type']
            else:
                ok = None
            checks.append(dict(slot, constraint='service_capabilities', matches=ok))
            if ok is not True:
                issue('violated' if ok is False else 'unverifiable', 'service_capabilities', **slot)
            if req.get('level') != 'required_category':
                issue('unverifiable', 'unsupported_requirement_level', **slot)
            for constraint in req.get('constraints', []):
                if constraint.get('type') == 'route_duration_range':
                    relation = constraint.get('relation')
                    anchor = _anchor(req, relation, previous, gt, places)
                    if relation not in ('start_anchor_to_gt', 'gt_to_end_anchor') or constraint.get('operator') != 'between':
                        (ok, observed) = (None, 'unsupported_route_constraint')
                    else:
                        (origin, dest) = (anchor, pid) if relation == 'start_anchor_to_gt' else (pid, anchor)
                        observed = _route_bounds(result, gt, places, origin, dest, constraint.get('mode')) if anchor else None
                        (low, high) = (constraint.get('minimum'), constraint.get('maximum'))
                        ok = None
                        if observed is not None and _finite(low) and _finite(high):
                            if low * 60 <= observed[0] <= observed[1] <= high * 60:
                                ok = True
                            elif observed[1] < low * 60 or observed[0] > high * 60:
                                ok = False
                else:
                    (ok, observed) = _check_fact(fact, constraint, weekday)
                checks.append(dict(slot, constraint=constraint, observed=observed, matches=ok))
                if ok is not True:
                    issue('violated' if ok is False else 'unverifiable', str(constraint.get('type')), expected=constraint, observed=observed, **slot)
            previous = pid
    if any((i['status'] == 'violated' for i in issues)):
        return {'status': 'violated', 'issues': issues, 'checks': checks}
    if issues:
        return {'status': 'unverifiable', 'issues': issues, 'checks': checks}
    (certificate, reason) = _timeline_certificate(result, gt, actual, facts_by_slot, places)
    if certificate is None:
        from test_runner.eval.itinerary_reschedule import rescheduled_certificate
        (certificate, reschedule_reason) = rescheduled_certificate(result, gt, actual, facts_by_slot, places)
        if certificate is None:
            checks.append({'constraint': 'fixed_selection_reschedule', 'matches': None, 'reason': reschedule_reason})
    if certificate is None:
        issue('unverifiable', reason)
    return {'status': 'verified' if certificate is not None else 'unverifiable', 'issues': issues, 'checks': checks, 'timeline_certificate': certificate}
