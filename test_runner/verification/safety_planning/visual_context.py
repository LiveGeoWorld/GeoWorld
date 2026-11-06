from __future__ import annotations
import copy
import math
from datetime import datetime
from test_runner.verification.safety_planning.spacetime import TimeWindow
from test_runner.verification.safety_planning.protection import ProtectionConflict

def _area_bounds(anchor):
    """Only explicit polygon coordinates bound an area; labels/points never do."""
    geometry = anchor.get('geometry') or {}
    kind = str(geometry.get('type', geometry.get('kind', ''))).lower()
    if kind not in {'polygon', 'multipolygon'}:
        return None
    data = geometry.get('data', geometry)
    coordinates = data.get('coordinates') if isinstance(data, dict) else None
    points = []

    def collect(value, depth):
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError('Incomplete polygon')
        if depth:
            for child in value:
                collect(child, depth - 1)
        else:
            if len(value) < 2 or any((type(v) not in (int, float) or not math.isfinite(v) for v in value[:2])):
                raise ValueError('Invalid coordinate')
            (lng, lat) = value[:2]
            if not (-180 <= lng <= 180 and -90 <= lat <= 90):
                raise ValueError('Coordinate out of bounds')
            points.append((lng, lat))
    try:
        collect(coordinates, 2 if kind == 'polygon' else 3)
    except (ValueError, TypeError):
        return None
    bounds = (min((p[0] for p in points)), min((p[1] for p in points)), max((p[0] for p in points)), max((p[1] for p in points)))
    return bounds if bounds[2] - bounds[0] < 180 else None

def _spatially_separate(current, prior):
    if not current or not prior:
        return False
    (left, right) = ([_area_bounds(a) for a in current], [_area_bounds(a) for a in prior])
    if any((b is None for b in left + right)):
        return False
    return all((a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or (b[3] < a[1]) for a in left for b in right))

def evidence_refs(context: dict) -> set[str]:
    refs = {ref for anchor in context.get('geographic_anchors', []) for ref in anchor.get('source_refs', [])}
    for route in context.get('route_evidence', []):
        refs.add(route['source_ref'])
        refs.update((step['source_ref'] for step in route.get('steps', [])))
    refs.update((place['source_ref'] for place in context.get('places', [])))
    if any((not isinstance(ref, str) or not ref for ref in refs)):
        raise ValueError('Geographic evidence references must be nonempty strings')
    return refs

def required_avoidance(context: dict, active: dict) -> set[tuple[str, str]]:
    required = set()
    for row in context['prior_spacetime_records']:
        window = TimeWindow(**row['active_window'])
        if window.overlaps(TimeWindow(**active)):
            required.add((row['record_ref'], 'new_event_to_prior_record'))
        if row['role'] == 'event' and window.overlaps(TimeWindow(**context['activity_window'])):
            required.add((row['record_ref'], 'prior_event_to_current_dependencies'))
    return required

def prepare_visual_context(event_input: dict, *, prior_records: list[dict], event_generation_window: dict[str, str]) -> dict:
    """Select temporally relevant records, then apply conservative spatial filtering.

    Drop records only when complete supplied area bounds are separated. Unknown
    geometry and route dependencies (which may leave those areas) are retained.
    """
    activity = TimeWindow(**event_input['activity_window'])
    permitted = TimeWindow(**event_generation_window)
    if not permitted.overlaps(activity):
        raise ValueError('Permitted event interval must overlap the activity')
    selected = []
    seen = set()
    for row in prior_records:
        ref = row['record_ref']
        if ref in seen or row['role'] not in {'event', 'decision_dependency', 'protected_state'}:
            raise ValueError('Prior records require distinct references and explicit roles')
        seen.add(ref)
        window = TimeWindow(**row['active_window'])
        if window.overlaps(permitted) or (row['role'] == 'event' and window.overlaps(activity)):
            if event_input.get('protection_contract') == 'safety_state_protection.v1' and (not event_input.get('screening_has_routes', bool(event_input.get('route_evidence')))) and (not row.get('routes')) and _spatially_separate(event_input.get('screening_anchors', event_input.get('geographic_anchors', [])), row.get('scopes', [])):
                continue
            selected.append({key: copy.deepcopy(row[key]) for key in ('record_ref', 'role', 'active_window', 'source_refs', 'description', 'routes', 'places', 'scopes', 'candidate_refs', 'candidate_profiles', 'public_place_constraints', 'effects', 'protection_contract', 'candidate_ref', 'scope_refs', 'state', 'decision_requirement', 'reason') if key in row})
    result = copy.deepcopy(event_input)
    result['event_generation_window'] = copy.deepcopy(event_generation_window)
    result['prior_spacetime_records'] = selected
    return result

def validate_avoidance(event: dict, context: dict) -> None:
    """Check reported source-bound avoidance, not a second geographic solver."""
    if 'prior_spacetime_records' not in context:
        raise ValueError('Safety avoidance requires an explicit prior-record snapshot, even when empty')
    parse = lambda value: datetime.fromisoformat(value.replace('Z', '+00:00'))
    allowed = context['event_generation_window']
    active = event['active_window']
    TimeWindow(**allowed)
    TimeWindow(**active)
    if not parse(allowed['start']) <= parse(active['start']) < parse(active['end']) <= parse(allowed['end']):
        raise ValueError('Generated event exceeds the time range used for avoidance screening')
    records = {row['record_ref']: row for row in context['prior_spacetime_records']}
    required = required_avoidance(context, active)
    rows = event.get('avoidance_assessments', [])
    if not isinstance(rows, list):
        raise ValueError('Avoidance assessments must be a list')
    current_refs = evidence_refs(context)
    seen = set()
    conflicts = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Avoidance assessment must be an object')
        ref = row.get('record_ref')
        direction = row.get('direction')
        if not isinstance(ref, str) or not isinstance(direction, str):
            raise ValueError('Avoidance needs a record reference and an explicit direction')
        key = (ref, direction)
        if key not in required or key in seen:
            raise ValueError('Avoidance references an unknown, irrelevant or duplicate record/direction')
        seen.add(key)
        prior_refs = set(records[ref].get('source_refs', []))
        citations = row.get('source_refs')
        if not isinstance(citations, list) or not citations or any((not isinstance(c, str) or c not in current_refs | prior_refs for c in citations)) or (not set(citations) & current_refs) or (not set(citations) & prior_refs):
            raise ValueError('Avoidance must cite both current and prior structured evidence')
        compatible = records[ref].get('protection_contract') == 'safety_state_protection.v1' and row.get('relation') == 'compatible' and (row.get('preserves_state') is True) and (row.get('preserves_decision') is True)
        if row.get('relation') != 'disjoint' and (not compatible):
            if row.get('relation') == 'intersects' or (row.get('relation') == 'compatible' and (row.get('preserves_state') is False or row.get('preserves_decision') is False)):
                conflicts.append(ref)
            else:
                raise ValueError('Unknown prior exposure is not evidence of cluster saturation')
        if not isinstance(row.get('reason'), str) or not row['reason'].strip():
            raise ValueError('Avoidance needs a geographic explanation')
    if required != seen:
        raise ValueError('Every temporally relevant prior record and direction requires avoidance evidence')
    if conflicts:
        raise ProtectionConflict(conflicts)
