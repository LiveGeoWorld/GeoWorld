from __future__ import annotations
import copy
from test_runner.verification.safety_planning.task_time import OCCUPANCY_CONTRACTS, time_fields
from test_runner.verification.safety_planning.event_contract import validate_event
from test_runner.verification.safety_planning.protection import validate_protections
from test_runner.verification.safety_planning.visual_context import evidence_refs, prepare_visual_context, validate_avoidance

class SafetyEvidenceIndex:
    """Append only after a completed choice; never solve or modify prior targets.

    Archived state contracts retain their original checks. Occupancy histories
    additionally check the frozen geometry before accepting a new entry.
    """

    def __init__(self, world_binding: str):
        if not isinstance(world_binding, str) or not world_binding.strip():
            raise ValueError('Safety history requires a frozen world binding')
        self.world_binding = world_binding
        self.generation_binding = {}
        self._entries: list[dict] = []

    @property
    def entries(self) -> list[dict]:
        return copy.deepcopy(self._entries)

    def prior_records(self) -> list[dict]:
        records = []
        for entry in self._entries:
            (context, event) = (entry['context'], entry['event'])
            if context.get('protection_contract') in OCCUPANCY_CONTRACTS:
                from test_runner.verification.safety_planning.occupancy import occupancy_records
                records.extend(occupancy_records([entry]))
                continue
            anchor = next((a for a in context['geographic_anchors'] if a['scope_ref'] == event['scope_ref']))
            records.append({'record_ref': f"{entry['owner']}:event", 'role': 'event', 'active_window': event['active_window'], 'description': event['description'], 'source_refs': anchor['source_refs'], 'scopes': [anchor], 'effects': event['effects'], 'routes': [route for route in context.get('route_evidence', []) if set(anchor['source_refs']) & {route['source_ref'], *(s['source_ref'] for s in route.get('steps', []))}]})
            if context.get('protection_contract') == 'safety_state_protection.v1':
                records[-1]['protection_contract'] = context['protection_contract']
                for (number, state) in enumerate(event['protected_states']):
                    sources = set(state['source_refs'])
                    records.append({'record_ref': f"{entry['owner']}:protection:{number}", 'role': 'protected_state', 'protection_contract': context['protection_contract'], **copy.deepcopy(state), 'scopes': [a for a in context['geographic_anchors'] if a['scope_ref'] in state['scope_refs']], 'routes': [r for r in context.get('route_evidence', []) if sources.intersection({r['source_ref'], *(s['source_ref'] for s in r.get('steps', []))})], 'places': [p for p in context.get('places', []) if p['source_ref'] in sources]})
                continue
            records.append({'record_ref': f"{entry['owner']}:dependencies", 'role': 'decision_dependency', 'active_window': context['activity_window'], 'description': 'Protected ordinary candidates, routes and decision requirements of an existing task.', 'candidate_refs': [row['candidate_ref'] for row in context['candidate_profiles']], 'candidate_profiles': context['candidate_profiles'], 'public_place_constraints': context['public_place_constraints'], 'source_refs': sorted(evidence_refs(context)), 'routes': context.get('route_evidence', []), 'places': context.get('places', []), 'scopes': context['geographic_anchors']})
        return copy.deepcopy(records)

    def prepare(self, context: dict, *, event_generation_window: dict) -> dict:
        if context.get('protection_contract') in OCCUPANCY_CONTRACTS:
            from test_runner.verification.safety_planning.occupancy import prepare
            return prepare(context, self.entries, event_generation_window)
        return prepare_visual_context(context, prior_records=self.prior_records(), event_generation_window=event_generation_window)

    def validate(self, owner: str, event: dict, context: dict) -> None:
        if 'events' in event:
            from test_runner.verification.safety_planning.event_bundle import materialize
            rebuilt = materialize(event, context)
            if rebuilt['geographic_anchors'] != context['geographic_anchors']:
                raise ValueError('History lacks reproducible simulated geometry')
            if rebuilt.get('compiled_gt_protection') != context.get('compiled_gt_protection'):
                raise ValueError('History lacks reproducible GT protection')
        if not isinstance(owner, str) or not owner.strip() or any((e['owner'] == owner for e in self._entries)):
            raise ValueError('An existing Safety task cannot be overwritten; require a distinct owner')
        expected = self.prepare(context, event_generation_window=context['event_generation_window'])
        if expected['prior_spacetime_records'] != context['prior_spacetime_records']:
            raise ValueError('Safety prior evidence changed or was omitted after the model input was prepared')
        if expected.get('interval_occupancy_records') != context.get('interval_occupancy_records'):
            raise ValueError('Safety interval occupancy changed after preparation')
        profiles = context['candidate_profiles']
        refs = [row['candidate_ref'] for row in profiles]
        if len(refs) < 2 or len(refs) != len(set(refs)) or (not evidence_refs(context)):
            raise ValueError('Safety history requires complete candidate dependencies and geographic sources')
        if not isinstance(context.get('public_place_constraints'), list):
            raise ValueError('Safety history must retain the ordinary decision constraints')
        if 'time_allocation' in context:
            if any((context['time_allocation'].get(key) != context[key] for key in (*time_fields(context), 'event_generation_window'))):
                raise ValueError('Safety time allocation differs from the protected decision window')
        validate_event(event, context)
        validate_protections(event, context)
        if context.get('protection_contract') in OCCUPANCY_CONTRACTS:
            from test_runner.verification.safety_planning.occupancy import CONTRACT, final_spatial_check, validate
            if context.get('protection_contract') == CONTRACT:
                check = final_spatial_check(event, context, self.entries)
                if 'final_spatial_check' in context:
                    stored = context['final_spatial_check']
                    if isinstance(stored, dict) and 'conflict_details' not in stored and (check.get('conflict_details') == []):
                        stored = {**stored, 'conflict_details': []}
                    if stored != check:
                        raise ValueError('Stored final spatial check differs from committed history')
            validate(event, context)
        else:
            validate_avoidance(event, context)

    def append(self, owner: str, event: dict, context: dict) -> None:
        self.validate(owner, event, context)
        saved = {key: copy.deepcopy(context[key]) for key in ('task_time', 'activity_window', 'event_generation_window', 'geographic_anchors', 'candidate_profiles', 'public_place_constraints', 'constrained_modes', 'route_evidence', 'places', 'place_evidence', 'protection_contract', 'screening_anchors', 'screening_has_routes', 'construction_context', 'compiled_gt_protection', 'time_allocation') if key in context}
        from test_runner.verification.safety_planning.occupancy import CONTRACT, final_spatial_check
        if context.get('protection_contract') == CONTRACT:
            saved['final_spatial_check'] = final_spatial_check(event, context, self.entries)
        self._entries.append({'owner': owner, 'event': copy.deepcopy(event), 'context': saved})

    def to_dict(self) -> dict:
        return {'schema_version': 'safety_evidence_history.v1', 'world_binding': self.world_binding, 'entries': self.entries, **self.generation_binding}

    @classmethod
    def from_dict(cls, payload: dict, *, world_binding: str) -> SafetyEvidenceIndex:
        if payload.get('schema_version') != 'safety_evidence_history.v1' or payload.get('world_binding') != world_binding:
            raise ValueError('Unsupported or mismatched Safety evidence history')
        if not isinstance(payload.get('entries'), list):
            raise ValueError('Safety evidence history requires an entry list')
        result = cls(world_binding)
        result.generation_binding = {key: payload[key] for key in ('algorithm_version', 'batch_id') if key in payload}
        if payload.get('algorithm_version') in OCCUPANCY_CONTRACTS and any((entry['context'].get('protection_contract') != payload['algorithm_version'] for entry in payload['entries'])):
            raise ValueError('Occupancy history contains an old algorithm task')
        for entry in payload['entries']:
            context = result.prepare(entry['context'], event_generation_window=entry['context']['event_generation_window'])
            result.append(entry['owner'], entry['event'], context)
        return result
