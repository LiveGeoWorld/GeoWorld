from __future__ import annotations
from copy import deepcopy
from typing import Any
'Canonical, backward-compatible dynamic-event normalization.'
DYNAMIC_EVENT_SCHEMA_VERSION = 'geoword.dynamic_events.v2'

def normalize_event_payload(payload: Any) -> list[dict[str, Any]]:
    """Return canonical events, rejecting non-deterministic v2 envelopes."""
    if isinstance(payload, list):
        raw_events = payload
    elif isinstance(payload, dict):
        raw_events = payload.get('events', payload.get('env_events', []))
    else:
        raw_events = []
    normalized = [normalize_event(event, index) for (index, event) in enumerate(raw_events, 1) if isinstance(event, dict)]
    if isinstance(payload, dict) and payload.get('schema_version') == DYNAMIC_EVENT_SCHEMA_VERSION:
        for event in normalized:
            if event['activation'].get('mode') != 'absolute' or not event['activation_time']:
                raise ValueError('dynamic v2 events require absolute activation_time')
            if event['app'] == 'MapApp':
                raise ValueError('dynamic v2 events cannot modify MapApp')
            if not event.get('state_field'):
                raise ValueError('dynamic v2 events require state_field')
    return normalized

def normalize_event(event: dict[str, Any], index: int=1) -> dict[str, Any]:
    """Map legacy event fields into the GeoWorld dynamic-event v1 contract."""
    source = deepcopy(event)
    trigger = source.get('trigger_condition') or {}
    scope = source.get('scope') or {}
    if not isinstance(scope, dict):
        scope = {}
    if not scope:
        scope = {'type': source.get('scope_type', ''), 'id': source.get('scope_id', '')}
    activation = source.get('activation') or {}
    if not isinstance(activation, dict):
        activation = {}
    if not activation:
        trigger_type = trigger.get('type', '')
        if trigger_type in {'on_query', 'on_route'}:
            activation = {'mode': 'on_tool', 'tool': 'get_place_detail' if trigger_type == 'on_query' else 'query_routes', 'target': trigger.get('target', '')}
        elif trigger_type == 'on_start':
            activation = {'mode': 'on_start'}
        else:
            activation = {'mode': 'absolute'}
    activation_time = source.get('activation_time') or activation.get('time') or source.get('start_time') or source.get('timestep') or ''
    if activation_time:
        activation['time'] = activation_time
    state_patch = source.get('state_patch', source.get('entry', {}))
    if not isinstance(state_patch, dict):
        state_patch = {}
    normalized = {'schema_version': DYNAMIC_EVENT_SCHEMA_VERSION, 'event_id': source.get('event_id') or f'legacy_event_{index:04d}', 'event_type': source.get('event_type', 'unknown'), 'app': source.get('app', 'CityNewsApp'), 'scope': {'type': scope.get('type', ''), 'id': scope.get('id', '')}, 'public_scope': deepcopy(source.get('public_scope') or {}), 'activation': activation, 'activation_time': activation_time, 'end_time': source.get('end_time', ''), 'state_patch': state_patch, 'observability': source.get('observability') or {'mode': 'pull', 'notify': False}, 'causal_parents': list(source.get('causal_parents', source.get('depends_on', [])) or []), 'description': source.get('description', source.get('description_zh', '')), 'public_message': source.get('public_message', ''), 'state_field': source.get('state_field', ''), 'scenario_family_id': source.get('scenario_family_id', ''), 'dynamic_level': source.get('dynamic_level', ''), 'legacy_trigger_condition': trigger, 'source': source}
    for key in ('geometry', 'spatial_scope', 'effects'):
        saved = source
        while isinstance(saved, dict):
            if key in saved:
                normalized[key] = deepcopy(saved[key])
                break
            nested = saved.get('source')
            if nested is saved:
                break
            saved = nested
    return normalized
