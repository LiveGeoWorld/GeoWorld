from __future__ import annotations
from typing import Any

def public_event_message(event: dict[str, Any]) -> str:
    """Return the authored message, or project a legacy event's public fact.

    This reads only the public patch and public scope. Private scope IDs and
    solver explanations must never be used as fallback announcement text.
    """
    explicit = str(event.get('public_message') or '').strip()
    if explicit:
        return explicit
    patch = event.get('state_patch') or {}
    if not isinstance(patch, dict):
        return ''
    for key in ('content', 'description'):
        value = str(patch.get(key) or '').strip()
        if value:
            return value
    if event.get('app') == 'LocalServiceApp':
        public_scope = event.get('public_scope') or {}
        name = str(public_scope.get('name') or '').strip()
        if name and (patch.get('open_status') == 'closed' or patch.get('availability') == 'unavailable'):
            return f'{name} is temporarily unavailable.'
    return ''
