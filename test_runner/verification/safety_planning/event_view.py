from __future__ import annotations
import copy
import hashlib
import json
from typing import Any

def read_event_view(payload: dict[str, Any], *, world_binding: str) -> dict[str, list[dict]]:
    value = copy.deepcopy(payload)
    supplied_hash = value.pop('content_hash', None)
    actual = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    if supplied_hash != actual or value.get('schema_version') != 'safety_public_event_view.v1' or value.get('world_binding') != world_binding:
        raise ValueError('Invalid or mismatched frozen Safety event view')
    return value['apps']
