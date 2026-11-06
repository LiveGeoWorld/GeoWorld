import json

from test_runner.agent.llm_agent import LLMAgent
from test_runner.agent.test_react_protocol import _agent_with_response
from test_runner.runner import _result_to_dict
from app.apps.base import BaseApp


class Echo(BaseApp):
    name = 'Echo'
    def get_tool_schemas(self):
        return [{'name': 'echo', 'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}]
    def echo(self):
        return {'ok': True}


def reply(content):
    return {'choices': [{'message': {'content': content}}]}


def agent(sequence):
    result = _agent_with_response({})
    result.max_steps = 20
    result._max_protocol_retries = 2
    result.env.register_app(Echo())
    iterator = iter(sequence)
    result._call_llm = lambda *a: next(iterator)
    return result


ECHO = reply('Action: {"action":"echo","action_input":{}}<end_action>')
SUBMIT = reply('Action: {"action":"submit","action_input":{"content":"done"}}<end_action>')
BAD = reply('Please clarify')


def test_each_executed_call_starts_new_two_correction_allowance():
    result = agent([BAD, BAD, ECHO, BAD, BAD, SUBMIT]).run()
    assert len(result.trace) == 2
    errors = result.agent_protocol['errors']
    assert [e['retry_allowed'] for e in errors] == [True] * 4
    assert [e['call_round'] for e in errors] == [1, 1, 2, 2]
    assert result.agent_protocol['model_steps'] == 6
    assert result.recovery_checkpoint['round_retries'] == 0


def test_resume_preserves_current_round_not_total_errors():
    first = agent([BAD, BAD, ECHO, BAD, None]).run()
    continuation = agent([BAD, BAD])
    result = continuation.run(recovery=_result_to_dict(first))
    assert len(result.trace) == 1
    assert result.termination['category'] == 'no_action'
    assert [e['retry_allowed'] for e in result.agent_protocol['errors']] == [True, True, True, True, False]
    assert result.agent_protocol['model_steps'] == 6


def test_successful_request_retains_empty_retry_audit(monkeypatch):
    instance = agent([])
    instance._provider = 'chat'
    empty = {**reply(''), 'usage': {'prompt_tokens': 7, 'completion_tokens': 2, 'total_tokens': 9}}
    responses = iter([empty, SUBMIT])
    instance._call_chat = lambda *a: next(responses)
    instance._call_llm = LLMAgent._call_llm.__get__(instance)
    monkeypatch.setattr('test_runner.agent.llm_agent.time.sleep', lambda *a: None)
    events = []
    result = instance.run(checkpoint_sink=lambda value: events.append(json.loads(json.dumps(value))))
    assert any(e['phase'] == 'api_attempt' and e['attempt']['status'] == 'retry_scheduled' for e in events)
    attempts = result.recovery_checkpoint['api_requests'][0]['attempts']
    assert any(a['status'] == 'empty_response' for a in attempts)
    assert attempts[-1]['status'] == 'success'
    assert all(a.get('started_at') and a.get('recorded_at') for a in attempts)
    assert result.recovery_checkpoint['model_turns'][0]['api_attempts'] == attempts
    assert result.tokens_used['total'] == 9


def test_append_only_audit_has_unique_identity_and_times(tmp_path):
    from test_runner.recovery_audit import event
    first = event(tmp_path, 'recovery_started', attempt_id='one')
    second = event(tmp_path, 'recovery_finished', attempt_id='one')
    assert first['event_id'] != second['event_id']
    assert len(list(tmp_path.glob('*.json'))) == 2
    assert first['time_utc'] <= second['time_utc']
