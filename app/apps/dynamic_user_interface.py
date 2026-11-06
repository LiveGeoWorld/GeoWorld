from datetime import datetime
from copy import deepcopy
from app.apps.user_interface import UserInterfaceApp
'Dynamic-only decision clock; leave the static UI contract untouched.'

class DynamicUserInterfaceApp(UserInterfaceApp):

    def __init__(self):
        super().__init__()
        self.answer_versions = []

    def submit_initial_plan(self, feasible, route_ids=None, mode='', explanation='', plan=None):
        self.initial_plan_data = self._build_plan_payload(feasible, route_ids, mode, explanation, plan)
        if self.env:
            self.env.record_initial_plan(self.initial_plan_data)
        return {'status': 'initial_plan_recorded', **self.get_simulation_time()}

    def get_simulation_time(self):
        return {'virtual_time': self.env.time_manager.virtual_time_iso() if self.env else None, 'planning_deadline': self.env.planning_deadline.isoformat() if self.env and self.env.planning_deadline else None}

    def advance_simulation(self, target_time, reason='agent_wait'):
        if not self.env:
            return {'advanced': False, **self.get_simulation_time()}
        try:
            if not isinstance(target_time, str) or datetime.fromisoformat(target_time.replace('Z', '+00:00')).tzinfo is None:
                raise ValueError('An absolute timestamp with timezone is required')
            self.env.advance_virtual_time(target_time, reason=reason)
        except (ValueError, TypeError):
            return {'advanced': False, 'status': 'invalid_target_time', 'message': 'Choose an ISO timestamp from the current time through the planning deadline.', **self.get_simulation_time()}
        return {'advanced': True, **self.get_simulation_time()}

    def final_answer(self, feasible, route_ids=None, mode='', explanation='', plan=None, answer_type='final', next_query_time=None):
        if self.final_answer_data is not None:
            return {'status': 'already_submitted', **self.get_simulation_time()}
        if answer_type not in {'intermediate', 'final'}:
            return {'status': 'invalid_answer_type', **self.get_simulation_time()}
        clock = self.get_simulation_time()
        if answer_type == 'intermediate':
            try:
                target = datetime.fromisoformat(next_query_time.replace('Z', '+00:00'))
                if target.tzinfo is None or not self.env or target <= self.env.time_manager.virtual_time() or (self.env.planning_deadline is not None and target > self.env.planning_deadline):
                    raise ValueError('invalid next query time')
            except (AttributeError, TypeError, ValueError):
                return {'status': 'invalid_next_query_time', 'message': 'An intermediate answer needs next_query_time: an absolute ISO timestamp with timezone, later than now and no later than the planning deadline. The round has not ended.', **clock}
        elif next_query_time is not None:
            return {'status': 'unexpected_next_query_time', 'message': 'A final answer ends the task; omit next_query_time.', **clock}
        payload = self._build_plan_payload(feasible, route_ids, mode, explanation, plan)
        version = {'round': len(self.answer_versions) + 1, 'answer_type': answer_type, 'virtual_time': clock['virtual_time'], 'answer': deepcopy(payload)}
        if answer_type == 'intermediate':
            version['next_query_time'] = next_query_time
            if self.initial_plan_data is None:
                self.initial_plan_data = deepcopy(payload)
                self.env.record_initial_plan(self.initial_plan_data)
            self.env.advance_virtual_time(next_query_time, reason='intermediate_answer_submitted')
            self.answer_versions.append(version)
            return {'status': 'intermediate_recorded', 'answer_type': answer_type, 'round': version['round'], 'next_round': version['round'] + 1, **self.get_simulation_time()}
        if self.initial_plan_data is None:
            return {'status': 'initial_plan_required', 'message': 'Submit an intermediate plan before the final answer.', **clock}
        event_information = plan.get('event_information') if isinstance(plan, dict) else None
        if not isinstance(event_information, list) or not event_information or any((not isinstance(item, str) or not item.strip() for item in event_information)):
            return {'status': 'event_information_required', 'message': 'The final plan must include nonempty plan.event_information from observed App messages.', **clock}
        self.answer_versions.append(version)
        self.final_answer_data = deepcopy(payload)
        return {'status': 'submitted', 'answer_type': 'final', 'round': version['round'], **clock}

    def get_tool_schemas(self):
        schemas = [s for s in super().get_tool_schemas() if s['name'] == 'final_answer']
        for schema in schemas:
            if schema['name'] == 'final_answer':
                schema['description'] = schema['description'].replace('提交最终方案。调用后任务结束。', "Submit the current round's plan. With answer_type=intermediate, include next_query_time: the first accepted intermediate plan is the required initial P0; the round then ends and the system advances to your chosen time, no later than the planning deadline, for further App queries. You decide whether to continue observing or finalize; pending events do not block finalization. With answer_type=final, omit next_query_time and include nonempty plan.event_information copied from relevant public App observations. A final answer is accepted only after P0. Both versions use the same source-task plan structure.")
                schema['parameters']['properties'].update({'answer_type': {'type': 'string', 'enum': ['intermediate', 'final'], 'description': 'Whether this plan ends the current round or the whole task.'}, 'next_query_time': {'type': 'string', 'description': 'Required only for intermediate: absolute ISO timestamp with timezone, later than now and no later than the planning deadline.'}})
                schema['parameters']['required'].append('answer_type')
                plan_schema = schema['parameters']['properties'].get('plan', {})
                plan_schema.setdefault('properties', {})['event_information'] = {'type': 'array', 'items': {'type': 'string', 'minLength': 1}, 'minItems': 1, 'description': 'Required for the final version: include ALL dynamic event messages actually observed through Apps that are relevant to this task or decision. Include both messages supporting the final plan and messages that make the original plan or other candidates unavailable. Copy each complete message verbatim from the App response, without summarizing, paraphrasing, translating, or combining messages. Ordinary query results such as route duration, distance, or regular opening hours do not replace event notices. Do not include messages you have not actually observed.'}
        schemas.append({'name': 'get_simulation_time', 'app': self.name, 'description': 'Read current virtual time and the planning deadline.', 'parameters': {'type': 'object', 'properties': {}, 'required': []}})
        return schemas
