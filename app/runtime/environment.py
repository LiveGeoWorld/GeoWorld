from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from app.runtime.dynamic_events import normalize_event_payload
from app.runtime.event_queue import EventQueue
from app.runtime.time_manager import TimeManager
from app.runtime.records import ToolCallRecord
'Environment mediates agent tools, sandbox apps, and virtual world time.'
_LEGACY_USER_INTERFACE_APP = 'AgentUserInterface'
_USER_INTERFACE_APP = 'UserInterfaceApp'

def _canonical_app_name(name: str) -> str:
    return _USER_INTERFACE_APP if name == _LEGACY_USER_INTERFACE_APP else name

def _canonical_tool_name(name: str) -> str:
    legacy_prefix = f'{_LEGACY_USER_INTERFACE_APP}__'
    if name.startswith(legacy_prefix):
        return f'{_USER_INTERFACE_APP}__{name[len(legacy_prefix):]}'
    return name

class Environment:
    """Test environment for one task with an auditable dynamic event lifecycle."""

    def __init__(self):
        self.apps: dict[str, Any] = {}
        self.time_manager = TimeManager()
        self.event_queue = EventQueue()
        self.trace: list[ToolCallRecord] = []
        self._call_seq = 0
        self.disruptions: list[dict[str, Any]] = []
        self.event_states: dict[str, str] = {}
        self.required_dynamic_event_ids: set[str] = set()
        self.causal_log: list[dict[str, Any]] = []
        self.dynamic_information_mode = 'pull'
        self.explicit_update_deliveries: list[dict[str, Any]] = []
        self.initial_plan_recorded = False
        self.planning_deadline = None
        self.terminal_state: dict[str, Any] | None = None
        self.terminate_on_invalid_tool = False
        self._public_app_tools: dict[str, set[str]] | None = None
        self._application_catalog: dict[str, str] | None = None
        self._opened_apps: set[str] = set()
        self._always_available_tools: set[tuple[str, str]] = set()

    def register_app(self, app: Any) -> None:
        self.apps[app.name] = app
        if hasattr(app, 'set_environment'):
            app.set_environment(self)

    def configure_public_tools(self, apps: dict[str, list[str]]) -> None:
        """Legacy fixed-tool configuration retained for unit-test compatibility."""
        self._application_catalog = None
        self._public_app_tools = {_canonical_app_name(name): set(tools) for (name, tools) in apps.items()}

    def configure_application_catalog(self, applications: dict[str, str], *, always_available_tools: set[tuple[str, str]] | None=None) -> None:
        """Install a task-independent App catalog with explicit app opening."""
        applications = {_canonical_app_name(name): description for (name, description) in applications.items()}
        always_available_tools = {(_canonical_app_name(app), tool) for (app, tool) in always_available_tools or set()}
        available_owners = set(self.apps)
        for app in self.apps.values():
            available_owners.update((str(schema.get('app') or app.name) for schema in app.get_tool_schemas()))
        missing = sorted(set(applications) - available_owners)
        if missing:
            raise ValueError(f"Application catalog refers to unregistered Apps: {', '.join(missing)}")
        self._public_app_tools = None
        self._application_catalog = dict(applications)
        self._opened_apps.clear()
        self._always_available_tools = always_available_tools

    def application_catalog(self) -> list[dict[str, str]]:
        if self._application_catalog is None:
            return []
        return [{'app_id': app_id, 'description': description} for (app_id, description) in self._application_catalog.items()]

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        if self._application_catalog is not None:
            return [self._open_app_schema(), *[schema for (schema, _, _) in self._iter_public_tools()]]
        return [schema for (schema, _, _) in self._iter_public_tools()]

    def _iter_public_tools(self):
        for app in self.apps.values():
            schemas = list(app.get_tool_schemas())
            if self.disruptions and app.name in {'CityNewsApp', 'WeatherApp', 'LocalServiceApp'}:
                schemas.append({'name': 'get_notice_history', 'app': app.name, 'description': 'Read published notices, including expired notices with their effective end times. This is history, not current state.', 'parameters': {'type': 'object', 'properties': {}, 'required': []}})
            for raw_schema in schemas:
                raw_name = str(raw_schema.get('name', ''))
                owner = str(raw_schema.get('app') or app.name)
                function_name = str(raw_schema.get('function_name') or raw_name)
                if self._application_catalog is not None:
                    if owner not in self._application_catalog:
                        continue
                    if owner not in self._opened_apps and (owner, raw_name) not in self._always_available_tools:
                        continue
                    public_name = f'{owner}__{raw_name}'
                elif self._public_app_tools is None:
                    public_name = raw_name
                else:
                    allowed = self._public_app_tools.get(owner)
                    if allowed is None or raw_name not in allowed:
                        continue
                    public_name = f'{owner}__{raw_name}'
                schema = {key: value for (key, value) in raw_schema.items() if key not in {'function_name', 'app'}}
                schema['name'] = public_name
                schema['app'] = owner
                handler = self._dynamic_notice_histories[app.name] if raw_name == 'get_notice_history' and self.disruptions and (app.name in getattr(self, '_dynamic_notice_histories', {})) else app
                yield (schema, handler, function_name)

    def execute_tool(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool_name = _canonical_tool_name(tool_name)
        self._call_seq += 1
        timestamp = datetime.now(timezone.utc).isoformat()
        started_at = self.time_manager.now_ms()
        if self._application_catalog is not None and tool_name == 'SystemApp__open_app':
            self.check_event('open_app', args)
            result = self._open_application(args)
            self.trace.append(ToolCallRecord(seq=self._call_seq, timestamp=timestamp, tool=tool_name, args=args, result=result, duration_ms=self.time_manager.now_ms() - started_at))
            return result
        target = next(((app, function_name) for (schema, app, function_name) in self._iter_public_tools() if schema['name'] == tool_name), None)
        if target is None and self._public_app_tools is None:
            target = next(((app, tool_name) for app in self.apps.values() if callable(getattr(app, tool_name, None))), None)
        if target:
            (app, function_name) = target
            self.check_event(function_name, args)
            func = getattr(app, function_name, None)
            if func and callable(func):
                try:
                    result = func(**args)
                except TypeError as exc:
                    result = {'status': 'invalid_arguments', 'error': str(exc)}
                    if self.terminate_on_invalid_tool:
                        self.terminate('invalid_tool_arguments', tool=tool_name)
                self.trace.append(ToolCallRecord(seq=self._call_seq, timestamp=timestamp, tool=tool_name, args=args, result=result, duration_ms=self.time_manager.now_ms() - started_at))
                return result
        result = {'status': 'invalid_tool', 'tool': tool_name}
        if self.terminate_on_invalid_tool:
            self.terminate('invalid_tool', tool=tool_name)
        self.trace.append(ToolCallRecord(seq=self._call_seq, timestamp=timestamp, tool=tool_name, args=args, result=result, duration_ms=self.time_manager.now_ms() - started_at))
        return result

    def terminate(self, reason: str, **details: Any) -> None:
        """End the current agent episode without giving a recovery hint."""
        if self.terminal_state is not None:
            return
        self.terminal_state = {'reason': reason, **details}
        self._record_causal('episode_terminated', reason=reason, **details)

    @staticmethod
    def _open_app_schema() -> dict[str, Any]:
        return {'name': 'SystemApp__open_app', 'app': 'SystemApp', 'description': 'Open one installed application and read its public tool interface.', 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {'app': {'type': 'string'}}, 'required': ['app']}}

    def _open_application(self, args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {'app'} or not isinstance(args.get('app'), str):
            if self.terminate_on_invalid_tool:
                self.terminate('invalid_tool_arguments', tool='SystemApp__open_app')
            return {'status': 'invalid_arguments'}
        app_id = _canonical_app_name(args['app'])
        if self._application_catalog is None or app_id not in self._application_catalog:
            if self.terminate_on_invalid_tool:
                self.terminate('invalid_application', app=app_id)
            return {'status': 'application_not_found'}
        self._opened_apps.add(app_id)
        tools = [schema for (schema, _, _) in self._iter_public_tools() if schema.get('app') == app_id]
        return {'status': 'opened', 'app': {'app_id': app_id, 'description': self._application_catalog[app_id]}, 'tools': tools}

    def inject_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Queue an external event for diagnostics or harness-level injection."""
        self.event_queue.push(event_type, payload)

    def load_dynamic_events(self, payload: Any, *, required_event_ids: set[str] | None=None) -> list[dict[str, Any]]:
        """Load v1 event envelopes and legacy lists through one contract."""
        self.disruptions = normalize_event_payload(payload)
        from app.runtime.dynamic_notice_history import DynamicNoticeHistory
        self._dynamic_notice_histories = {name: DynamicNoticeHistory() for name in ('CityNewsApp', 'WeatherApp', 'LocalServiceApp')}
        self.event_states = {event['event_id']: 'scheduled' for event in self.disruptions}
        loaded_ids = set(self.event_states)
        required = set() if required_event_ids is None else set(required_event_ids)
        missing = required - loaded_ids
        if missing:
            raise ValueError(f'Required Dynamic events were not loaded: {sorted(missing)}')
        self.required_dynamic_event_ids = required
        self._record_causal('events_loaded', event_count=len(self.disruptions))
        return list(self.disruptions)

    def pending_required_dynamic_events(self) -> bool:
        """Return whether a task-bound event must still occur before finalization."""
        events = {event['event_id']: event for event in self.disruptions}
        for event_id in self.required_dynamic_event_ids:
            if self.event_states.get(event_id) == 'applied':
                continue
            event = events[event_id]
            activation_time = event.get('activation_time')
            if activation_time and self.planning_deadline is not None:
                if self.time_manager._parse_time(activation_time) >= self.planning_deadline:
                    continue
            return True
        return False

    def init_events(self, task_timestep: str='', *, planning_deadline: str='') -> None:
        """Set the episode start and activate due start/absolute events."""
        if task_timestep:
            self.time_manager.set_virtual_time(task_timestep)
        self.planning_deadline = self.time_manager._parse_time(planning_deadline) if planning_deadline else None
        self._activate_due_events(include_start=True)

    def advance_virtual_time(self, target_time: str, reason: str='checkpoint') -> list[str]:
        """Advance instantly to a benchmark checkpoint and apply due events."""
        if self.planning_deadline is not None and self.time_manager._parse_time(target_time) > self.planning_deadline:
            raise ValueError('dynamic_time_exceeds_planning_window')
        previous = self.time_manager.virtual_time_iso()
        current = self.time_manager.advance_to(target_time).isoformat()
        self._record_causal('time_advanced', previous_time=previous, virtual_time=current, reason=reason)
        return self._activate_due_events(include_start=False)

    def advance_to_next_checkpoint(self, reason: str='initial_plan_submitted') -> dict[str, Any]:
        """Advance to the next absolute event checkpoint without wall-clock waiting."""
        if self.disruptions and (not self.initial_plan_recorded):
            self._record_causal('checkpoint_blocked', reason='initial_plan_required')
            return {'advanced': False, 'status': 'initial_plan_required', 'virtual_time': self.time_manager.virtual_time_iso(), 'activated_event_ids': [], 'notifications': []}
        current = self.time_manager.virtual_time()
        candidates: list[tuple[Any, dict[str, Any]]] = []
        for event in self.disruptions:
            end = event.get('end_time')
            if end and self.event_states.get(event['event_id']) == 'applied':
                end_point = self.time_manager._parse_time(end)
                if current is None or end_point > current:
                    candidates.append((end_point, event))
            if self.event_states.get(event['event_id']) != 'scheduled':
                continue
            if event.get('activation', {}).get('mode', 'absolute') != 'absolute':
                continue
            if not self._parents_applied(event):
                continue
            activation_time = event.get('activation_time', '')
            if not activation_time:
                continue
            try:
                event_time = self.time_manager._parse_time(activation_time)
            except ValueError:
                continue
            if current is None or event_time > current:
                candidates.append((event_time, event))
        if self.planning_deadline is not None:
            candidates = [(point, event) for (point, event) in candidates if point <= self.planning_deadline]
            if current is None or current < self.planning_deadline:
                candidates.append((self.planning_deadline, {}))
        if not candidates:
            self._record_causal('checkpoint_unavailable', reason=reason)
            return {'advanced': False, 'virtual_time': self.time_manager.virtual_time_iso(), 'activated_event_ids': [], 'notifications': []}
        checkpoint = min((event_time for (event_time, _) in candidates))
        activated = self.advance_virtual_time(checkpoint.isoformat(), reason=reason)
        notifications = []
        for event in self.disruptions:
            if event['event_id'] not in activated:
                continue
            if (event.get('observability') or {}).get('notify', False):
                notifications.append({'event_id': event['event_id'], 'description': event.get('description', '')})
        return {'advanced': True, 'virtual_time': self.time_manager.virtual_time_iso(), 'activated_event_ids': activated, 'notifications': notifications}

    def record_initial_plan(self, plan: dict[str, Any]) -> None:
        self.initial_plan_recorded = True
        self._record_causal('initial_plan_submitted', plan=plan)

    def check_event(self, tool_name: str, tool_args: dict[str, Any]) -> None:
        """Activate tool-observable events after the relevant tool call."""
        self._record_causal('tool_observed', tool=tool_name, args=tool_args)
        for event in self.disruptions:
            if self.event_states.get(event['event_id']) != 'scheduled':
                continue
            if not self._parents_applied(event):
                continue
            activation = event.get('activation', {})
            if activation.get('mode') != 'on_tool' or activation.get('tool') != tool_name:
                continue
            if self._tool_event_matches(tool_name, tool_args, activation.get('target', activation.get('match', ''))):
                self._apply_event(event, reason=f'tool:{tool_name}')

    @staticmethod
    def _tool_event_matches(tool_name: str, args: dict[str, Any], target: Any) -> bool:
        if isinstance(target, dict):
            return all((args.get(key) == value for (key, value) in target.items()))
        return False

    def _activate_due_events(self, include_start: bool) -> list[str]:
        current = self.time_manager.virtual_time()
        activated: list[str] = []
        made_progress = True
        while made_progress:
            made_progress = False
            for event in sorted(self.disruptions, key=lambda item: self.time_manager._parse_time(item.get('activation_time') or self.time_manager.virtual_time_iso())):
                if self.event_states.get(event['event_id']) != 'scheduled':
                    continue
                if not self._parents_applied(event):
                    continue
                mode = event.get('activation', {}).get('mode', 'absolute')
                if self.planning_deadline is not None and event.get('activation_time') and (self.time_manager._parse_time(event['activation_time']) >= self.planning_deadline):
                    continue
                is_start_event = mode == 'on_start' and include_start
                is_due_absolute_event = mode == 'absolute' and self._event_is_due(event, current)
                if is_start_event and self._event_is_due(event, current):
                    self._apply_event(event, reason='episode_start')
                elif is_due_absolute_event:
                    self._apply_event(event, reason='virtual_time')
                else:
                    continue
                if self.event_states.get(event['event_id']) == 'applied':
                    activated.append(event['event_id'])
                    made_progress = True
        for app in self.apps.values():
            if current is not None and hasattr(app, 'refresh_dynamic_state'):
                app.refresh_dynamic_state(current)
        return activated

    def _parents_applied(self, event: dict[str, Any]) -> bool:
        parents = event.get('causal_parents', []) or []
        return all((self.event_states.get(parent) == 'applied' for parent in parents))

    def _event_is_due(self, event: dict[str, Any], current: Any) -> bool:
        activation_time = event.get('activation_time', '')
        if not activation_time or current is None:
            return True
        try:
            return self.time_manager._parse_time(activation_time) <= current
        except ValueError:
            return False

    def _apply_event(self, event: dict[str, Any], reason: str) -> None:
        """Apply a state patch once and record the resulting state transition."""
        event_id = event['event_id']
        if self.event_states.get(event_id) == 'applied':
            return
        entry = dict(event.get('state_patch', {}))
        entry.setdefault('scope', event.get('scope', {}))
        entry.setdefault('timestep', event.get('activation_time', ''))
        entry.setdefault('end_time', event.get('end_time', ''))
        entry.setdefault('event_id', event_id)
        entry.setdefault('event_type', event.get('event_type', ''))
        entry.setdefault('state_field', event.get('state_field', ''))
        entry.setdefault('public_scope', event.get('public_scope', {}))
        entry.setdefault('public_message', event.get('public_message', ''))
        entry['spatial_notice'] = 'effects' in event
        app_name = event.get('app', '')
        if app_name == 'CityNewsApp' and (event.get('scope') or {}).get('type') == 'route_operation':
            source = event.get('source') or {}
            for key in ('geometry', 'spatial_scope'):
                if key in event:
                    entry[key] = event[key]
                elif key in source:
                    entry[key] = source[key]
        if app_name == 'LocalServiceApp':
            public_scope = event.get('public_scope') or {}
            service_name = str(public_scope.get('name') or '').strip()
            if entry.get('open_status') == 'closed' or entry.get('availability') == 'unavailable':
                fallback_notice = f'{service_name} is temporarily unavailable.'
            else:
                fallback_notice = str(event.get('description') or '')
            if not entry.get('public_message'):
                entry['public_message'] = event.get('public_message') or (fallback_notice if service_name else '')
        app = self.apps.get(app_name)
        if app and hasattr(app, 'inject_entry'):
            app.inject_entry(entry)
            if app_name in {'CityNewsApp', 'WeatherApp', 'LocalServiceApp'}:
                public = event.get('public_scope') or {}
                if app_name == 'CityNewsApp':
                    public = app.public_notice_scope(entry)
                self._dynamic_notice_histories[app_name].notices.append({'published_at': event.get('activation_time', ''), 'end_time': event.get('end_time', ''), 'message': str(entry.get('public_message') or entry.get('description') or entry.get('content') or ''), 'title': str(entry.get('title') or ''), 'scope': {key: value for (key, value) in public.items() if key in {'type', 'name', 'location', 'geometry', 'origin_name', 'dest_name', 'mode', 'radius_m'}}})
            self.event_states[event_id] = 'applied'
            self._record_causal('event_applied', event_id=event_id, event_type=event.get('event_type', ''), app=app_name, scope=event.get('scope', {}), reason=reason)
        else:
            self.event_states[event_id] = 'failed'
            self._record_causal('event_apply_failed', event_id=event_id, app=app_name, reason='target_app_missing_or_not_mutable')

    def _record_causal(self, kind: str, **details: Any) -> None:
        self.causal_log.append({'sequence': len(self.causal_log) + 1, 'kind': kind, 'virtual_time': self.time_manager.virtual_time_iso(), **details})

    def finalize(self) -> list[ToolCallRecord]:
        return list(self.trace)
