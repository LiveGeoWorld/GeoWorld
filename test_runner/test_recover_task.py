import pytest

from test_runner.agent.test_react_protocol import _agent_with_response
from test_runner.recover_task import restore_agent, verify_binding
from test_runner.runner import _result_to_dict


def row(agent, category='no_action'):
    return {'termination': {'category': category}, 'trace': [],
            'recovery_input': {'system_prompt': 'Original public system'},
            'agent_protocol': {'step': 1, 'response': 'Can you clarify?'},
            'tokens_used': {'prompt': 20, 'completion': 5, 'total': 25},
            'duration_s': 9, 'model_name': agent._model}


def test_zero_call_recovery_keeps_budget_and_appends_actual_error():
    agent = _agent_with_response({})
    agent.max_steps = 4
    agent._max_protocol_retries = 2
    seen = []
    def call(messages, tools):
        seen.extend(messages)
        return {'choices': [{'message': {'content': 'Action: {"action":"submit","action_input":{"content":"done"}}<end_action>'}}],
                'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}}
    agent._call_llm = call
    result = agent.run(recovery=row(agent))
    assert result.tokens_used['total'] == 30
    assert result.duration_s >= 9
    assert result.agent_protocol['model_steps'] == 2
    assert len(result.agent_protocol['errors']) == 1
    assert seen[0]['content'] == 'Original public system'
    assert seen[-2]['content'] == 'Can you clarify?'
    assert 'call_rejected' in seen[-1]['content']


def test_repeated_invalid_response_stops_after_two_feedbacks():
    agent = _agent_with_response({'choices': [{'message': {'content': 'Please clarify'}}]})
    agent.max_steps = 10
    agent._max_protocol_retries = 2
    result = agent.run(recovery=row(agent))
    assert result.termination['category'] == 'no_action'
    assert [e['retry_allowed'] for e in result.agent_protocol['errors']] == [True, True, False]
    assert result.agent_protocol['model_steps'] == 3


def test_missing_original_prompt_is_not_replaced_by_another_task():
    agent = _agent_with_response({})
    saved = row(agent)
    saved.pop('recovery_input')
    with pytest.raises(ValueError, match='Missing original system'):
        restore_agent(agent, saved, [{'role': 'system', 'content': 'different'}], 'different')


def test_frozen_hash_mismatch_refuses_recovery():
    saved = {'task_id': 't', 'user_prompt': 'q', 'evaluation_world_snapshot':
             {'world_id': 'w', 'city_versions': {'c': 1}, 'world_hashes': {'c': 'old'}}}
    with pytest.raises(ValueError, match='world_hashes'):
        verify_binding(saved, {'task_id': 't', 'user_prompt': 'q'},
                       {'world_id': 'w', 'city_versions': {'c': 1}, 'world_hashes': {'c': 'new'}}, 'shared_world')


@pytest.mark.parametrize('with_raw', [False, True])
def test_replay_restores_action_and_public_observation(with_raw):
    agent = _agent_with_response({})
    saved = row(agent)
    record = {'seq': 1, 'timestamp': 'original', 'tool': 'echo', 'args': {'value': 7},
              'duration_ms': 3, 'result': {'value': 7},
              'llm_input': {'system_prompt': 'Original public system', 'user_prompt': agent.user_prompt},
              'llm_output': {'response': 'Action: echo'}}
    if with_raw:
        record['raw_result'] = {'value': 7}
    saved['trace'] = [record]
    saved['agent_protocol']['step'] = 2
    calls = []
    def replay(tool, args):
        calls.append((tool, args))
        return {'value': args['value']}
    agent.env.execute_tool = replay
    restored = restore_agent(agent, saved, [{'role': 'system', 'content': 'new'},
                                            {'role': 'user', 'content': agent.user_prompt}], 'new')
    assert calls == [('echo', {'value': 7})]
    assert restored['start_step'] == 2
    assert restored['messages'][-1]['content'] == 'Observation:\n{"value": 7}'
    assert agent.env.trace[0].timestamp == 'original'
    assert len(restored['observation_context']) == 1


def test_previous_corrections_are_not_silently_reset():
    agent = _agent_with_response({})
    saved = row(agent)
    saved['agent_protocol']['errors'] = [{'retry_allowed': True}]
    with pytest.raises(ValueError, match='saved full conversation'):
        restore_agent(agent, saved, [], '')


def _reservation_replay_case(monkeypatch):
    import app.apps.local_service as module
    app = module.LocalServiceApp()
    app._public_services = [{'name': 'Cafe', 'location': {'lat': 1, 'lng': 2}}]
    args = {'name': 'Cafe', 'location': {'lat': 1, 'lng': 2}, 'party_size': 2, 'time': '10:30'}
    # Reproduce two process-salted hashes without depending on random test seeds.
    monkeypatch.setattr(module, 'hash', lambda key: 123456, raising=False)
    original = app.make_reservation(**args)
    replay_app = module.LocalServiceApp()
    replay_app._public_services = app._public_services
    monkeypatch.setattr(module, 'hash', lambda key: 654321)
    agent = _agent_with_response({})
    agent.env.register_app(replay_app)
    agent.env.configure_public_tools({'LocalServiceApp': ['make_reservation', 'cancel_reservation']})
    saved = row(agent, 'model_api')
    saved['recovery_input']['initial_messages'] = [
        {'role': 'system', 'content': 'Original public system'},
        {'role': 'user', 'content': agent.user_prompt},
    ]
    saved['agent_protocol']['step'] = 2
    saved['trace'] = [{'seq': 1, 'timestamp': 'original',
                       'tool': 'LocalServiceApp__make_reservation', 'args': args,
                       'duration_ms': 1, 'result': original, 'raw_result': original,
                       'llm_input': {'system_prompt': 'Original public system', 'user_prompt': agent.user_prompt},
                       'llm_output': {'response': 'Action: original reservation'}}]
    return agent, saved, replay_app, args


def test_reservation_replay_preserves_issued_id_and_later_calls(monkeypatch):
    agent, saved, app, args = _reservation_replay_case(monkeypatch)
    restored = restore_agent(agent, saved, [], '')
    assert restored['model_steps'] == 1
    assert 'reservation_123456' in restored['messages'][-1]['content']
    assert app.make_reservation(**args)['reservation_id'] == 'reservation_123456'
    assert app.cancel_reservation('reservation_123456')['reservation_id'] == 'reservation_123456'


@pytest.mark.parametrize('changed', [
    {'status': 'failed'}, {'service_name': 'Different cafe'}, {'time': '11:30'},
    {'party_size': 8}, {'reservation_id': 'invalid'}, {'unexpected': True},
])
def test_reservation_replay_rejects_changed_facts_or_invalid_id(monkeypatch, changed):
    agent, saved, app, args = _reservation_replay_case(monkeypatch)
    saved['trace'][0]['raw_result'] = {**saved['trace'][0]['raw_result'], **changed}
    with pytest.raises(ValueError, match='Replay raw observation mismatch'):
        restore_agent(agent, saved, [], '')
    assert app.make_reservation(**args)['reservation_id'] == 'reservation_654321'


def test_reservation_replay_rejects_conflicting_ids_for_same_booking(monkeypatch):
    import copy
    agent, saved, app, args = _reservation_replay_case(monkeypatch)
    second = copy.deepcopy(saved['trace'][0])
    second['seq'] = 2
    second['raw_result']['reservation_id'] = 'reservation_111111'
    saved['trace'].append(second)
    saved['agent_protocol']['step'] = 3
    with pytest.raises(ValueError, match='Replay raw observation mismatch at 2'):
        restore_agent(agent, saved, [], '')


def test_zero_call_checkpoint_saves_initial_request_and_all_failed_turns():
    agent = _agent_with_response({'choices': [{'message': {'content': 'Please clarify'}}]})
    agent.max_steps = 10
    agent._max_protocol_retries = 2
    result = agent.run()
    checkpoint = result.recovery_checkpoint
    assert len(checkpoint['initial_messages']) == 2
    assert len(checkpoint['model_turns']) == 3
    assert all('feedback' in turn for turn in checkpoint['model_turns'][:2])
    assert result.trace == []
    assert result.grade == 'FAIL'
    assert _result_to_dict(result)['recovery_checkpoint'] == checkpoint


def test_resume_exhausted_feedback_does_not_call_model_or_reset_budget():
    agent = _agent_with_response({'choices': [{'message': {'content': 'Please clarify'}}]})
    agent.max_steps = 10
    agent._max_protocol_retries = 2
    first = agent.run()
    saved = _result_to_dict(first)
    continuation = _agent_with_response({})
    continuation.max_steps = 10
    continuation._max_protocol_retries = 2
    def forbidden(*args):
        raise AssertionError('Exhausted recovery must not call model')
    continuation._call_llm = forbidden
    result = continuation.run(recovery=saved)
    assert result.grade == 'FAIL'
    assert result.agent_protocol['model_steps'] == 3
    assert sum(e['retry_allowed'] for e in result.agent_protocol['errors']) == 2


def test_checkpoint_after_one_feedback_preserves_conversation_and_used_opportunity():
    response = {'choices': [{'message': {'content': 'Please clarify'}}]}
    agent = _agent_with_response(response)
    agent.max_steps = 10
    agent._max_protocol_retries = 2
    count = 0
    def call(*args):
        nonlocal count
        count += 1
        return response if count == 1 else None
    agent._call_llm = call
    saved = _result_to_dict(agent.run())
    continuation = _agent_with_response(response)
    continuation.max_steps = 10
    continuation._max_protocol_retries = 2
    seen = []
    def still_wrong(messages, tools):
        seen.append(list(messages))
        return response
    continuation._call_llm = still_wrong
    result = continuation.run(recovery=saved)
    assert len(seen) == 2
    assert 'call_rejected' in seen[0][-1]['content']
    assert sum(e['retry_allowed'] for e in result.agent_protocol['errors']) == 2
    assert result.agent_protocol['model_steps'] == 3


def test_historical_prompt_builder_reconstructs_without_copying_reference(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    from test_runner.recover_task import reconstruct_initial_request
    project = Path(__file__).resolve().parents[1]
    repository = tmp_path / 'history'
    repository.mkdir()
    files = {
        'test_runner/agent/llm_agent.py': (project / 'test_runner/agent/llm_agent.py').read_text(encoding='utf-8').replace(
            'test_runner.verification.safety_planning.output_contract',
            'task_module.task_types.safety_planning.output_contract'),
        'task_module/task_types/safety_planning/output_contract.py':
            (project / 'test_runner/verification/safety_planning/output_contract.py').read_text(encoding='utf-8'),
    }
    for relative, content in files.items():
        target = repository / relative
        if os.name == 'nt':
            target = Path('\\\\?\\' + str(target.resolve()))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
    def git(*args):
        return subprocess.check_output(['git', '-c', 'core.longpaths=true', *args], cwd=repository, text=True).strip()
    git('init', '-q')
    git('add', '.')
    git('-c', 'user.name=Test Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'Archived prompt fixture')
    revision = git('rev-parse', 'HEAD')
    agent = _agent_with_response({})
    agent.task_type = 'route_planning'
    system = agent._build_system_prompt(agent.env.get_tool_schemas())
    restored = reconstruct_initial_request(agent, revision, [system], repository=repository)
    assert restored['system_prompt'] == system
    assert restored['initial_messages'][1]['content'] == agent.user_prompt
    assert restored['provenance']['revision'] == revision
    with pytest.raises(ValueError, match='does not match archived'):
        reconstruct_initial_request(agent, revision, ['different task prompt'], repository=repository)


def test_old_empty_response_retries_original_request_without_format_feedback():
    agent = _agent_with_response({})
    agent.max_steps = 10
    agent._max_protocol_retries = 2
    saved = row(agent)
    saved['agent_protocol']['response'] = ''
    requests = []
    def call(messages, tools):
        requests.append(list(messages))
        return {'choices': [{'message': {'content': 'Action: {"action":"submit","action_input":{"content":"done"}}<end_action>'}}]}
    agent._call_llm = call
    result = agent.run(recovery=saved)
    assert len(requests[0]) == 2
    assert result.agent_protocol['errors'] == []
    assert result.agent_protocol['model_steps'] == 1


def test_legacy_dynamic_binding_uses_original_provenance():
    binding = {'world_id': 'w', 'city_versions': {'c': 1}, 'world_hashes': {'c': 'old'}}
    saved = {'task_id': 't', 'user_prompt': 'q', 'evaluation_world_snapshot': {'dynamic_events': {}}}
    provenance = {'world_id': 'w', 'generation_context': {'world_city_versions': {'c': 'v1'},
                   'world_map_snapshot': {'c': {'sha256': 'old'}}}}
    verify_binding(saved, {'task_id': 't', 'user_prompt': 'q'}, binding, 'shared_world', provenance)
    with pytest.raises(ValueError, match='world_hashes'):
        verify_binding(saved, {'task_id': 't', 'user_prompt': 'q'}, {**binding, 'world_hashes': {'c': 'new'}},
                       'shared_world', provenance)


def test_known_legacy_feedback_audit_reconstructs_used_opportunities():
    agent = _agent_with_response({})
    agent.max_steps = 10
    agent._max_protocol_retries = 2
    saved = row(agent)
    saved['agent_protocol'].update(step=3, policy='bounded_call_feedback_v1',
        errors=[{'step': 1, 'response': 'first bad', 'error': 'unparseable_action', 'retry_allowed': True},
                {'step': 2, 'response': 'second bad', 'error': 'unparseable_action', 'retry_allowed': True},
                {'step': 3, 'response': 'third bad', 'error': 'unparseable_action', 'retry_allowed': False}])
    def forbidden(*args):
        raise AssertionError('Two existing feedbacks must remain exhausted')
    agent._call_llm = forbidden
    result = agent.run(recovery=saved)
    assert result.grade == 'FAIL'
    assert result.agent_protocol['model_steps'] == 3
    assert sum(e['retry_allowed'] for e in result.agent_protocol['errors']) == 2
    assert result.recovery_checkpoint['messages'][2]['content'] == 'first bad'
    assert result.recovery_checkpoint['messages'][4]['content'] == 'second bad'
