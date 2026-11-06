from __future__ import annotations
import hashlib
import json
from pathlib import Path
from app.runtime.dynamic_events import normalize_event_payload
from app.runtime.message_senders import resolve_message_senders
'Resolve the public world event pool separately from private task evidence.'
DYNAMIC_EVENT_MODES = ('shared_world', 'task_only')

def validate_event_mode(mode: str) -> str:
    if mode not in DYNAMIC_EVENT_MODES:
        raise ValueError(f'Unsupported dynamic_event_mode: {mode}')
    return mode

def load_world_events(task_dir: Path, task_events, *, dynamic_event_mode: str='shared_world'):
    validate_event_mode(dynamic_event_mode)
    own = normalize_event_payload(task_events)
    provenance_path = task_dir / 'task_provenance.json'
    if not provenance_path.exists():
        from copy import deepcopy
        events = deepcopy(own)
        senders = resolve_message_senders(events, [task_dir])
        return (events, own, {'event_source': 'legacy_task_local', 'message_senders': senders, 'dynamic_event_mode': dynamic_event_mode, 'loaded_event_count': len(events)})
    provenance = json.loads(provenance_path.read_text(encoding='utf-8'))
    context = provenance.get('generation_context') or provenance
    world = provenance.get('world_id')
    city = provenance.get('city_id') or context.get('city_id')
    versions = context.get('world_city_versions') or context.get('required_city_versions') or {}
    version = versions.get(city, context.get('city_version'))
    if not world or not city or version is None:
        raise ValueError('Dynamic shared events require World, city and city version provenance')
    binding = f"{world}/{city}/v{int(str(version).lstrip('vV'))}"
    city_root = task_dir.parents[2]
    if city_root.name != city or city_root.parent.name != world:
        raise ValueError('Dynamic task directory does not match its World/city provenance')
    batch_root = task_dir.parent.parent
    batch_ledger_path = batch_root / 'city_event_ledger.json'
    city_ledger_path = city_root / 'city_event_ledger.json'
    is_formal_batch = (batch_root / 'world_provenance.json').exists() or (batch_root / 'batch_metadata.json').exists()
    ledger_path = batch_ledger_path if batch_ledger_path.exists() else city_ledger_path
    raw = ledger_path.read_bytes()
    ledger = json.loads(raw)
    if ledger.get('city_id') != city:
        raise ValueError('Shared event ledger city does not match task provenance')
    records = [event for event in ledger.get('events', []) if event.get('world_binding') == binding]
    events = normalize_event_payload(records)
    ids = [event['event_id'] for event in events]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate event IDs in shared world ledger')
    missing = {event['event_id'] for event in own} - set(ids)
    if missing:
        if is_formal_batch and (not batch_ledger_path.exists()):
            raise ValueError(f'Dynamic batch shared ledger is missing or stale: {batch_ledger_path}; city fallback {city_ledger_path} does not contain task events {sorted(missing)}')
        raise ValueError(f'Task events absent from bound shared ledger: {sorted(missing)}')
    for event in events:
        if set(event.get('causal_parents') or []) - set(ids):
            raise ValueError('Shared event causal parent outside bound world event pool')
    world_event_count = len(events)
    if dynamic_event_mode == 'task_only':
        own_ids = {event['event_id'] for event in own}
        events = [event for event in events if event['event_id'] in own_ids]
        selected_ids = {event['event_id'] for event in events}
        for event in events:
            if set(event.get('causal_parents') or []) - selected_ids:
                raise ValueError('Task-only event has causal parents outside task binding')
    senders = resolve_message_senders(events, sorted(city_root.glob('*/tasks/*')), binding)
    return (events, own, {'event_source': 'shared_city_ledger', 'world_binding': binding, 'ledger_path': str(ledger_path), 'ledger_sha256': hashlib.sha256(raw).hexdigest(), 'world_event_count': world_event_count, 'task_event_count': len(own), 'dynamic_event_mode': dynamic_event_mode, 'loaded_event_count': len(events), 'message_senders': senders, 'unbound_event_count': sum((not event.get('world_binding') for event in ledger.get('events', [])))})
