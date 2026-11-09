"""Bind a bounded completion repair to its existing private timeline nodes."""
from copy import deepcopy

from task_module.task_types.itinerary.clock_precision import clock_seconds
from task_module.task_types.itinerary.time_repair import TimeRepairError, editable_fields, repair_time


def repair_incoming_time(node, timeline, constraint, evidence, client, audit, opening_intervals):
    saved = node.get('local_completion')
    if constraint['relation'] != 'start_anchor_to_gt' or not saved:
        raise TimeRepairError('selected route has no matching local completion result; scope adaptation requires agreement')
    original = deepcopy(saved['output'])
    linked = [next(item for item in timeline if item['node_id'] == ref) for ref in saved['node_ids']]
    transits = [item for item in linked if item['block_kind'] == 'transit']
    if len(transits) != 1:
        raise TimeRepairError('local completion must identify exactly one existing transit')
    transit = transits[0]
    transit_index = linked.index(transit)
    before, after = linked[:transit_index], linked[transit_index+1:]
    begin = clock_seconds(linked[0]['start'])
    payload = {
        'activity': node.get('event_type'),
        'constraint': {'type': constraint['type'], 'relation': constraint['relation'], 'mode': constraint['mode']},
        'target_seconds': evidence['target_seconds'],
        'competitor_seconds': evidence['competitor_seconds'],
        'original_transition': original, 'editable_time_fields': editable_fields(original),
    }

    def build_updates(transition, bounds):
        low, high = bounds
        updates = []
        departure = clock_seconds(transition['departure_time'])
        activity = clock_seconds(transition['activity_start'])
        if not activity < clock_seconds(node['end']):
            raise ValueError('activity_start must precede the unchanged activity end')
        if opening_intervals and not any(a*60 <= activity < b*60 for a, b in opening_intervals):
            raise ValueError('activity_start is outside the unchanged POI opening intervals')
        end_of_travel = (clock_seconds(transition['after_arrival'][0]['start'])
                         if transition['after_arrival'] else activity)
        duration = end_of_travel - departure
        if duration < evidence['target_seconds'] or not low <= duration <= high:
            raise ValueError(f'local transit reserves {duration} seconds; needs actual {evidence["target_seconds"]} and boundary {bounds}')
        for nodes, key in [(before, 'before_departure'), (after, 'after_arrival')]:
            if len(nodes) != len(transition[key]):
                raise ValueError(f'{key} cannot add or remove nodes')
            for item, block in zip(nodes, transition[key]):
                updates.append((item, block['start'], block['end']))
        travel_end_text = transition['after_arrival'][0]['start'] if after else transition['activity_start']
        updates.append((transit, transition['departure_time'], travel_end_text))
        times = {item['node_id']: (a, b) for item, a, b in updates}
        cursor = begin
        for item in linked:
            a, b = times[item['node_id']]
            if clock_seconds(a) != cursor or clock_seconds(b) <= cursor:
                raise ValueError(f'node {item["node_id"]} breaks local continuity at {cursor} seconds')
            cursor = clock_seconds(b)
        if cursor != activity:
            raise ValueError('local transition does not meet activity_start')
        return updates

    result, bounds = repair_time(client, payload, build_updates, audit=audit)
    updates = build_updates(result['transition'], bounds)
    for item, start, end in updates:
        item['start'], item['end'] = start, end
    node['start'] = result['transition']['activity_start']
    saved['output'] = deepcopy(result['transition'])
    saved.setdefault('time_repairs', []).append(deepcopy(result))
    constraint['minimum'], constraint['maximum'] = bounds[0] / 60, bounds[1] / 60
    constraint['boundary_text'] = result['boundary']['text']


def sync_repaired_menus(dag, menus, requirements=(), contracts=()):
    from task_module.task_types.itinerary.dag_builder import _occupied_boundaries
    from task_module.task_types.itinerary.clock_precision import clock_minutes
    nodes = {node['node_id']: node for day in dag.get('days', []) for node in day.get('timeline', [])}
    for node in nodes.values():
        if 'occupied_time_boundaries' in node:
            node['occupied_time_boundaries'] = _occupied_boundaries(clock_minutes(node['start']), clock_minutes(node['end']))
    for menu in menus:
        for key in ('required_slots', 'optional_slots'):
            for slot in menu.get(key, []):
                node = nodes.get(slot.get('slot_id'))
                if node:
                    slot['time_range'] = (node['start'], node['end'])
                    slot['occupied_time_boundaries'] = node.get('occupied_time_boundaries', [])
                    for candidate in slot.get('candidates', []):
                        candidate['duration_typical_min'] = clock_minutes(node['end']) - clock_minutes(node['start'])
    from task_module.task_types.itinerary.requirement_builder import _activity_group_context
    by_requirement = {item['requirement_id']: item for item in requirements}
    private = {item['requirement_id']: item for item in contracts}
    for day in dag.get('days', []):
        timeline = day.get('timeline', [])
        for index, node in enumerate(timeline):
            ref = node.get('requirement_id')
            if ref in by_requirement:
                group = _activity_group_context(timeline, index, lodging=dag.get('lodging') or {})
                for record in (by_requirement[ref], private.get(ref)):
                    if record is not None:
                        record['activity_group'] = group
                        record['route_context'] = group['route_context']
