import copy
from datetime import datetime
from test_runner.verification.safety_planning.event_bundle import circle_anchor, same_circle_anchor
from test_runner.verification.safety_planning.task_time import applies

def required_routes(context):
    from test_runner.verification.safety_planning.occupancy import scope_geometry
    target = context.get('construction_context', {}).get('fixed_target_ref')
    modes = {c['transportation_mode'] for c in context['public_place_constraints'] if c.get('transportation_mode')}
    routes = [r for r in context.get('route_evidence', []) if r['candidate_ref'] == target and r['mode'] in modes]
    if {r['mode'] for r in routes} != modes or len(routes) != len(modes):
        raise ValueError('GT protection requires one frozen route per C transportation mode')
    anchors = {a['scope_ref']: a for a in context['geographic_anchors']}
    for route in routes:
        if route['source_ref'] not in anchors:
            raise ValueError('GT route protection lacks its frozen scope')
        scope_geometry(anchors[route['source_ref']], context)
    return routes

def compile_protection(bundle, context):
    result = copy.deepcopy(context)
    if any(('protected_states' in event for event in bundle['events'])):
        raise ValueError('New events must not contain legacy protected_states')
    protection = bundle.get('gt_protection')
    if not isinstance(protection, dict) or set(protection) != {'place_scope', 'route_refs', 'active_window'}:
        raise ValueError('GT protection requires place_scope, route_refs and active_window')
    target = context.get('construction_context', {}).get('fixed_target_ref')
    places = [p for p in context['place_evidence'] if p['candidate_ref'] == target]
    if len(places) != 1:
        raise ValueError("GT protection requires the fixed target's place evidence")
    definition = protection['place_scope']
    if not isinstance(definition, dict) or definition.get('center_ref') != places[0]['source_refs'][0]:
        raise ValueError('GT protection circle must be centred on the fixed GT, not a distractor')
    window = protection['active_window']
    if not isinstance(window, dict) or set(window) != {'start', 'end'} or (not applies(window, context['task_time'])):
        raise ValueError('GT protection interval must contain task_time')
    parse = lambda value: datetime.fromisoformat(value.replace('Z', '+00:00'))
    permitted = context['event_generation_window']
    if not parse(permitted['start']) <= parse(window['start']) < parse(window['end']) <= parse(permitted['end']):
        raise ValueError('GT protection interval must stay inside the permitted window')
    routes = required_routes(context)
    refs = protection['route_refs']
    if not isinstance(refs, list) or any((not isinstance(ref, str) for ref in refs)) or len(refs) != len(set(refs)) or (set(refs) != {r['source_ref'] for r in routes}):
        raise ValueError('GT route protection must match C-required GT routes only')
    anchors = {a['scope_ref']: a for a in result['geographic_anchors']}
    ref = 'gt_protection_scope'
    anchor = circle_anchor(definition, ref, context, anchors)
    if ref in anchors and (not same_circle_anchor(anchors[ref], anchor)):
        raise ValueError('Stored GT protection geometry differs from its author parameters')
    if ref not in anchors:
        result['geographic_anchors'].append(anchor)
    rows = [{'candidate_ref': target, 'scope_refs': [ref], 'source_refs': [definition['center_ref']], 'active_window': copy.deepcopy(window), 'dependency_kind': 'place'}]
    rows.extend(({'candidate_ref': target, 'scope_refs': [r['source_ref']], 'source_refs': [r['source_ref']], 'active_window': copy.deepcopy(window), 'dependency_kind': 'route'} for r in routes))
    if 'compiled_gt_protection' in context and context['compiled_gt_protection'] != rows:
        raise ValueError('Stored GT protection dependencies differ from the author output and C')
    result['compiled_gt_protection'] = rows
    return result
