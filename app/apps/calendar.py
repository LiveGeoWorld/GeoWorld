from __future__ import annotations
from pathlib import Path
from datetime import date as calendar_date
import re
from typing import Any
from shared.io import read_json
from app.apps.base import BaseApp
'CalendarApp sandbox — check participant availability and schedule conflicts.\n\nData source: task_sandbox.json (participant availability windows).\n'

class CalendarApp(BaseApp):
    """Sandboxed CalendarApp for multi-entity coordination tasks."""
    name = 'CalendarApp'
    _date_root = Path(__file__).resolve().parents[1] / 'assets/calendars/2026'
    _cities = {'shanghai': ('shanghai_smoke', 'Shanghai'), '上海': ('shanghai_smoke', 'Shanghai'), 'tokyo': ('tokyo_complex', 'Tokyo'), '东京': ('tokyo_complex', 'Tokyo'), 'singapore': ('singapore_singapore', 'Singapore'), '新加坡': ('singapore_singapore', 'Singapore'), 'london': ('london_smoke', 'London'), '伦敦': ('london_smoke', 'London'), 'new york': ('new_york_united_states', 'New York'), '纽约': ('new_york_united_states', 'New York'), 'cape town': ('cape_town_south_africa', 'Cape Town'), '开普敦': ('cape_town_south_africa', 'Cape Town')}

    def _date_interval(self, date='', start_date='', end_date=''):
        if date and (start_date or end_date):
            raise ValueError('Use date OR both start_date and end_date, not both.')
        if not date and (not (start_date and end_date)):
            raise ValueError('Provide date or both start_date and end_date.')
        values = (date, date) if date else (start_date, end_date)
        if any((not isinstance(v, str) or not re.fullmatch('\\d{4}-\\d{2}-\\d{2}', v) for v in values)):
            raise ValueError('Dates must be YYYY-MM-DD.')
        (first, last) = map(calendar_date.fromisoformat, values)
        if first.year != 2026 or last.year != 2026:
            raise ValueError('Only frozen 2026 dates are supported.')
        if first > last:
            raise ValueError('start_date must not exceed end_date.')
        return (first.isoformat(), last.isoformat())

    def get_weekday(self, date: str) -> dict:
        try:
            (first, _) = self._date_interval(date=date)
        except (ValueError, TypeError) as exc:
            return {'status': 'invalid_date', 'message': str(exc)}
        weekdays = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
        return {'status': 'ok', 'date': first, 'weekday': weekdays[calendar_date.fromisoformat(first).weekday()]}

    def _query_dates(self, city_name, date, start_date, end_date, festivals):
        city = self._cities.get(' '.join(str(city_name).split()).casefold())
        if not city:
            return {'status': 'city_not_found', 'message': 'Use Shanghai, Tokyo, Singapore, London, New York or Cape Town.'}
        try:
            (first, last) = self._date_interval(date, start_date, end_date)
        except (ValueError, TypeError) as exc:
            return {'status': 'invalid_date', 'message': str(exc)}
        result = {'status': 'ok', 'city_name': city[1], 'start_date': first, 'end_date': last}
        filename = 'festivals.json' if festivals else 'calendar.json'
        try:
            data = read_json(self._date_root / filename)['cities'][city[0]]
        except (OSError, ValueError, KeyError):
            return {'status': 'calendar_data_unavailable', 'message': 'Frozen calendar data could not be read.'}
        if festivals:
            result['festivals'] = [{'date': r['date'], 'name': r['name'], 'category': r['source_type']} for r in data['festivals'] if first <= r['date'] <= last]
        else:
            matched = [r for r in data['periods'] if r['start'] <= last and r['end'] >= first]
            result['holidays'] = [{'start_date': r['start'], 'end_date': r['end'], 'name': r['name']} for r in matched if r.get('kind', 'public_holiday') == 'public_holiday']
            result['makeup_workdays'] = [{'start_date': r['start'], 'end_date': r['end'], 'name': r['name']} for r in matched if r.get('kind') == 'working_day']
            if date:
                result.update(date=first, is_public_holiday=bool(result['holidays']), is_makeup_workday=bool(result['makeup_workdays']))
        return result

    def get_festivals(self, city_name: str, date: str='', start_date: str='', end_date: str='') -> dict:
        return self._query_dates(city_name, date, start_date, end_date, True)

    def get_holidays(self, city_name: str, date: str='', start_date: str='', end_date: str='') -> dict:
        return self._query_dates(city_name, date, start_date, end_date, False)

    def __init__(self, task_dir: str=''):
        super().__init__()
        self._sandbox: dict = {}
        self._event_history: list[dict] = []
        if task_dir:
            self._load(task_dir)

    def inject_entry(self, entry: dict) -> None:
        """Append a calendar transition and derive its current availability."""
        record = dict(entry)
        self._event_history.append(record)
        scope = record.get('scope', {}) if isinstance(record.get('scope'), dict) else {}
        character_id = str(record.get('character_id') or scope.get('id') or '')
        participant = self._participant(character_id)
        if not participant:
            return
        for key in ('available_from', 'must_leave_by'):
            if key in record:
                participant[key] = record[key]
        participant.setdefault('_calendar_history', []).append(record)

    def trigger_step(self, step: int) -> None:
        pass

    def _load(self, task_dir: str) -> None:
        root = Path(task_dir)
        path = root / 'task_sandbox.json'
        if path.exists():
            self._sandbox = read_json(path)
        snapshot_path = root / 'static_app_state.json'
        if snapshot_path.exists():
            snapshot = read_json(snapshot_path)
            apps = snapshot.get('apps', {}) if isinstance(snapshot, dict) else {}
            state = apps.get('CalendarApp', {}) if isinstance(apps, dict) else {}
            for patch in state.get('availability', []) if isinstance(state, dict) else []:
                if not isinstance(patch, dict):
                    continue
                for participant in self._sandbox.get('participants', []):
                    if participant.get('character_id') == patch.get('character_id'):
                        participant.update(patch)

    def _participant(self, char_id: str) -> dict | None:
        for p in self._sandbox.get('participants', []):
            if p.get('character_id') == char_id:
                return p
        return None

    def _resolve_public_participant(self, reference: str) -> tuple[dict | None, str]:
        """Resolve public names uniquely; retain exact legacy IDs for old calls."""
        normalized = ' '.join(reference.split()).casefold()
        matches = [p for p in self._sandbox.get('participants', []) if normalized and ' '.join(str(p.get('name', '')).split()).casefold() == normalized]
        if len(matches) > 1:
            return (None, 'participant_ambiguous')
        if matches:
            return (matches[0], 'ok')
        participant = self._participant(reference) if reference else None
        return (participant, 'ok' if participant else 'participant_not_found')

    def get_availability(self, character_id: str, date: str='') -> dict[str, Any]:
        """Get a participant's free time windows."""
        (p, status) = self._resolve_public_participant(character_id)
        if not p:
            return {'status': status, 'windows': [], 'message': 'Participant identity could not be resolved; availability is unknown.'}
        return {'status': 'ok', 'name': p.get('name', ''), 'windows': [{'start': p.get('available_from', '18:00'), 'end': p.get('must_leave_by', '21:00'), 'is_free': True}]}

    def check_conflicts(self, character_ids: list[str], start_time: str='18:00', end_time: str='21:00') -> dict[str, Any]:
        """Check if all participants are free in the given time window."""
        results = []
        for cid in character_ids:
            (p, status) = self._resolve_public_participant(cid)
            if not p:
                results.append({'status': status, 'available': None, 'message': 'Participant identity could not be resolved; availability is unknown.'})
                continue
            avail_start = p.get('available_from', '18:00')
            avail_end = p.get('must_leave_by', '21:00')
            available = start_time >= avail_start and end_time <= avail_end
            results.append({'status': 'available' if available else 'conflict', 'name': p.get('name', ''), 'available': available, 'conflict_with': '' if available else f'Requested interval is outside availability {avail_start}–{avail_end}.'})
        return {'result': results}

    def add_event(self, title: str, start_time: str, end_time: str, location: str='', attendee_ids: list[str] | None=None) -> dict[str, Any]:
        """Add a calendar event."""
        return {'status': 'created', 'event_id': f'evt_{title}', 'title': title}

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [{'name': 'get_weekday', 'description': '查询2026年指定日期的星期。date 必须为 YYYY-MM-DD。成功返回 status、date 和 weekday，weekday 为英文星期全称（Monday 至 Sunday）。无效日期或非2026年日期返回 invalid_date。不查询人物日程。', 'parameters': {'type': 'object', 'properties': {'date': {'type': 'string', 'description': 'YYYY-MM-DD'}}, 'required': ['date']}}, *[{'name': name, 'description': description, 'parameters': {'type': 'object', 'properties': {'city_name': {'type': 'string', 'description': '公开城市名称：Shanghai、Tokyo、Singapore、London、New York、Cape Town（支持对应中文名）。'}, 'date': {'type': 'string', 'description': '2026年单日 YYYY-MM-DD；与区间参数互斥。'}, 'start_date': {'type': 'string', 'description': '区间起日 YYYY-MM-DD，须同时给 end_date。'}, 'end_date': {'type': 'string', 'description': '区间止日 YYYY-MM-DD，包含起止两天。'}}, 'required': ['city_name']}} for (name, description) in [('get_festivals', '查询指定城市2026年单日或日期区间内已记录的节日。必须提供 city_name，并选择一种日期方式：提供 date，或同时提供 start_date 和 end_date；两种方式不能混用。日期格式为 YYYY-MM-DD，区间包含起止两天。成功返回 status、city_name、start_date、end_date 和 festivals；festivals 中每条记录包含 date、name 和 category。合法查询无命中时返回 status=ok、festivals=[]，表示查询范围内无已记录节日。未知城市返回 city_not_found；无效日期、缺失或混用日期参数、倒置区间或非2026年日期返回 invalid_date；冻结资料无法读取时返回 calendar_data_unavailable。节日日期不等同于公共假期。'), ('get_holidays', '查询指定城市2026年单日或日期区间内的公共假期及调休工作日。必须提供 city_name，并选择一种日期方式：提供 date，或同时提供 start_date 和 end_date；两种方式不能混用。日期格式为 YYYY-MM-DD，区间包含起止两天。成功返回 status、city_name、start_date、end_date、holidays 和 makeup_workdays；两类列表中的记录均包含 start_date、end_date 和 name。返回与查询范围相交的完整原始记录区间，不裁剪为查询区间。单日查询还返回 date、is_public_holiday 和 is_makeup_workday；区间查询不返回这两个是否标记。合法查询无命中时返回 status=ok 和两个空列表；单日的两个是否标记均为 false。未知城市返回 city_not_found；无效日期、缺失或混用日期参数、倒置区间或非2026年日期返回 invalid_date；冻结资料无法读取时返回 calendar_data_unavailable。公共假期与调休工作日分别列出，不代表人物休息、空闲或商户停业。')]], {'name': 'get_availability', 'description': '查看某个参与者今晚的空闲时间窗口。', 'parameters': {'type': 'object', 'properties': {'character_id': {'type': 'string', 'description': 'Participant public name from the task or contacts. The legacy parameter name does not require an internal ID.'}, 'date': {'type': 'string', 'description': '日期 YYYY-MM-DD，默认今天'}}, 'required': ['character_id']}}, {'name': 'check_conflicts', 'description': '检查多个参与者在指定时间段内是否空闲。', 'parameters': {'type': 'object', 'properties': {'character_ids': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Participant public names from the task or contacts; not internal IDs.'}, 'start_time': {'type': 'string', 'description': '开始时间 HH:MM'}, 'end_time': {'type': 'string', 'description': '结束时间 HH:MM'}}, 'required': ['character_ids']}}]
