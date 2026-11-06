from __future__ import annotations
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any
from shared.io import read_json
from app.assets.safety.event_view import read_event_view

def load_safety_event_view(root: Path) -> dict[str, list[dict]] | None:
    task_path = root / 'task.json'
    task = read_json(task_path) if task_path.is_file() else {}
    if task.get('task_type') != 'safety_planning':
        return None
    sandbox_path = root / 'task_sandbox.json'
    sandbox = read_json(sandbox_path) if sandbox_path.is_file() else {}
    binding = sandbox.get('safety_event_view')
    if binding is None:
        return None
    relative = Path(binding['path'])
    path = (root / relative).resolve()
    if relative.is_absolute() or not path.is_relative_to(root.resolve()):
        raise ValueError('Safety event view must be a task-local artifact')
    return read_event_view(read_json(path), world_binding=binding['world_binding'])

def matches_public_scope(notice: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    scope = notice.get('scope') or {}
    names = [scope.get('name'), scope.get('district_name'), scope.get('city_name')]
    names.extend(scope.get('district_names') or [])
    return query.strip().casefold() in {str(name).strip().casefold() for name in names if name}

def matches_time_range(notice: dict[str, Any], query: str) -> bool:
    if not query:
        return True
    from app.assets.safety.spacetime import TimeWindow, _instant
    window = notice['active_window']
    query = query.strip()
    if '/' in query:
        (start, end) = query.split('/', 1)
        return TimeWindow(**window).overlaps(TimeWindow(start, end))
    if len(query) == 10:
        zone = datetime.fromisoformat(window['start'].replace('Z', '+00:00')).tzinfo
        start = datetime.fromisoformat(query).replace(tzinfo=zone)
        return TimeWindow(**window).overlaps(TimeWindow(start.isoformat(), (start + timedelta(days=1)).isoformat()))
    point = _instant(query)
    return _instant(window['start']) <= point < _instant(window['end'])
