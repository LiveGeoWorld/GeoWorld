from __future__ import annotations
from copy import deepcopy
from pathlib import Path
from typing import Any
from shared.io import read_json
from shared.event_time import event_active_at
from app.apps.base import BaseApp
from app.apps.safety_event_view import matches_public_scope, load_safety_event_view
'Saved public weather-event queries; never generate routine weather.'

class WeatherApp(BaseApp):
    name = 'WeatherApp'

    def __init__(self, task_dir: str=''):
        super().__init__()
        self._sandbox = {}
        self._static_state = {}
        self._event_history = []
        self._dynamic_now = None
        self._current_weather_by_scope = {}
        self._safety_view = None
        if task_dir:
            root = Path(task_dir)
            view = load_safety_event_view(root)
            self._safety_view = view['WeatherApp'] if view is not None else None
            if (root / 'task_sandbox.json').exists():
                self._sandbox = read_json(root / 'task_sandbox.json')
            if (root / 'static_app_state.json').exists():
                self._static_state = read_json(root / 'static_app_state.json').get('apps', {}).get(self.name, {})

    def inject_entry(self, entry: dict) -> None:
        record = deepcopy(entry)
        self._event_history.append(record)
        if record.get('condition') and (not record.get('spatial_notice')):
            self._current_weather_by_scope[self._scope_key(record.get('scope'))] = record

    def trigger_step(self, step: int) -> None:
        pass

    def refresh_dynamic_state(self, current) -> None:
        self._dynamic_now = current
        self._current_weather_by_scope = {}
        for record in self._event_history:
            if not record.get('spatial_notice') and record.get('condition') and event_active_at(record, current):
                self._current_weather_by_scope[self._scope_key(record.get('scope'))] = record

    def get_alerts(self, district_id: str='') -> dict[str, Any]:
        """Return saved notices only; empty does not mean safe weather."""
        alerts = [deepcopy(row) for row in self._safety_view or [] if matches_public_scope(row, district_id)]
        if self._safety_view is None:
            alerts.extend((self._public_baseline_notice(row) for row in self._static_state.get('alerts', []) if self._baseline_alert_active(row) and (matches_public_scope(row, district_id) or (district_id and self._scope_district(row.get('scope')) == district_id)) and (not self._current_weather_by_scope.get(self._scope_key(row.get('scope'))))))
        updates = []
        for record in self._event_history:
            if self._dynamic_now is not None and (not event_active_at(record, self._dynamic_now)):
                continue
            notice = {'scope': deepcopy(record.get('public_scope') or {}), 'description': record.get('public_message') or record.get('description', ''), 'published_at': record.get('timestep', ''), 'end_time': record.get('end_time', '')}
            if not (matches_public_scope(notice, district_id) or (district_id and self._scope_district(record.get('scope')) == district_id)):
                continue
            if not record.get('spatial_notice') and record.get('condition'):
                if self._current_weather_by_scope.get(self._scope_key(record.get('scope'))) is not record:
                    continue
                if self._is_benign_condition(str(record['condition'])):
                    if notice['description']:
                        updates.append({'published_at': notice['published_at'], 'end_time': notice['end_time'], 'message': notice['description'], 'scope': notice['scope']})
                    continue
            if notice['description']:
                alerts.append(notice)
        return {'alerts': alerts, 'count': len(alerts), 'updates': updates}

    @staticmethod
    def _public_baseline_notice(record: dict) -> dict:
        notice = deepcopy(record)
        scope = notice.get('scope')
        if isinstance(scope, dict):
            notice['scope'] = {key: value for (key, value) in scope.items() if key not in {'id', 'district_id', 'place_id', 'route_id', 'service_id'}}
        return notice

    def _baseline_alert_active(self, record: dict) -> bool:
        """Filter saved baseline notices using their written effective times."""
        if self._dynamic_now is None:
            return True
        from app.runtime.time_manager import TimeManager
        current = TimeManager._parse_time(self._dynamic_now)
        window = record.get('active_window') or {}
        start = window.get('start') or record.get('activation_time') or record.get('published_at') or record.get('timestamp') or record.get('timestep')
        end = window.get('end') or record.get('end_time')
        published = record.get('published_at') or record.get('timestamp') or record.get('timestep')
        if published and TimeManager._parse_time(published) > current:
            return False
        timing = {'activation_time': TimeManager._parse_time(start).isoformat() if start else '', 'end_time': TimeManager._parse_time(end).isoformat() if end else ''}
        return event_active_at(timing, current)

    def get_forecast(self, district_id: str='', hours: int=24) -> dict[str, Any]:
        """Unadvertised legacy entry: no forecast or fabricated values."""
        return {'status': 'unsupported_operation', 'forecast': [], 'count': 0}

    def get_current_conditions(self, district_id: str='') -> dict[str, Any]:
        """Unadvertised legacy entry: no routine weather."""
        return {'status': 'unsupported_operation'}

    @staticmethod
    def _scope_district(scope: Any) -> str:
        if not isinstance(scope, dict):
            return ''
        return str(scope.get('id') or '') if scope.get('type') == 'district' else ''

    def _scope_key(self, scope: Any) -> str:
        district = self._scope_district(scope)
        return f'district:{district}' if district else 'global'

    @staticmethod
    def _is_benign_condition(condition: str) -> bool:
        return condition.strip().casefold() in {'clear', 'sunny', 'fair', '晴', '天晴'}

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [{'name': 'get_alerts', 'description': '查询当前可见且有效的天气事件及解除通知。只返回保存的公开内容；空结果不代表天气安全。不提供天气预报或日常天气数据。', 'parameters': {'type': 'object', 'properties': {'district_id': {'type': 'string', 'description': '事件公开范围中的地区、城市或地点名称；不传则查询全部天气事件。'}}, 'required': []}}]
