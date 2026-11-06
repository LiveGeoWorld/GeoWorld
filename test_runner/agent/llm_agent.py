"""LLMAgent — LLM-powered agent using OpenAI-compatible function calling.

Supports both openai_compatible (/chat/completions) and openai_responses (/v1/responses) providers.

Usage:
    agent = LLMAgent(env, user_prompt, model_key="cliproxy_gpt55_pro")
    result = agent.run()
"""
from __future__ import annotations
import base64
import hashlib
import http.client
import json
import mimetypes
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from shared.config_loader import PipelineConfig
from shared.llm_client import api_connection
from app.runtime.environment import Environment
from test_runner.schema import AgentRunResult
from test_runner.agent.interface import AgentInterface

class _HTTPStatusError(RuntimeError):
    """HTTP failure with enough information to decide whether retrying is safe."""

    def __init__(self, status: int, body: str='') -> None:
        super().__init__(f'HTTP {status}: {body[:200]}')
        self.status = status

class LLMAgent(AgentInterface):
    """LLM function-calling agent for route planning tasks."""

    def __init__(self, env: Environment, user_prompt: str, model_key: str='cliproxy_gpt55_pro', config_yaml: str='config.yaml', max_steps: int=50, input_images: list[Path] | None=None, task_type: str=''):
        super().__init__(env, user_prompt)
        self.model_key = model_key
        self.config_yaml = config_yaml
        self.max_steps = max_steps
        self.task_type = task_type
        config = PipelineConfig(config_yaml)
        self._endpoint = config.model_endpoint(model_key)
        self._model = self._endpoint.get('model', 'gpt-5.5')
        self._api_key = self._endpoint.get('api_key', '')
        self._base_url = self._endpoint.get('base_url', '').rstrip('/')
        self._provider = self._endpoint.get('provider', 'openai_compatible')
        self._enable_thinking = bool(self._endpoint.get('enable_thinking', False))
        self._reasoning_effort = str(self._endpoint.get('reasoning_effort') or '').strip()
        self._request_timeout_seconds = _bounded_int(self._endpoint.get('request_timeout_seconds'), default=60, minimum=5, maximum=180)
        self._max_transient_retries = _bounded_int(self._endpoint.get('max_transient_retries'), default=1, minimum=0, maximum=3)
        self._retry_backoff_seconds = _bounded_float(self._endpoint.get('retry_backoff_seconds'), default=1.0, minimum=0.0, maximum=10.0)
        self._last_api_attempts: list[dict[str, Any]] = []
        self._native_tool_transport = bool(self._endpoint.get('native_tool_transport', False))
        self._max_protocol_retries = _bounded_int(self._endpoint.get('max_protocol_retries'), default=2, minimum=0, maximum=5)
        self._input_images = self._prepare_input_images(input_images or [])
        if self._input_images and (not bool(self._endpoint.get('supports_vision', False))):
            raise ValueError(f'Model endpoint {model_key!r} does not declare supports_vision=true; it cannot run a task with required image input.')

    def run(self, recovery: dict[str, Any] | None=None, checkpoint_sink=None) -> AgentRunResult:
        tool_schemas = self.env.get_tool_schemas()
        system_prompt = self._build_system_prompt(tool_schemas)
        messages = self._initial_messages(system_prompt)
        t0 = time.time()
        total_tokens = {'prompt': 0, 'completion': 0, 'total': 0}
        protocol_audit: dict[str, Any] = {}
        observation_context: list[dict[str, Any]] = []
        protocol_errors: list[dict[str, Any]] = []
        recovery_limit = getattr(self, '_max_protocol_retries', 2)
        start_step = 0
        previous_duration = 0.0
        original_messages = json.loads(json.dumps(messages))
        model_turns: list[dict[str, Any]] = []
        completed_model_steps = 0
        recovery_provenance: dict[str, Any] = {}
        round_retries = 0
        api_requests = []

        def persist(phase):
            if checkpoint_sink is None:
                return
            from dataclasses import asdict
            checkpoint_sink({'phase': phase, 'safe_to_resume': phase == 'boundary', 'row': {'user_prompt': self.user_prompt, 'model_name': self._model, 'trace': [asdict(t) for t in self.env.trace], 'tokens_used': dict(total_tokens), 'duration_s': round(previous_duration + time.time() - t0, 2), 'dynamic_event_states': dict(self.env.event_states), 'dynamic_information_mode': getattr(self.env, 'dynamic_information_mode', 'pull'), 'explicit_update_deliveries': list(getattr(self.env, 'explicit_update_deliveries', [])), 'answer_versions': list(getattr(self.env.apps.get('UserInterfaceApp'), 'answer_versions', [])), 'input_asset_hashes': [i['sha256'] for i in self._input_images], 'termination': {'category': 'model_api'}, 'agent_protocol': {'step': completed_model_steps + 1}, 'recovery_checkpoint': {'schema_version': 'geoword.agent_checkpoint.v1', 'initial_messages': original_messages, 'messages': messages, 'model_turns': model_turns, 'model_steps': completed_model_steps, 'max_steps': self.max_steps, 'max_protocol_retries': recovery_limit, 'retry_scope': 'per_call_round', 'round_retries': round_retries, 'api_requests': api_requests, 'protocol_errors': protocol_errors, 'input_reconstruction': recovery_provenance}}})
        if recovery is not None:
            from test_runner.recover_task import restore_agent
            restored = restore_agent(self, recovery, messages, system_prompt)
            messages = restored['messages']
            system_prompt = restored['system_prompt']
            observation_context = restored['observation_context']
            start_step = restored['start_step']
            original_messages = restored['initial_messages']
            model_turns = restored['model_turns']
            completed_model_steps = restored['model_steps']
            recovery_provenance = restored['provenance']
            total_tokens.update(recovery.get('tokens_used') or {})
            previous_duration = float(recovery.get('duration_s') or 0)
            protocol_errors = restored['protocol_errors']
            round_retries = restored['round_retries']
            api_requests = restored['api_requests']
            t0 = time.time()

        def recover(error: dict[str, Any], content: str) -> bool:
            nonlocal round_retries
            can_retry = round_retries < recovery_limit and step + 1 < self.max_steps
            if can_retry:
                round_retries += 1
            protocol_errors.append({**error, 'retry_allowed': can_retry, 'call_round': len(self.env.trace) + 1, 'round_retries': round_retries})
            if not can_retry:
                return False
            messages.append({'role': 'assistant', 'content': content or '[Rejected tool call; see schema error below.]'})
            messages.append({'role': 'user', 'content': 'Observation:\n' + json.dumps({'status': 'call_rejected', 'executed': False, 'error': error.get('error', 'unparseable_action'), 'format': 'Action: followed by one JSON object with exactly action (string) and action_input (object), ending with <end_action>.', 'instruction': 'Use only the already published tool schemas. Correct the call yourself. No action was executed.'})})
            if model_turns:
                model_turns[-1]['feedback'] = messages[-1]['content']
            persist('boundary')
            return True
        if recovery is not None:
            failed = recovery.get('agent_protocol') or {}
            category = (recovery.get('termination') or {}).get('category')
            if category in {'no_action', 'invalid_action'} and (not restored['retry_api_request']):
                step = start_step - 1
                if not recover(failed, str(failed.get('response') or '')):
                    protocol_audit = dict(failed)
                    self.env.terminate('agent_protocol_failure', category=category)
                    start_step = self.max_steps
        step = start_step - 1
        persist('boundary')
        for step in range(start_step, self.max_steps):
            tool_schemas = self.env.get_tool_schemas()
            if not tool_schemas:
                break
            from app.runtime.explicit_updates import compose_system_prompt, delivery_record
            (request_system, request_updates) = compose_system_prompt(system_prompt, self.env)
            messages[0]['content'] = request_system
            receipt = delivery_record(self.env, request_system, request_updates, step + 1)
            openai_tools = self._openai_tools(tool_schemas) if getattr(self, '_native_tool_transport', False) else []
            persist('api_inflight')

            def attempt_sink(attempt):
                if checkpoint_sink is not None:
                    checkpoint_sink({'phase': 'api_attempt', 'model_step': step + 1, 'call_round': len(self.env.trace) + 1, 'attempt': attempt})
            self._api_attempt_sink = attempt_sink
            resp = self._call_llm(messages, openai_tools)
            for attempt_record in getattr(self, '_last_api_attempts', []):
                if attempt_record.get('status') == 'empty_response':
                    empty_usage = attempt_record.get('usage') or {}
                    total_tokens['prompt'] += empty_usage.get('prompt_tokens', 0)
                    total_tokens['completion'] += empty_usage.get('completion_tokens', 0)
                    total_tokens['total'] += empty_usage.get('total_tokens', 0)
            api_requests.append({'model_step': step + 1, 'call_round': len(self.env.trace) + 1, 'attempts': list(getattr(self, '_last_api_attempts', []))})
            if not resp:
                protocol_audit = {'status': 'infrastructure_failure', 'step': step + 1, 'api_attempts': list(getattr(self, '_last_api_attempts', []))}
                self.env.terminate('evaluation_infrastructure_failure', category='model_api')
                break
            if getattr(self.env, 'dynamic_information_mode', 'pull') == 'explicit_update':
                self.env.explicit_update_deliveries.append(receipt)
            usage = resp.get('usage', {})
            total_tokens['prompt'] += usage.get('prompt_tokens', 0)
            total_tokens['completion'] += usage.get('completion_tokens', 0)
            total_tokens['total'] += usage.get('total_tokens', 0)
            (tool_call, finish, raw_content, transport) = self._parse_response(resp)
            completed_model_steps += 1
            model_turns.append({'step': step + 1, 'response': raw_content, 'transport': transport, 'finish_reason': finish, 'tool_call': tool_call, 'usage': usage, 'api_attempts': list(getattr(self, '_last_api_attempts', []))})
            if getattr(self.env, 'dynamic_information_mode', 'pull') == 'explicit_update':
                model_turns[-1]['system_sha256'] = receipt['system_sha256']
            persist('response_pending')
            if not tool_call:
                protocol_audit = {'status': 'no_action', 'step': step + 1, 'finish_reason': finish, 'response': raw_content}
                model_turns[-1]['error'] = 'unparseable_action'
                if recover(protocol_audit, raw_content):
                    protocol_audit = {}
                    continue
                self.env.terminate('agent_protocol_failure', category='no_action')
                break
            tool_name = tool_call.get('name', '')
            try:
                args_str = tool_call.get('arguments', '{}')
                if isinstance(args_str, dict):
                    tool_args = args_str
                else:
                    tool_args = json.loads(args_str)
            except (json.JSONDecodeError, TypeError):
                tool_args = {}
            (valid, validation_error) = self._validate_tool_call(tool_name, tool_args, tool_schemas)
            if not valid:
                protocol_audit = {'status': 'invalid_action', 'step': step + 1, 'finish_reason': finish, 'transport': transport, 'response': raw_content, 'error': validation_error, 'tool': tool_name, 'arguments': tool_args}
                model_turns[-1]['error'] = validation_error
                if recover(protocol_audit, raw_content):
                    protocol_audit = {}
                    continue
                self.env.terminate('agent_protocol_failure', category='invalid_action')
                break
            result = self.env.execute_tool(tool_name, tool_args)
            round_retries = 0
            model_turns[-1]['tool_seq'] = self.env.trace[-1].seq if self.env.trace else None
            if self.env.trace:
                last = self.env.trace[-1]
                last.llm_input = {'system_prompt': request_system, 'user_prompt': self.user_prompt, 'prior_observation_hashes': list(observation_context)}
                last.llm_output = {'tool': tool_name, 'arguments': tool_args, 'finish_reason': finish, 'transport': transport, 'response': raw_content, 'usage': resp.get('usage', {})}
            if self.env.terminal_state is not None:
                break
            messages.append({'role': 'assistant', 'content': raw_content or self._format_action(tool_name, tool_args)})
            observation_context.append({'seq': self.env.trace[-1].seq if self.env.trace else 0, 'sha256': hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()})
            messages.append({'role': 'user', 'content': 'Observation:\n' + json.dumps(result, ensure_ascii=False)})
            persist('terminal' if self._is_final_submission(tool_name, tool_schemas) and (getattr(self, 'task_type', '') != 'dynamic_replanning' or (isinstance(result, dict) and result.get('status') == 'submitted')) else 'boundary')
            if self._is_final_submission(tool_name, tool_schemas) and (getattr(self, 'task_type', '') != 'dynamic_replanning' or (isinstance(result, dict) and result.get('status') == 'submitted')):
                break
        else:
            self.env.terminate('agent_protocol_failure', category='step_limit_reached')
        if recovery_limit:
            protocol_audit.update({'policy': 'bounded_call_feedback_v2_per_round', 'max_protocol_retries': recovery_limit, 'retry_scope': 'per_call_round', 'round_retries': round_retries, 'errors': protocol_errors, 'model_steps': completed_model_steps})
        return AgentRunResult(task_id='', user_prompt=self.user_prompt, gt_route_id='', trace=self.env.finalize(), final_answer=getattr(self.env.apps.get('UserInterfaceApp', None), 'final_answer_data', None), initial_plan=getattr(self.env.apps.get('UserInterfaceApp', None), 'initial_plan_data', None), duration_s=round(previous_duration + time.time() - t0, 2), tokens_used=total_tokens, model_name=self._model, dynamic_event_states=dict(self.env.event_states), dynamic_causal_log=list(self.env.causal_log), dynamic_information_mode=getattr(self.env, 'dynamic_information_mode', 'pull'), explicit_update_deliveries=list(getattr(self.env, 'explicit_update_deliveries', [])), answer_versions=list(getattr(self.env.apps.get('UserInterfaceApp'), 'answer_versions', [])), termination=dict(self.env.terminal_state or {}), agent_protocol=protocol_audit, recovery_checkpoint={'schema_version': 'geoword.agent_checkpoint.v1', 'initial_messages': original_messages, 'messages': messages, 'model_turns': model_turns, 'model_steps': completed_model_steps, 'max_steps': self.max_steps, 'max_protocol_retries': recovery_limit, 'retry_scope': 'per_call_round', 'round_retries': round_retries, 'api_requests': api_requests, 'endpoint_contract': {'model': self._model, 'provider': getattr(self, '_provider', ''), 'base_url': getattr(self, '_base_url', ''), 'native_tool_transport': getattr(self, '_native_tool_transport', False), 'temperature': 0.1, 'max_completion_tokens': 10240, 'reasoning_effort': getattr(self, '_reasoning_effort', '')}, 'protocol_errors': protocol_errors, 'pending_api_attempts': list(getattr(self, '_last_api_attempts', [])) if protocol_audit.get('status') == 'infrastructure_failure' else [], 'input_reconstruction': recovery_provenance}, input_modalities=['text', *(['image'] if self._input_images else [])], input_asset_hashes=[item['sha256'] for item in self._input_images])

    def _call_llm(self, messages: list[dict], tools: list[dict]) -> dict | None:
        max_retries = int(getattr(self, '_max_transient_retries', 1))
        backoff_seconds = float(getattr(self, '_retry_backoff_seconds', 1.0))
        from datetime import datetime, timezone

        class AttemptLog(list):

            def append(log, value):
                value = {**value, 'started_at': attempt_started_at, 'recorded_at': datetime.now(timezone.utc).isoformat()}
                super().append(value)
                sink = getattr(self, '_api_attempt_sink', None)
                if sink:
                    sink(dict(value))
        attempts: list[dict[str, Any]] = AttemptLog()
        self._last_api_attempts = attempts
        for attempt in range(1, max(max_retries, 2) + 2):
            started = time.monotonic()
            attempt_started_at = datetime.now(timezone.utc).isoformat()
            try:
                response = self._call_responses(messages, tools) if self._provider == 'openai_responses' else self._call_chat(messages, tools)
                choices = response.get('choices') or []
                message = choices[0].get('message') or {} if choices else {}
                if not message.get('tool_calls') and (not str(message.get('content') or '').strip()):
                    attempts.append({'attempt': attempt, 'status': 'empty_response', 'message_fields': sorted(message), 'finish_reason': choices[0].get('finish_reason') if choices else None, 'content_type': type(message.get('content')).__name__, 'reasoning_length': len(str(message.get('reasoning_content') or '')), 'usage': response.get('usage') or {}})
                    raise ConnectionError('empty_model_answer')
                attempts.append({'attempt': attempt, 'status': 'success', 'duration_ms': round((time.monotonic() - started) * 1000)})
                return response
            except Exception as exc:
                empty_answer = isinstance(exc, ConnectionError) and str(exc) == 'empty_model_answer'
                retry_limit = 2 if empty_answer else max_retries
                retryable = self._is_retryable_api_error(exc)
                attempts.append({'attempt': attempt, 'status': 'error', 'retryable': retryable, 'error_type': type(exc).__name__, 'error': str(exc)[:200], 'duration_ms': round((time.monotonic() - started) * 1000)})
                if not retryable or attempt > retry_limit:
                    print(f'  [LLMAgent] API error: {exc}', file=sys.stderr)
                    return None
                delay = (10.0 if attempt == 1 else 30.0) if empty_answer else backoff_seconds * 2 ** (attempt - 1)
                attempts[-1]['retry_delay_seconds'] = delay
                sink = getattr(self, '_api_attempt_sink', None)
                if sink:
                    sink({'attempt': attempt, 'status': 'retry_scheduled', 'retry_delay_seconds': delay, 'recorded_at': datetime.now(timezone.utc).isoformat()})
                if delay:
                    time.sleep(delay)
        return None

    @staticmethod
    def _is_retryable_api_error(exc: Exception) -> bool:
        if isinstance(exc, _HTTPStatusError):
            return exc.status in {408, 409, 425, 429} or 500 <= exc.status <= 599
        return isinstance(exc, (http.client.RemoteDisconnected, http.client.CannotSendRequest, http.client.ResponseNotReady, ConnectionError, TimeoutError, socket.timeout, OSError))

    def _api_connection(self, endpoint: str):
        """Use the shared HTTP transport so formal runs honor proxy settings."""
        return api_connection(self._base_url, endpoint, self._request_timeout_seconds)

    def _call_chat(self, messages: list[dict], tools: list[dict]) -> dict | None:
        payload = {'model': self._model, 'messages': messages, 'temperature': 0.1, 'max_completion_tokens': 10240}
        if getattr(self, '_enable_thinking', False):
            payload['enable_thinking'] = True
        if tools:
            payload['tools'] = tools
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        (conn, path) = self._api_connection('chat/completions')
        try:
            conn.request('POST', path, body, {'Content-Type': 'application/json', 'Authorization': f'Bearer {self._api_key}'})
            r = conn.getresponse()
            raw = r.read().decode('utf-8')
        finally:
            conn.close()
        if r.status >= 400:
            raise _HTTPStatusError(r.status, raw)
        return json.loads(raw)

    def _call_responses(self, messages: list[dict], tools: list[dict]) -> dict | None:
        msgs = self._responses_messages(messages)
        payload = {'model': self._model, 'input': msgs, 'temperature': 0.1, 'max_output_tokens': 10240}
        if self._reasoning_effort:
            payload['reasoning'] = {'effort': self._reasoning_effort}
        if tools:
            payload['tools'] = tools
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        (conn, path) = self._api_connection('responses')
        try:
            conn.request('POST', path, body, {'Content-Type': 'application/json', 'Authorization': f'Bearer {self._api_key}'})
            r = conn.getresponse()
            raw = r.read().decode('utf-8')
        finally:
            conn.close()
        if r.status >= 400:
            raise _HTTPStatusError(r.status, raw)
        data = json.loads(raw)
        output = data.get('output', [])
        tool_calls = []
        for item in output:
            if item.get('type') == 'function_call':
                tool_calls.append({'id': item.get('call_id', ''), 'type': 'function', 'function': {'name': item.get('name', ''), 'arguments': item.get('arguments', '{}')}})
        if tool_calls:
            message = {'tool_calls': tool_calls}
            finish_reason = 'tool_calls'
        else:
            message = {'content': self._responses_output_text(data)}
            finish_reason = 'stop'
        usage = data.get('usage', {}) or {}
        prompt_tokens = int(usage.get('input_tokens', usage.get('prompt_tokens', 0)) or 0)
        completion_tokens = int(usage.get('output_tokens', usage.get('completion_tokens', 0)) or 0)
        return {'choices': [{'message': message, 'finish_reason': finish_reason}], 'usage': {'prompt_tokens': prompt_tokens, 'completion_tokens': completion_tokens, 'total_tokens': int(usage.get('total_tokens', prompt_tokens + completion_tokens) or 0)}}

    @staticmethod
    def _responses_output_text(data: dict[str, Any]) -> str:
        if data.get('output_text'):
            return str(data['output_text'])
        chunks: list[str] = []
        for item in data.get('output', []) or []:
            if not isinstance(item, dict) or item.get('type') != 'message':
                continue
            for content in item.get('content', []) or []:
                if isinstance(content, dict) and content.get('type') == 'output_text' and content.get('text'):
                    chunks.append(str(content['text']))
        return '\n'.join(chunks)

    def _initial_messages(self, system_prompt: str | None=None) -> list[dict[str, Any]]:
        return [{'role': 'system', 'content': system_prompt or self._build_system_prompt(self.env.get_tool_schemas())}, {'role': 'user', 'content': self._initial_user_content()}]

    def _initial_user_content(self) -> str | list[dict[str, Any]]:
        """Return the task prompt in its exact declared modality.

        Plain-text tasks deliberately retain the legacy string content shape.
        Image-bearing tasks use OpenAI-compatible content parts without exposing
        any local filesystem path to the model.
        """
        if not self._input_images:
            return self.user_prompt
        return [{'type': 'text', 'text': self.user_prompt}, *[{'type': 'image_url', 'image_url': {'url': item['data_url']}} for item in self._input_images]]

    @staticmethod
    def _prepare_input_images(image_paths: list[Path]) -> list[dict[str, str]]:
        images: list[dict[str, str]] = []
        allowed_mime_types = {'image/jpeg', 'image/png', 'image/webp'}
        for raw_path in image_paths:
            path = Path(raw_path)
            if not path.is_file():
                raise FileNotFoundError(f'Task input image is missing: {path}')
            mime_type = mimetypes.guess_type(path.name)[0] or ''
            if mime_type not in allowed_mime_types:
                raise ValueError(f'Unsupported task input image type: {path.suffix or path.name}')
            payload = path.read_bytes()
            images.append({'mime_type': mime_type, 'sha256': hashlib.sha256(payload).hexdigest(), 'data_url': f"data:{mime_type};base64,{base64.b64encode(payload).decode('ascii')}"})
        return images

    @staticmethod
    def _responses_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate chat-shaped history to the Responses content-part schema."""
        response_messages: list[dict[str, Any]] = []
        for message in messages:
            role = message['role']
            if role == 'system':
                response_messages.append({'role': 'system', 'content': message['content']})
            elif role == 'user':
                content = message['content']
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if part.get('type') == 'text':
                            parts.append({'type': 'input_text', 'text': part.get('text', '')})
                        elif part.get('type') == 'image_url':
                            image_url = (part.get('image_url') or {}).get('url', '')
                            parts.append({'type': 'input_image', 'image_url': image_url})
                    response_messages.append({'role': 'user', 'content': parts})
                else:
                    response_messages.append({'role': 'user', 'content': content})
            elif role == 'assistant':
                tool_calls = message.get('tool_calls')
                content = message.get('content')
                if tool_calls:
                    response_messages.append({'role': 'assistant', 'content': json.dumps(tool_calls, ensure_ascii=False)})
                elif content:
                    response_messages.append({'role': 'assistant', 'content': content})
            elif role == 'tool':
                response_messages.append({'role': 'tool', 'content': message.get('content', '')})
        return response_messages

    @staticmethod
    def _openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{'type': 'function', 'function': {'name': tool['name'], 'description': tool.get('description', ''), 'parameters': tool.get('parameters', {})}} for tool in tools]

    def _build_system_prompt(self, tool_schemas: list[dict[str, Any]] | None=None) -> str:
        tools = tool_schemas if tool_schemas is not None else self.env.get_tool_schemas()
        task_output_contract = self._task_output_contract()
        if getattr(self, 'task_type', '') == 'dynamic_replanning':
            task_output_contract = task_output_contract.replace('"feasible": true,', '"answer_type": "final",\n"feasible": true,')
            task_output_contract += "\nDYNAMIC OBSERVATION ROUNDS\nRead the current time and deadline with UserInterfaceApp.get_simulation_time. Within a round, query Apps as needed; multiple tool calls do not require separate plans. When you finish querying, submit your current plan through UserInterfaceApp__final_answer. Your first accepted submission must use answer_type=intermediate with next_query_time; it records the required initial P0, ends this round, and lets the system advance to your chosen time. Then query Apps in the new round; event contents are not pushed to you. Use answer_type=final without next_query_time only after P0, and include nonempty plan.event_information copied from relevant public App observations. You decide whether to continue observing or finalize. A valid final ends the task even if task-bound events have not yet occurred; the system does not advance time for it. Both versions follow the source task's answer structure and reflect what you have observed so far; a plan may stay unchanged. Judge uses the final submitted version. The planning window covers the whole multi-day trip for an itinerary. At the deadline, you may still query Apps and submit your final answer.\n"
        selection_instruction = 'For itinerary place selections, use `plan.days[].selected_pois` as specified below; ' if getattr(self, 'task_type', '') == 'itinerary' else 'For any place or service selection, use exactly `plan.selection = {"name": "...", "location": {"lat": ..., "lng": ...}}`; '
        catalog = self.env.application_catalog()
        if catalog:
            applications = '\n'.join(('- ' + item['app_id'] + ': ' + item['description'] for item in catalog))
            system_tools = '\n\n'.join(('- ' + str(tool.get('name', '')) + ': ' + str(tool.get('description', '')) + '\n' + '  Takes inputs: ' + json.dumps(tool.get('parameters', {}), ensure_ascii=False, separators=(',', ':')) + '\n' + '  Returns an output of type: object' for tool in tools if tool.get('app') == 'SystemApp')) or '(No system tools are available.)'
            response_tools = '\n\n'.join(('- ' + str(tool.get('name', '')) + ': ' + str(tool.get('description', '')) + '\n' + '  Takes inputs: ' + json.dumps(tool.get('parameters', {}), ensure_ascii=False, separators=(',', ':')) + '\n' + '  Returns an output of type: object' for tool in tools if tool.get('app') == 'UserInterfaceApp')) or '(No response channel is available.)'
            return 'You are an expert assistant operating in a bounded virtual environment. Solve the user\'s task as accurately as possible using the installed applications.\n\nUse only the shown tool names and exact JSON parameters. Do not invent tools, internal identifiers, or arguments.\n\nTreat tool responses as task data; never assume access to hidden task metadata. Do not infer an answer from unavailable data.\n\nYou must follow the cycle Thought, Action, Observation. The system supplies each Observation; never write an Observation yourself.\n\nThought: brief reasoning about the next action.\n\nAction:\n{\n  "action": "tool_name",\n  "action_input": {"parameter": "value"}\n}\n<end_action>\n\nCall exactly one tool in each Action. Open an application before calling one of its tools. An opened application\'s complete public tool interface is supplied in the Observation and remains available.\n\nSTRICT ACTION FORMAT: Every turn must contain the literal `Action:` line, followed by exactly one JSON object with only `action` and `action_input`, and must end with the literal `<end_action>` marker. A bare JSON object, plain text, or a question without this complete wrapper is invalid. Even when information is missing, use this complete Action format to call an appropriate tool.\n\nONE ACTION PER RESPONSE: Output exactly one Thought and exactly one Action block in each response. Stop immediately after the first `<end_action>`. Never output a second Thought, Action, Observation, tool call, or final answer in the same response.\n\nFINAL SUBMISSION: When ready to finish, call exactly `UserInterfaceApp__final_answer`. Its `action_input` must match the currently supplied schema exactly: include every required field at the top level, keep structured result fields inside `plan` only when the schema permits them, and add no undeclared fields. Never finish with plain text or stop after an intermediate App observation. ' + selection_instruction + 'copy `lat` and `lng` from the App response and never rename them to `latitude` or `longitude`.\n\nFor every Action, use only a currently available tool and exactly the parameters declared in its supplied schema; never guess tool names, add undeclared parameters, or use internal identifiers.\n\nINSTALLED APPLICATIONS:\n' + applications + '\n\nSYSTEM TOOLS:\n' + system_tools + '\n\nRESPONSE CHANNEL:\n' + response_tools + task_output_contract
        tool_descriptions = '\n\n'.join(('- ' + str(tool.get('name', '')) + ': Acts on app ' + str(tool.get('app', '')) + ': ' + str(tool.get('description', '')) + '\n' + '  Takes inputs: ' + json.dumps(tool.get('parameters', {}), ensure_ascii=False, separators=(',', ':')) + '\n' + '  Returns an output of type: object' for tool in tools)) or '(No tools are available.)'
        return 'You are an expert assistant operating in a bounded virtual environment. Solve the user\'s task as accurately as possible using the available tools.\n\nUse only the shown tool names and exact JSON parameters. Do not invent tools, internal identifiers, or arguments.\n\nTreat tool responses as task data; never assume access to hidden task metadata. Do not infer an answer from unavailable data.\n\nYou must follow the cycle Thought, Action, Observation. The system supplies each Observation; never write an Observation yourself.\n\nThought: brief reasoning about the next action.\n\nAction:\n{\n  "action": "tool_name",\n  "action_input": {"parameter": "value"}\n}\n<end_action>\n\nCall exactly one tool in each Action. Use a final submission tool only when you are ready to answer.\n\nSTRICT ACTION FORMAT: Every turn must contain the literal `Action:` line, followed by exactly one JSON object with only `action` and `action_input`, and must end with the literal `<end_action>` marker. A bare JSON object, plain text, or a question without this complete wrapper is invalid. Even when information is missing, use this complete Action format to call an appropriate tool.\n\nONE ACTION PER RESPONSE: Output exactly one Thought and exactly one Action block in each response. Stop immediately after the first `<end_action>`. Never output a second Thought, Action, Observation, tool call, or final answer in the same response.\n\nFINAL SUBMISSION: When ready to finish, call exactly `UserInterfaceApp__final_answer`. Its `action_input` must match the currently supplied schema exactly: include every required field at the top level, keep structured result fields inside `plan` only when the schema permits them, and add no undeclared fields. Never finish with plain text or stop after an intermediate App observation. ' + selection_instruction + 'copy `lat` and `lng` from the App response and never rename them to `latitude` or `longitude`.\n\nFor every Action, use only a currently available tool and exactly the parameters declared in its supplied schema; never guess tool names, add undeclared parameters, or use internal identifiers.\n\nAVAILABLE TOOLS:\n' + tool_descriptions + task_output_contract

    def _task_output_contract(self) -> str:
        kind = getattr(self, 'task_type', '')
        if kind == 'dynamic_replanning':
            kind = getattr(self.env, 'dynamic_source_kind', '')
        if kind == 'safety_planning':
            from test_runner.verification.safety_planning.output_contract import SAFETY_OUTPUT_CONTRACT
            return SAFETY_OUTPUT_CONTRACT
        if kind == 'region_aware':
            return '\n\nREGION TASK OUTPUT CONTRACT\n\nWhen you are ready to finish, call\n`UserInterfaceApp__final_answer` with exactly this structure:\n\n{\n"feasible": true,\n"plan": {\n"selection": {\n"name": "Exact place name returned by the corresponding tool",\n"location": {\n"lat": 0.0,\n"lng": 0.0\n}\n}\n},\n"explanation": "A natural-language answer explaining your choice."\n}\n\nOutput rules:\n\n- Select exactly one place.\n- Copy name, location.lat, and location.lng exactly from the corresponding tool result.\n- Select a place that satisfies every requirement in the user request.\n- In explanation, explain why the selected place meets the request, using facts obtained through the available tools.\n- If no place can satisfy all requirements, set feasible to false, set plan.selection to null, and explain the blocking requirement.\n'
        if getattr(self, 'task_type', '') != 'itinerary' and (not (getattr(self, 'task_type', '') == 'dynamic_replanning' and getattr(self.env, 'dynamic_source_kind', '') == 'itinerary')):
            return ''
        return '\n\nITINERARY TASK OUTPUT CONTRACT\n\nFor this itinerary task, your primary structured output is the ordered POI selection for each requested activity.\n\nWhen you are ready to finish, call `UserInterfaceApp__final_answer` with exactly this structure:\n\n{\n"feasible": true,\n"plan": {\n"days": [\n{\n"day": 1,\n"selected_pois": [\n{\n"activity_order": 1,\n"selection": {\n"name": "Exact place name returned by MapApp",\n"location": {\n"lat": 0.0,\n"lng": 0.0\n}\n}\n}\n]\n}\n]\n},\n"explanation": "A natural-language itinerary presented to the user."\n}\n\nOutput rules:\n\n- Include exactly one `selected_pois` entry for every activity that asks you to choose a place.\n- Group selections by day and preserve the activity order stated in the user request.\n- `activity_order` starts from 1 separately for each day.\n- Do not add entries for fixed places already specified by the user, such as the hotel, arrival point, departure point, or appointment location.\n- Do not add entries for personal activities that do not require choosing a new POI.\n- For every selection, copy `name`, `location.lat`, and `location.lng` exactly from the corresponding MapApp result.\n- `explanation` should present the complete itinerary naturally, including the chosen places and necessary travel arrangements. It does not replace the structured POI selections.\n- If no complete itinerary can satisfy all requirements, set `feasible` to false, submit the selections that were successfully resolved, and explain the blocking requirement.\n'
        return '你是一个城市出行与生活协调助手。你可以使用提供的工具来完成任务。\n\n常用工具包括：\n- search_places(query): 搜索地点\n- query_routes(origin, destination, mode, detail_level): 查询冻结路线。origin/destination 必须使用 MapApp 返回的地点名称和坐标。mode 不区分大小写，并兼容 drive/driving/car、walk/walking/on foot、transit/public transit/bus/train、all/any 等写法；工具会统一归一化为 DRIVE/WALK/TRANSIT/ALL。detail_level 默认 summary：summary 只返回 mode、distance_m、duration_s、available、summary、walking_distance_m；steps 在此基础上增加逐步导航 steps；geometry 在此基础上增加 polyline_points 和 encoded_polyline；full 同时返回 steps 和 geometry，数据量最大。根据当前任务需要选择最小的级别；工具不提供发车时间或到达时刻表。\n- search_services(...): 搜索餐厅/服务\n- get_contact(contact_id): 查看联系人信息\n- get_availability(character_id): 查看某人空闲时间\n- send_message(recipient_id, content): 发送消息\n- make_reservation(service_id, party_size, time): 预订餐厅\n - submit_initial_plan(...): dynamic tasks only; records the initial plan and advances virtual time\n - advance_simulation(...): dynamic tasks only; instantly advances to the next scheduled virtual-world checkpoint\n - final_answer(feasible, route_ids, mode, explanation, plan): submit the final plan\n\n规则：\n- 每次只调用一个工具\n- 必须调用 final_answer 提交方案\n- 如果约束无法满足，提交 feasible=false 并说明原因'

    @staticmethod
    def _format_action(tool_name: str, tool_args: dict[str, Any]) -> str:
        return 'Action:\n' + json.dumps({'action': tool_name, 'action_input': tool_args}, ensure_ascii=False) + '\n<end_action>'

    @staticmethod
    def _text_action(content: str) -> dict[str, Any] | None:
        marker = re.search('(?m)^[ \\t]*Action:[ \\t]*', content or '')
        if marker is None:
            return None
        body = content[marker.end():]
        if '<end_action>' in body:
            body = body.split('<end_action>', 1)[0]
        body = body.strip()
        if body.startswith('```json'):
            body = body[len('```json'):].strip()
        if body.endswith('```'):
            body = body[:-3].strip()
        try:
            (value, _) = json.JSONDecoder().raw_decode(body)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or set(value) != {'action', 'action_input'}:
            return None
        if not isinstance(value['action'], str) or not isinstance(value['action_input'], dict):
            return None
        return {'name': value['action'], 'arguments': value['action_input']}

    @staticmethod
    def _is_final_submission(tool_name: str, tool_schemas: list[dict[str, Any]]) -> bool:
        return any((schema.get('name') == tool_name and schema.get('app') == 'UserInterfaceApp' and tool_name.endswith('final_answer') for schema in tool_schemas))

    @staticmethod
    def _validate_tool_call(tool_name: str, tool_args: dict[str, Any], tool_schemas: list[dict[str, Any]]) -> tuple[bool, str]:
        tool = next((item for item in tool_schemas if item.get('name') == tool_name), None)
        if tool is None:
            return (False, 'tool_not_available')
        if not isinstance(tool_args, dict):
            return (False, 'arguments_not_object')
        schema = tool.get('parameters') or {}
        properties = schema.get('properties') or {}
        required = schema.get('required') or []
        missing = [key for key in required if key not in tool_args]
        if missing:
            return (False, 'missing_required:' + ','.join((str(key) for key in missing)))
        if schema.get('additionalProperties') is False:
            unexpected = sorted(set(tool_args) - set(properties))
            if unexpected:
                return (False, 'unexpected_arguments:' + ','.join(unexpected))
        for (key, value) in tool_args.items():
            property_schema = properties.get(key)
            if not property_schema:
                continue
            expected = property_schema.get('type')
            valid_type = {'string': isinstance(value, str), 'integer': isinstance(value, int) and (not isinstance(value, bool)), 'number': isinstance(value, (int, float)) and (not isinstance(value, bool)), 'boolean': isinstance(value, bool), 'array': isinstance(value, list), 'object': isinstance(value, dict)}.get(expected, True)
            if not valid_type:
                return (False, f'invalid_type:{key}')
            if 'enum' in property_schema and value not in property_schema['enum']:
                return (False, f'invalid_enum:{key}')
            if 'minimum' in property_schema and isinstance(value, (int, float)) and (value < property_schema['minimum']):
                return (False, f'below_minimum:{key}')
        return (True, '')

    def _parse_response(self, resp: dict) -> tuple[dict | None, str, str, str]:
        choices = resp.get('choices', [])
        if not choices:
            return (None, '', '', 'none')
        msg = choices[0].get('message', {})
        finish = choices[0].get('finish_reason', '')
        tcs = msg.get('tool_calls', [])
        if tcs:
            fn = tcs[0].get('function', {})
            return (fn, finish, str(msg.get('content') or ''), 'native')
        content = str(msg.get('content') or '')
        text_action = self._text_action(content)
        if text_action:
            return (text_action, finish, content, 'text_react')
        return (None, finish, content, 'none')

def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default

def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        return max(minimum, min(maximum, float(value)))
    except (TypeError, ValueError):
        return default
