"""Replay frozen current-pipeline Writer inputs, without a legacy approximation."""
import copy
import json
from pathlib import Path

from shared.io import read_json


def load_current_writer_input(task_dir: Path) -> dict:
    path = Path(task_dir) / 'generation_log' / 'writer_messages.json'
    if not path.is_file():
        raise ValueError(f'{task_dir.name}: missing frozen current-pipeline Writer input; do not substitute legacy target fields')
    log = read_json(path)
    users = [m['content'] for m in log.get('messages', []) if m.get('role') == 'user']
    if len(users) != 1:
        raise ValueError(f'{task_dir.name}: expected exactly one frozen Writer user message')
    payload = json.loads(users[0])
    if set(payload) != {'task_context', 'activity_intent', 'public_place_constraints', 'response_requirement'}:
        raise ValueError(f'{task_dir.name}: incompatible Writer fields')
    context = payload['task_context']
    if not isinstance(context, dict) or any(not context.get(k) for k in ('traveler', 'city', 'starting_place', 'task_time')):
        raise ValueError(f'{task_dir.name}: incomplete current-pipeline context')
    if not isinstance(payload['public_place_constraints'], list):
        raise ValueError(f'{task_dir.name}: invalid frozen constraints')
    # An explicitly constructed empty C is valid; never manufacture one for missing data.
    if not payload['activity_intent'] or not payload['response_requirement']:
        raise ValueError(f'{task_dir.name}: incomplete Writer input')
    return copy.deepcopy(payload)
