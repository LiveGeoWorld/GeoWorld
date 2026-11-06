from datetime import datetime
from test_runner.verification.safety_planning.spacetime import TimeWindow
from test_runner.verification.safety_planning.task_time import applies

class ProtectionConflict(ValueError):
    """A source-checked conflicting relation, not an ordinary pairing failure."""

    def __init__(self, record_refs, details=None):
        self.record_refs = sorted(set(record_refs))
        self.details = list(details or [])
        super().__init__('Existing Safety protection conflicts: ' + ', '.join(self.record_refs))

def validate_protections(event: dict, context: dict) -> None:
    if context.get('protection_contract') in {'safety_occupancy.v5', 'safety_occupancy.v6'}:
        from test_runner.verification.safety_planning.gt_protection import compile_protection
        resolved = compile_protection(event, context)
        if resolved['compiled_gt_protection'] != context.get('compiled_gt_protection'):
            raise ValueError('Missing compiled GT protection')
        return
    if 'events' in event:
        from test_runner.verification.safety_planning.event_bundle import events
        combined = {'protected_states': [r for item in events(event) for r in item.get('protected_states', [])]}
        return validate_protections(combined, context)
    if context.get('protection_contract') not in {'safety_state_protection.v1', 'safety_occupancy.v1', 'safety_occupancy.v2', 'safety_occupancy.v3', 'safety_occupancy.v4'}:
        return
    profiles = {row['candidate_ref'] for row in context['candidate_profiles']}
    anchors = {row['scope_ref']: row for row in context['geographic_anchors']}
    rows = event.get('protected_states')
    if not isinstance(rows, list) or not rows:
        raise ValueError('Joint event requires explicit protected states')
    activity = None if 'task_time' in context else TimeWindow(**context['activity_window'])
    parse = lambda value: datetime.fromisoformat(value.replace('Z', '+00:00'))
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('candidate_ref') not in profiles:
            raise ValueError('Protection must bind a retained core candidate')
        seen.add(row['candidate_ref'])
        scopes = row.get('scope_refs')
        if not isinstance(scopes, list) or not scopes or any((not isinstance(ref, str) or ref not in anchors for ref in scopes)):
            raise ValueError('Protection requires supplied scope references')
        refs = row.get('source_refs')
        scope_sources = {ref for scope in scopes for ref in anchors[scope].get('source_refs', [])}
        if not isinstance(refs, list) or not refs or any((not isinstance(ref, str) or ref not in scope_sources for ref in refs)) or any((not set(refs).intersection(anchors[scope].get('source_refs', [])) for scope in scopes)):
            raise ValueError("Protection must cite every selected scope's frozen sources")
        if any((not isinstance(row.get(key), str) or not row[key].strip() for key in ('state', 'decision_requirement', 'reason'))):
            raise ValueError('Protection requires explicit state and decision reasoning')
        window = row.get('active_window')
        if not isinstance(window, dict) or set(window) != {'start', 'end'}:
            raise ValueError('Protection requires an effective time interval')
        if any((not isinstance(value, str) for value in window.values())):
            raise ValueError('Protection times must be ISO strings')
        protected = TimeWindow(**window)
        if 'task_time' in context:
            if not applies(window, context['task_time']):
                raise ValueError('Protection must contain the task time')
        elif not (parse(protected.start) <= parse(activity.start) and parse(protected.end) >= parse(activity.end)):
            raise ValueError('Protection must cover the task activity window')
    if seen != profiles:
        raise ValueError('Protection must cover GT and every retained core competitor')
