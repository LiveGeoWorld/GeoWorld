from __future__ import annotations
import copy
from typing import Any
from app.assets.safety.task_time import applies, moment

def apply_event(event: dict[str, Any], profiles: list[dict[str, Any]], *, scope_members: set[str], activity_window: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Project explicit author effects using already-resolved private membership."""
    if 'events' in event:
        from app.assets.safety.event_bundle import events
        merged = {p['candidate_ref']: {'unavailable': False, 'delay_minutes': {}, 'matched_effects': [], 'unknown_effects': []} for p in profiles}
        for (index, item) in enumerate(events(event)):
            rows = apply_event(item, profiles, scope_members=scope_members[index], activity_window=activity_window)
            for (ref, row) in rows.items():
                dest = merged[ref]
                dest['unavailable'] |= row['unavailable']
                for (mode, delay) in row['delay_minutes'].items():
                    dest['delay_minutes'][mode] = dest['delay_minutes'].get(mode, 0) + delay
                for key in ('matched_effects', 'unknown_effects'):
                    dest[key].extend(({'event_index': index, 'effect_index': number} for number in row[key]))
        return merged
    active = applies(event['active_window'], activity_window)
    result = {}
    for profile in profiles:
        ref = profile['candidate_ref']
        row: dict[str, Any] = {'unavailable': False, 'delay_minutes': {}, 'matched_effects': [], 'unknown_effects': []}
        if active and ref in scope_members:
            for (index, effect) in enumerate(event['effects']):
                if any((condition['field'] in profile['facts'] and profile['facts'][condition['field']] != condition['equals'] for condition in effect['where'])):
                    continue
                if any((condition['field'] not in profile['facts'] for condition in effect['where'])):
                    row['unknown_effects'].append(index)
                    continue
                row['matched_effects'].append(index)
                if effect['kind'] == 'unavailable':
                    row['unavailable'] = True
                else:
                    mode = effect['transportation_mode']
                    row['delay_minutes'][mode] = row['delay_minutes'].get(mode, 0) + effect['delay_minutes']
        result[ref] = row
    return result

def public_event(event: dict[str, Any], anchor: dict[str, Any]) -> dict[str, Any]:
    """Explicit public whitelist: no candidate references, evidence, or impact map."""
    return {'info_type': 'notice', 'category': event['event_kind'], 'title': event['title'], 'description': event['description'], 'scope': copy.deepcopy(anchor['public_scope']), 'active_window': copy.deepcopy(event['active_window'])}
