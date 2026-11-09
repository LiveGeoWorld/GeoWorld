"""Restore author-only final-C evidence from the exact bound snapshot, read-only."""
from task_module.task_types.itinerary.sampler import ItinerarySampler
from task_module.task_types.itinerary.requirement_builder import compare_selected_constraints


class FrozenRoutes:
    def __init__(self, payload):
        raw = payload.get('routes') or payload.get('cache') or {}
        self.raw = raw
        rows = raw.values() if isinstance(raw, dict) else raw
        self.index = {}
        for row in rows:
            if row.get('waypoint_place_id') or row.get('waypoints'):
                continue
            key = (row.get('origin_place_id'), row.get('dest_place_id'), row.get('mode'))
            self.index.setdefault(key, []).append(row)

    def get(self, origin, destination, mode):
        rows = self.index.get((origin['place_id'], destination['place_id'], mode), [])
        if len(rows) != 1:
            return None
        row = rows[0]
        if not row.get('route') and row.get('route_ref') and isinstance(self.raw, dict):
            row = {**row, 'route': (self.raw.get(row['route_ref']) or {}).get('route')}
        return row


def restore_comparisons(sandbox, world, routes):
    # Generation uses this factual POI capability projection, not legacy services.
    from map_module._internal.factual_service_index import materialize_factual_services
    world = materialize_factual_services(world)
    sampler = ItinerarySampler(world, {}, None)
    candidates = sampler.activity_candidates(routing_only=False)
    lookup = {p['place_id']: p for p in world.get('places', [])}
    requirements = {r['requirement_id']: r for r in sandbox.get('public_requirements', [])}
    nodes = {n.get('requirement_id'): (day, i, day.get('timeline', []))
             for day in (sandbox.get('construction_dag') or {}).get('days', [])
             for i, n in enumerate(day.get('timeline', [])) if n.get('requirement_id')}
    for contract in sandbox.get('requirement_contracts', []):
        if contract.get('candidate_comparison'):
            continue
        requirement = requirements.get(contract.get('requirement_id'))
        node = nodes.get(contract.get('requirement_id'))
        if not requirement or not node:
            continue
        target = next((p for p in candidates if p['place_id'] == contract['gt_place_id']
                       and p['service_type'] == contract.get('selected_service_type')), None)
        if not target:
            continue
        day, index, timeline = node
        contract['candidate_comparison'] = compare_selected_constraints(
            target, candidates, requirement.get('constraints', []), day.get('weekday_index', 0),
            timeline, index, FrozenRoutes(routes), lookup)
        contract['candidate_comparison']['source'] = 'bound_snapshot_restoration'
