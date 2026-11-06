"""Expose curated place runtimes as tools in the unified test environment."""
from __future__ import annotations
from typing import Any, Callable
from test_runner.place.runtime import AppRuntime
from test_runner.place.session import TaskSession
from app.apps.base import BaseApp
from app.apps.map_app import MapAppSandbox

def discovery_route_evidence(session: TaskSession) -> dict[str, Any]:
    """Bind authoring routes to the public starting POI at the same coordinates."""
    example = session.curated_source.get('example') or {}
    contract = (example.get('evidence_ledger') or {}).get('construction_contract') or {}
    start = example.get('start') or {}
    place = next((p for p in session.world.get('places', []) if start.get('place_id') and p.get('place_id') == start['place_id']), None)
    if place is None:
        return {'cache': []}
    routes = []
    for row in contract.get('route_evidence') or []:
        origin = {'lat': row.get('origin_lat'), 'lng': row.get('origin_lng')}
        if not row.get('route') or not MapAppSandbox._same_location(origin, place.get('location')):
            continue
        routes.append({**row, 'origin_place_id': place['place_id'], 'origin_name': place['name']})
    return {'cache': routes}
_PLACE_RUNTIME_PARAMETERS: dict[str, dict[str, Any]] = {'search_places': {'type': 'object', 'additionalProperties': False, 'properties': {'keyword': {'type': 'string'}, 'city_hint': {'type': 'string'}, 'district_id': {'type': 'string'}, 'place_type': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1}}, 'required': ['keyword']}, 'search_grounding_candidates': {'type': 'object', 'additionalProperties': False, 'properties': {'keyword': {'type': 'string'}, 'area_hint': {'type': 'string'}, 'entity_type_hint': {'type': 'string'}, 'category': {'type': 'string'}, 'district_id': {'type': 'string'}, 'address_hint': {'type': 'string'}, 'near': {'type': 'string'}, 'near_radius_m': {'type': 'integer', 'minimum': 1}, 'city_hint': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1}}, 'required': []}, 'get_nearby': {'type': 'object', 'additionalProperties': False, 'properties': {'place_id': {'type': 'string'}, 'radius_m': {'type': 'integer', 'minimum': 1}, 'place_type': {'type': 'string'}}, 'required': ['place_id']}, 'select_grounded_location': {'type': 'object', 'additionalProperties': False, 'properties': {'candidate_id': {'type': 'string'}, 'rationale': {'type': 'string'}}, 'required': ['candidate_id']}, 'search_services': {'type': 'object', 'additionalProperties': False, 'properties': {'keyword': {'type': 'string'}, 'city_hint': {'type': 'string'}, 'service_type': {'type': 'string'}, 'near': {'type': 'string'}, 'radius_m': {'type': 'integer', 'minimum': 1}, 'filters': {'type': 'object'}, 'limit': {'type': 'integer', 'minimum': 1}}, 'required': []}, 'get_service_detail': {'type': 'object', 'additionalProperties': False, 'properties': {'name': {'type': 'string'}, 'location': {'type': 'object', 'properties': {'lat': {'type': 'number'}, 'lng': {'type': 'number'}}}}, 'required': ['name']}, 'compare_services': {'type': 'object', 'additionalProperties': False, 'properties': {'services': {'type': 'array', 'items': {'type': 'object', 'properties': {'name': {'type': 'string'}, 'location': {'type': 'object'}}}}, 'service_names': {'type': 'array', 'items': {'type': 'string'}}, 'constraints': {'type': 'array'}}, 'required': ['services']}, 'check_availability': {'type': 'object', 'additionalProperties': False, 'properties': {'name': {'type': 'string'}, 'location': {'type': 'object', 'properties': {'lat': {'type': 'number'}, 'lng': {'type': 'number'}}}}, 'required': ['name']}}

class PlaceRuntimeSandbox(BaseApp):
    """Adapter that turns MapApp/LocalService runtime calls into agent tools.

    The adapter intentionally exposes no gold answer. It records the exact
    runtime app/function/arguments/response sequence used by judge_sequence.
    """
    name = 'PlaceRuntimeSandbox'

    def __init__(self, runtime: AppRuntime, *, include_map: bool=True, include_local_service: bool=True, route_map: MapAppSandbox | None=None) -> None:
        super().__init__()
        self.runtime = runtime
        self._route_map = route_map
        self.events: list[dict[str, Any]] = []
        self._tool_to_action: dict[str, str] = {}
        app_sources = []
        if include_map:
            app_sources.append(('MapApp', runtime.map_app))
        if include_local_service:
            app_sources.append(('LocalServiceApp', runtime.local_service))
        for (app_name, app) in app_sources:
            for function in app.list_tools():
                if function in self._tool_to_action:
                    raise ValueError(f'duplicate place runtime tool: {function}')
                self._tool_to_action[function] = f'{app_name}.{function}'

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        schemas = [{'name': function, 'app': action.split('.', 1)[0], 'description': f'{action} runtime tool', 'parameters': _PLACE_RUNTIME_PARAMETERS.get(function, {'type': 'object', 'additionalProperties': False, 'properties': {}, 'required': []})} for (function, action) in sorted(self._tool_to_action.items())]
        if self._route_map is not None:
            schemas.extend(({**schema, 'app': 'MapApp'} for schema in self._route_map.get_tool_schemas() if schema['name'] == 'query_routes'))
        return schemas

    def query_routes(self, **kwargs: Any) -> dict[str, Any]:
        if self._route_map is None:
            raise AttributeError('query_routes')
        response = self._route_map.query_routes(**kwargs)
        self._record_call('MapApp.query_routes', kwargs, response)
        return response

    def _record_call(self, action: str, args: dict[str, Any], response: dict[str, Any]) -> None:
        (app, function) = action.split('.', 1)
        self.events.append({'step': len(self.events) + 1, 'app': app, 'function': function, 'agent_action': action, 'args': args, 'response': response})

    def __getattr__(self, name: str) -> Callable[..., dict[str, Any]]:
        action = self._tool_to_action.get(name)
        if not action:
            raise AttributeError(name)

        def invoke(**kwargs: Any) -> dict[str, Any]:
            response = self.runtime.dispatch(action, dict(kwargs), {})
            self._record_call(action, dict(kwargs), response)
            return response
        return invoke

class PlaceGroundingSandbox(BaseApp):
    """Forward-only place-grounding policy on top of the shared MapApp runtime.

    This is not a second agent. It is a task-specific tool policy used by the
    normal ReAct loop: a model may either look up a city first or search places
    directly, but an empty lookup or invalid transition ends the episode before
    another model turn can use it as a retry hint.
    """
    name = 'PlaceGroundingSandbox'

    def __init__(self, runtime: AppRuntime) -> None:
        super().__init__()
        self.runtime = runtime
        self.phase = 'initial'
        self.events: list[dict[str, Any]] = []
        self._city_handles: dict[str, str] = {}
        self._candidate_handles: dict[str, str] = {}
        self.final_answer_data: dict[str, Any] | None = None

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [self._city_schema(), self._place_schema(), self._detail_schema(), self._select_schema(), self._submit_schema()]

    def search_city(self, **kwargs: Any) -> dict[str, Any]:
        if self.phase != 'initial':
            return self._invalid('invalid_grounding_stage', expected='initial')
        if set(kwargs) != {'city_name'} or not isinstance(kwargs.get('city_name'), str):
            return self._invalid('invalid_city_query')
        query = kwargs['city_name'].strip().casefold()
        city_matches = [(city_id, name) for (city_id, name) in self.runtime.map_app.city_names.items() if str(name).strip().casefold() == query]
        if not city_matches:
            self._record('MapApp', 'search_city', {'city_name': kwargs['city_name']}, {'cities': []})
            return self._terminate('city_lookup_empty')
        (city_id, city_name) = city_matches[0]
        handle = f'city_ref_{len(self._city_handles) + 1}'
        self._city_handles[handle] = city_id
        response = {'cities': [{'city_handle': handle, 'name': city_name}]}
        self._record('MapApp', 'search_city', {'city_name': kwargs['city_name']}, response)
        self.phase = 'city_resolved'
        return response

    def search_grounding_candidates(self, **kwargs: Any) -> dict[str, Any]:
        if self.phase not in {'initial', 'city_resolved', 'candidates_resolved'}:
            return self._invalid('invalid_grounding_stage', expected='initial_city_or_candidates_resolved')
        allowed = {'keyword', 'near', 'near_candidate_ref', 'near_radius_m', 'category', 'city_handle'}
        if set(kwargs) - allowed or not isinstance(kwargs.get('keyword'), str) or (not kwargs['keyword'].strip()):
            return self._invalid('invalid_place_query')
        city_handle = str(kwargs.get('city_handle') or '')
        if self.phase in {'city_resolved', 'candidates_resolved'} and city_handle and (city_handle not in self._city_handles):
            return self._invalid('missing_or_invalid_city_handle')
        if self.phase == 'initial' and city_handle:
            return self._invalid('city_handle_requires_successful_city_lookup')
        raw_args: dict[str, Any] = {'keyword': kwargs['keyword'].strip(), 'limit': 8}
        for key in ('near', 'category'):
            value = kwargs.get(key)
            if isinstance(value, str) and value.strip():
                raw_args[key] = value.strip()
        near_candidate_ref = kwargs.get('near_candidate_ref')
        if near_candidate_ref is not None:
            if not isinstance(near_candidate_ref, str) or not near_candidate_ref:
                return self._invalid('invalid_near_candidate_ref')
            candidate_id = self._candidate_handles.get(near_candidate_ref)
            if not candidate_id:
                return self._invalid('candidate_not_returned_by_lookup')
            raw_args['near_candidate_id'] = candidate_id
        if 'near_radius_m' in kwargs:
            try:
                raw_args['near_radius_m'] = int(kwargs['near_radius_m'])
            except (TypeError, ValueError):
                return self._invalid('invalid_near_radius')
        if city_handle:
            raw_args['city_id'] = self._city_handles[city_handle]
        raw = self.runtime.map_app.search_grounding_candidates(raw_args)
        candidates = raw.get('candidates') or []
        self._record('MapApp', 'search_grounding_candidates', raw_args, raw)
        if not candidates:
            return self._terminate('place_lookup_empty')
        public_candidates = []
        for item in candidates:
            candidate_id = str(item.get('candidate_id') or '')
            if not candidate_id:
                continue
            handle = f'place_ref_{len(self._candidate_handles) + 1}'
            self._candidate_handles[handle] = candidate_id
            public_candidates.append({'candidate_handle': handle, 'name': item.get('name', ''), 'address': item.get('address', ''), 'type': item.get('primary_type') or item.get('type') or (item.get('tags') or [''])[0], 'city': item.get('city_name', '')})
        if not public_candidates:
            return self._terminate('place_lookup_empty')
        self.phase = 'candidates_resolved'
        return {'candidates': public_candidates}

    def get_grounding_candidate_detail(self, **kwargs: Any) -> dict[str, Any]:
        if self.phase != 'candidates_resolved':
            return self._invalid('invalid_grounding_stage', expected='candidates_resolved')
        if set(kwargs) != {'place_handle'} or not isinstance(kwargs.get('place_handle'), str):
            return self._invalid('invalid_place_handle')
        handle = kwargs['place_handle']
        candidate_id = self._candidate_handles.get(handle)
        if not candidate_id:
            return self._invalid('candidate_not_returned_by_lookup')
        raw = self.runtime.map_app.select_grounded_location({'candidate_id': candidate_id})
        selected = raw.get('selected') or {}
        selected_type = selected.get('primary_type') or selected.get('type') or (selected.get('tags') or [''])[0]
        response = {'place': {'place_handle': handle, 'name': selected.get('name', ''), 'address': selected.get('address', ''), 'type': selected_type}}
        self._record('MapApp', 'get_place_detail', {'candidate_id': candidate_id}, response)
        return response

    def select_grounded_location(self, **kwargs: Any) -> dict[str, Any]:
        if self.phase != 'candidates_resolved':
            return self._invalid('invalid_grounding_stage', expected='candidates_resolved')
        if set(kwargs) - {'candidate_handle', 'rationale'} or not isinstance(kwargs.get('candidate_handle'), str):
            return self._invalid('invalid_candidate_selection')
        handle = kwargs['candidate_handle']
        candidate_id = self._candidate_handles.get(handle)
        if not candidate_id:
            return self._invalid('candidate_not_returned_by_lookup')
        raw = self.runtime.map_app.select_grounded_location({'candidate_id': candidate_id, 'rationale': str(kwargs.get('rationale') or '')})
        self._record('MapApp', 'select_grounded_location', {'candidate_id': candidate_id, 'rationale': str(kwargs.get('rationale') or '')}, raw)
        selected = raw.get('selected') or {}
        self.phase = 'candidate_selected'
        return {'selected': {'candidate_handle': handle, 'name': selected.get('name', '')}}

    def submit_grounding_answer(self, **kwargs: Any) -> dict[str, Any]:
        if self.phase != 'candidate_selected':
            return self._invalid('invalid_grounding_stage', expected='candidate_selected')
        if set(kwargs) != {'content'} or not isinstance(kwargs.get('content'), str) or (not kwargs['content'].strip()):
            return self._invalid('invalid_grounding_response')
        content = kwargs['content'].strip()
        self._record('UserInterfaceApp', 'send_message_to_user', {'content': content}, {'status': 'sent'})
        self.final_answer_data = {'content': content}
        self.phase = 'terminal'
        return self._terminate('grounding_submitted')

    def _invalid(self, reason: str, **details: Any) -> dict[str, Any]:
        self._record('PlaceGroundingSandbox', 'invalid_action', {'phase': self.phase}, {'status': 'invalid'})
        return self._terminate(reason, **details)

    def _terminate(self, reason: str, **details: Any) -> dict[str, Any]:
        self.phase = 'terminal'
        if self.env is not None:
            self.env.terminate(reason, **details)
        return {'status': 'terminal', 'reason': reason}

    def _record(self, app: str, function: str, args: dict[str, Any], response: dict[str, Any]) -> None:
        self.events.append({'step': len(self.events) + 1, 'app': app, 'function': function, 'agent_action': f'{app}.{function}', 'args': args, 'response': response})

    @staticmethod
    def _city_schema() -> dict[str, Any]:
        return {'name': 'search_city', 'app': 'MapApp', 'description': "Search one exact English city name before a city-scoped place lookup. Use the English map name, for example city_name='Shanghai', not '上海'.", 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {'city_name': {'type': 'string', 'description': "One exact English city name, such as 'Shanghai' or 'Cape Town'."}}, 'required': ['city_name']}}

    @staticmethod
    def _place_schema() -> dict[str, Any]:
        return {'name': 'search_places', 'function_name': 'search_grounding_candidates', 'app': 'MapApp', 'description': 'Search public map places. Use exactly the declared parameter names. Put one concise public place name or category in keyword, preferably the exact original-language name previously observed from MapApp; English place categories are also accepted. Put city, street, area, and other spatial context in city_handle, near, or near_candidate_ref instead of combining all clues into keyword.', 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {'keyword': {'type': 'string', 'description': "One concise public place name or English category, such as 'Trenchtown', '上海新世界城', or 'restaurant'. Prefer the exact original-language public name returned by MapApp when available. Do not append city names, street names, areas, visual descriptions, nearby landmarks, reasoning, or the full user request. For example, use keyword='V&A Waterfront' with spatial context supplied separately, not keyword='V&A Waterfront shopping mall Cape Town'."}, 'city_handle': {'type': 'string', 'description': 'A city_handle returned by MapApp__search_city in this episode.'}, 'near': {'type': 'string', 'description': 'One concise public landmark name, preferably copied from MapApp, used as spatial context.'}, 'near_candidate_ref': {'type': 'string', 'description': 'A candidate_handle returned by an earlier MapApp__search_places call in this episode; searches near that specific candidate.'}, 'near_radius_m': {'type': 'integer', 'minimum': 1, 'description': 'Search radius in metres around near or near_candidate_ref.'}, 'category': {'type': 'string', 'description': "One concise English place category, such as 'hotel' or 'shopping mall'."}}, 'required': ['keyword']}}

    @staticmethod
    def _detail_schema() -> dict[str, Any]:
        return {'name': 'get_place_detail', 'function_name': 'get_grounding_candidate_detail', 'app': 'MapApp', 'description': "Read public details for one place handle returned by this episode's search.", 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {'place_handle': {'type': 'string'}}, 'required': ['place_handle']}}

    @staticmethod
    def _select_schema() -> dict[str, Any]:
        return {'name': 'select_grounded_location', 'app': 'MapApp', 'description': "REQUIRED SUBMISSION CHECKPOINT: Select exactly one candidate returned by the current episode's place lookup before calling UserInterfaceApp__final_answer. Finding, inspecting, or naming the correct place does not count as selecting it. After this tool succeeds, submit the user-facing answer with UserInterfaceApp__final_answer.", 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {'candidate_handle': {'type': 'string'}, 'rationale': {'type': 'string'}}, 'required': ['candidate_handle']}}

    @staticmethod
    def _submit_schema() -> dict[str, Any]:
        return {'name': 'final_answer', 'function_name': 'submit_grounding_answer', 'app': 'UserInterfaceApp', 'description': 'FINAL STEP ONLY: Send the user-facing grounding answer only after a successful MapApp__select_grounded_location call. Before this tool, you must explicitly select the chosen candidate_handle; merely writing the correct place name in content is not a submission and will fail with invalid_grounding_stage.', 'parameters': {'type': 'object', 'additionalProperties': False, 'properties': {'content': {'type': 'string'}}, 'required': ['content']}}
