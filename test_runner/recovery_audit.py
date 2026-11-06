"""Evaluator-only, append-only event files; no credentials or model hints."""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

def event(directory, event_type, **fields):
    from test_runner.recover_batch import atomic
    identifier = uuid4().hex
    payload = {'schema_version': 'geoworld.recovery_audit.v1', 'event_id': identifier, 'time_utc': datetime.now(timezone.utc).isoformat(), 'event_type': event_type, **fields}
    atomic(Path(directory) / (payload['time_utc'].replace(':', '-') + '_' + identifier + '.json'), payload)
    return payload
