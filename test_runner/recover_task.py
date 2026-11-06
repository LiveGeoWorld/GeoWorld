"""Explicit single-task continuation; never overwrite an original evaluation.

Replay executes saved actions only to reconstruct the sandbox. Saved public
observations, not today's replacements, form the continued model conversation.
"""
from __future__ import annotations
import argparse
import copy
import json
import hashlib
import ast
import subprocess
import types
from pathlib import Path
from typing import Any

def verify_binding(row, task, binding, mode, provenance=None):
    if row.get('task_id') != task.get('task_id') or row.get('user_prompt') != task.get('user_prompt'):
        raise ValueError('Recovery task identity/prompt mismatch')
    if row.get('dynamic_event_mode', 'shared_world') != mode:
        raise ValueError('Recovery event mode mismatch')
    snapshot = row.get('evaluation_world_snapshot') or {}
    if 'world_hashes' not in snapshot and provenance:
        context = provenance.get('generation_context') or provenance
        frozen = context.get('world_map_snapshot') or {}
        snapshot = {'world_id': provenance.get('world_id'), 'city_versions': {k: int(str(v).lstrip('vV')) for (k, v) in (context.get('world_city_versions') or {}).items()}, 'world_hashes': {k: v.get('sha256') for (k, v) in frozen.items()}}
    for field in ('world_id', 'city_versions', 'world_hashes'):
        if field not in snapshot or snapshot[field] != binding.get(field):
            raise ValueError('Recovery frozen binding mismatch: ' + field)

def reconstruct_initial_request(agent, revision, archived_prompts, repository=None):
    """Build from local historical code, using archived text only as validation.

    Never copy a reference task prompt. Exact comparison validates the template
    and the initial public interface; the user request/media remain this task's.
    """
    repository = Path(repository or Path(__file__).resolve().parents[1])
    commit = subprocess.check_output(['git', 'rev-parse', '--verify', revision + '^{commit}'], cwd=repository, text=True).strip()
    source = subprocess.check_output(['git', 'show', commit + ':test_runner/agent/llm_agent.py'], cwd=repository).decode('utf-8')
    tree = ast.parse(source)
    klass = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'LLMAgent'))
    names = {'_build_system_prompt', '_task_output_contract'}
    methods = [n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name in names]
    if {n.name for n in methods} != names:
        raise ValueError('Historical prompt builder is incomplete')
    output_path = 'task_module/task_types/safety_planning/output_contract.py'
    output_source = subprocess.check_output(['git', 'show', commit + ':' + output_path], cwd=repository).decode('utf-8')
    output_tree = ast.parse(output_source)
    safety_text = next((ast.literal_eval(n.value) for n in output_tree.body if isinstance(n, ast.Assign) and any((isinstance(t, ast.Name) and t.id == 'SAFETY_OUTPUT_CONTRACT' for t in n.targets))))

    class HistoricalImports(ast.NodeTransformer):

        def visit_ImportFrom(self, node):
            if node.module == 'task_module.task_types.safety_planning.output_contract':
                return ast.copy_location(ast.Assign(targets=[ast.Name(id='SAFETY_OUTPUT_CONTRACT', ctx=ast.Store())], value=ast.Constant(value=safety_text)), node)
            raise ValueError('Unsupported historical prompt dependency: ' + str(node.module))
    methods = [ast.fix_missing_locations(HistoricalImports().visit(n)) for n in methods]
    namespace = {'json': json, 'Any': Any}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(repository / 'historical_prompt'), 'exec'), namespace)
    builder = types.SimpleNamespace(env=agent.env, task_type=agent.task_type)
    builder._task_output_contract = types.MethodType(namespace['_task_output_contract'], builder)
    initial_tools = agent.env.get_tool_schemas()
    interface_sources = {}
    if agent.task_type == 'dynamic_replanning' and agent.env.application_catalog():

        def schema_class(path, class_name, base):
            code = subprocess.check_output(['git', 'show', commit + ':' + path], cwd=repository).decode('utf-8')
            interface_sources[path] = hashlib.sha256(code.encode()).hexdigest()
            parsed = ast.parse(code)
            node = next((n for n in parsed.body if isinstance(n, ast.ClassDef) and n.name == class_name))
            body = [n for n in node.body if isinstance(n, ast.Assign) or (isinstance(n, ast.FunctionDef) and n.name == 'get_tool_schemas')]
            isolated = ast.ClassDef(name=class_name, bases=[ast.Name(id='HistoricalBase', ctx=ast.Load())], keywords=[], body=body, decorator_list=[])
            scope = {'Any': Any, 'HistoricalBase': base}
            exec(compile(ast.fix_missing_locations(ast.Module(body=[isolated], type_ignores=[])), path, 'exec'), scope)
            return scope[class_name]
        static_class = schema_class('test_runner/sandbox/user_interface.py', 'UserInterfaceApp', object)
        dynamic_class = schema_class('test_runner/sandbox/dynamic_user_interface.py', 'DynamicUserInterfaceApp', static_class)
        historical_schema = object.__new__(dynamic_class).get_tool_schemas()
        response = next((s for s in historical_schema if s['name'] == 'final_answer'))
        response = {k: v for (k, v) in response.items() if k not in {'function_name', 'app'}}
        response.update(name='UserInterfaceApp__final_answer', app='UserInterfaceApp')
        initial_tools = [response if s['name'] == 'UserInterfaceApp__final_answer' else s for s in initial_tools]
    system = namespace['_build_system_prompt'](builder, initial_tools)
    if system not in archived_prompts:
        raise ValueError('Historical prompt reconstruction does not match archived public-interface evidence')
    tools_hash = hashlib.sha256(json.dumps(initial_tools, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    return {'system_prompt': system, 'initial_messages': agent._initial_messages(system), 'provenance': {'method': 'historical_builder_exact_archive_validation', 'revision': commit, 'prompt_source_sha256': hashlib.sha256(source.encode()).hexdigest(), 'system_sha256': hashlib.sha256(system.encode()).hexdigest(), 'output_contract_source_sha256': hashlib.sha256(output_source.encode()).hexdigest(), 'historical_interface_sources': interface_sources, 'initial_tools_sha256': tools_hash}}

def restore_agent(agent, row, initial_messages, generated_system):
    from test_runner.runner import _summarize_result
    category = (row.get('termination') or {}).get('category')
    if category not in {'no_action', 'invalid_action', 'model_api'}:
        raise ValueError('Not a recoverable failed-call record')
    protocol = row.get('agent_protocol') or {}
    traces = row.get('trace') or []
    checkpoint = row.get('recovery_checkpoint') or {}
    empty_legacy_response = not checkpoint and category == 'no_action' and (not str(protocol.get('response') or '').strip())
    if checkpoint and checkpoint.get('schema_version') != 'geoword.agent_checkpoint.v1':
        raise ValueError('Unsupported recovery checkpoint')
    if traces and (traces[0].get('llm_input') or {}).get('user_prompt') != agent.user_prompt:
        raise ValueError('Recovery original user prompt mismatch')
    recovery_input = row.get('recovery_input') or {}
    if not checkpoint and (not traces) and recovery_input.get('historical_revision'):
        recovery_input = reconstruct_initial_request(agent, recovery_input['historical_revision'], recovery_input.get('archived_prompt_evidence') or [])
    initial_saved = checkpoint.get('initial_messages') or recovery_input.get('initial_messages')
    if initial_saved:
        system = initial_saved[0]['content']
    elif traces:
        system = (traces[0].get('llm_input') or {}).get('system_prompt')
    else:
        system = recovery_input.get('system_prompt')
    if not system:
        raise ValueError('Missing original system prompt; provide verified recovery_input')
    legacy_errors = protocol.get('errors') or []
    if legacy_errors and (not checkpoint) and (protocol.get('policy') not in {'bounded_call_feedback_v1', 'bounded_call_feedback_v2_per_round'}):
        raise ValueError('Previously corrected record requires saved full conversation or known feedback policy')
    if row.get('input_asset_hashes', []) != [i['sha256'] for i in agent._input_images]:
        raise ValueError('Recovery input media mismatch')
    if row.get('model_name') and row['model_name'] != agent._model:
        raise ValueError('Recovery model mismatch')
    messages = copy.deepcopy(initial_saved or initial_messages)
    messages[0]['content'] = system
    original_messages = copy.deepcopy(messages)
    reconstructed = []
    observation_context = []
    for record in traces:
        output = record.get('llm_output') or {}
        if 'response' not in output:
            raise ValueError('Missing saved assistant response')
        actual = agent.env.execute_tool(record['tool'], record.get('args') or {})
        if agent.env.terminal_state is not None:
            raise ValueError('Replay terminated before saved failure')
        if 'raw_result' in record:
            observation = record['raw_result']
            if actual != observation and record['tool'] == 'LocalServiceApp__make_reservation':
                from app.apps.local_service import LocalServiceApp
                app = agent.env.apps.get('LocalServiceApp')
                if isinstance(app, LocalServiceApp):
                    actual = app._restore_reservation_result(record.get('args') or {}, actual, observation)
            if actual != observation:
                raise ValueError('Replay raw observation mismatch at ' + str(record['seq']))
        else:
            saved = record.get('result')
            if saved == actual:
                observation = saved
            elif saved == _summarize_result(record['tool'], actual):
                observation = actual
            else:
                raise ValueError('Replay summarized observation mismatch at ' + str(record['seq']))
        messages.append({'role': 'assistant', 'content': output['response'] or agent._format_action(record['tool'], record.get('args') or {})})
        messages.append({'role': 'user', 'content': 'Observation:\n' + json.dumps(observation, ensure_ascii=False)})
        observation_context.append({'seq': record['seq'], 'sha256': hashlib.sha256(json.dumps(observation, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()})
        from test_runner.schema import ToolCallRecord
        reconstructed.append(ToolCallRecord(**{k: record[k] for k in ('seq', 'timestamp', 'tool', 'args', 'duration_ms')}, result=observation, llm_input=record.get('llm_input'), llm_output=output))
    if row.get('dynamic_event_states') and row['dynamic_event_states'] != agent.env.event_states:
        raise ValueError('Replay dynamic event-state mismatch')
    if row.get('answer_versions') and row['answer_versions'] != getattr(agent.env.apps.get('UserInterfaceApp'), 'answer_versions', []):
        raise ValueError('Replay answer-version mismatch')
    agent.env.trace = reconstructed
    step = protocol.get('step')
    prior_errors = [e for e in legacy_errors if e.get('step', step) < step] if isinstance(step, int) else []
    if not isinstance(step, int) or (not checkpoint and step != len(traces) + len(prior_errors) + 1):
        raise ValueError('Missing or inconsistent original failed-step count')
    errors = copy.deepcopy(checkpoint.get('protocol_errors') or [])
    if errors and errors[-1].get('step') == step and (not errors[-1].get('retry_allowed')):
        errors.pop()
    if checkpoint:
        if checkpoint.get('max_steps') != agent.max_steps:
            raise ValueError('Recovery model-step budget mismatch')
        if checkpoint.get('max_protocol_retries') != agent._max_protocol_retries:
            raise ValueError('Recovery correction limit mismatch')
        saved_messages = checkpoint.get('messages')
        explicit = getattr(agent.env, 'dynamic_information_mode', 'pull') == 'explicit_update'
        prefix_matches = bool(saved_messages) and saved_messages[:len(original_messages)] == original_messages
        if explicit and saved_messages:
            from app.runtime.explicit_updates import compose_system_prompt
            (expected_system, _) = compose_system_prompt(system, agent.env)
            saved_system = saved_messages[0].get('content', '')
            receipts = row.get('explicit_update_deliveries') or []
            last_returned = bool(receipts) and hashlib.sha256(saved_system.encode('utf-8')).hexdigest() == receipts[-1].get('system_sha256') and saved_system.startswith(system + '\n\nWORLD UPDATES')
            untouched_initial = not receipts and (not traces) and (saved_system == system)
            prefix_matches = (saved_system == expected_system or last_returned or untouched_initial) and saved_messages[1:len(original_messages)] == original_messages[1:]
        if not prefix_matches:
            raise ValueError('Invalid saved conversation prefix')
        observed = [m.get('content') for m in messages[len(original_messages):] if m['role'] == 'user']
        transcript = [m.get('content') for m in saved_messages if m['role'] == 'user']
        cursor = 0
        for observation in observed:
            while cursor < len(transcript) and transcript[cursor] != observation:
                cursor += 1
            if cursor == len(transcript):
                raise ValueError('Saved conversation omits a replayed observation')
            cursor += 1
        messages = copy.deepcopy(saved_messages)
        turns = copy.deepcopy(checkpoint.get('model_turns') or [])
        model_steps = checkpoint['model_steps']
    else:
        turns = []
        messages = copy.deepcopy(original_messages)
        by_step = {e['step']: e for e in prior_errors}
        if len(by_step) != len(prior_errors):
            raise ValueError('Duplicate legacy error-step audit')
        success_iter = iter(reconstructed)
        for old_step in range(1, step):
            if old_step in by_step:
                error = by_step[old_step]
                if not error.get('retry_allowed') or 'response' not in error:
                    raise ValueError('Missing legacy feedback evidence')
                response = error['response'] or '[Rejected tool call; see schema error below.]'
                feedback = 'Observation:\n' + json.dumps({'status': 'call_rejected', 'executed': False, 'error': error.get('error', 'unparseable_action'), 'format': 'Action: followed by one JSON object with exactly action (string) and action_input (object), ending with <end_action>.', 'instruction': 'Use only the already published tool schemas. Correct the call yourself. No action was executed.'})
                messages.extend([{'role': 'assistant', 'content': response}, {'role': 'user', 'content': feedback}])
                turns.append({'step': old_step, 'response': error['response'], 'feedback': feedback, 'error': error.get('error', 'unparseable_action'), 'recovered_legacy': True})
            else:
                record = next(success_iter)
                response = (record.llm_output or {}).get('response') or agent._format_action(record.tool, record.args)
                messages.extend([{'role': 'assistant', 'content': response}, {'role': 'user', 'content': 'Observation:\n' + json.dumps(record.result, ensure_ascii=False)}])
                turns.append({'step': old_step, 'response': response, 'tool_seq': record.seq, 'recovered_legacy': True})
        errors = copy.deepcopy(prior_errors)
        model_steps = step - 1 if category == 'model_api' or empty_legacy_response else step
        if category != 'model_api' and (not empty_legacy_response):
            turns.append({'step': step, 'response': protocol.get('response', ''), 'error': protocol.get('error', 'unparseable_action'), 'recovered_legacy': True})
    last_call_step = max((t.get('step', 0) for t in turns if t.get('tool_seq') is not None), default=0)
    if getattr(agent.env, 'dynamic_information_mode', 'pull') == 'explicit_update':
        receipts = copy.deepcopy(row.get('explicit_update_deliveries') or [])
        by_step = {t['step']: t for t in turns}
        for receipt in receipts:
            turn = by_step.get(receipt.get('model_step'), {})
            if receipt.get('response_received') is not True or turn.get('system_sha256') != receipt.get('system_sha256'):
                raise ValueError('Recovery Explicit-Update receipt is not bound to a returned model turn')
        agent.env.explicit_update_deliveries = receipts
    round_retries = sum((bool(e.get('retry_allowed')) for e in errors if e.get('step', 0) > last_call_step))
    if checkpoint.get('retry_scope') == 'per_call_round' and checkpoint.get('round_retries') != round_retries:
        raise ValueError('Recovery current-round correction count mismatch')
    return {'messages': messages, 'system_prompt': system, 'observation_context': observation_context, 'start_step': model_steps, 'model_steps': model_steps, 'initial_messages': original_messages, 'model_turns': turns, 'protocol_errors': errors, 'round_retries': round_retries, 'api_requests': copy.deepcopy(checkpoint.get('api_requests') or []), 'retry_api_request': category == 'model_api' or empty_legacy_response, 'provenance': checkpoint.get('input_reconstruction') or recovery_input.get('provenance') or {}}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results', required=True)
    parser.add_argument('--task-id', required=True)
    parser.add_argument('--task-dir', required=True)
    parser.add_argument('--config', default='config.yaml')
    parser.add_argument('--model-key', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--recovery-input', help='Verified original system prompt for a zero-call record')
    parser.add_argument('--historical-revision', help='Local historical prompt code; validated against archived public prompts')
    args = parser.parse_args()
    out = Path(args.output)
    if out.exists():
        raise ValueError('Refusing to overwrite recovery output')
    source = json.loads(Path(args.results).read_text(encoding='utf-8'))
    matches = [t for t in source['tasks'] if t['task_id'] == args.task_id]
    if len(matches) != 1:
        raise ValueError('Expected exactly one source record')
    row = matches[0]
    if args.recovery_input:
        row['recovery_input'] = json.loads(Path(args.recovery_input).read_text(encoding='utf-8'))
    elif args.historical_revision:
        row['recovery_input'] = {'historical_revision': args.historical_revision, 'archived_prompt_evidence': list({(tr.get('llm_input') or {}).get('system_prompt') for t in source['tasks'] for tr in t.get('trace', []) if (tr.get('llm_input') or {}).get('system_prompt')})}
    from test_runner.runner import run_task, _resolve_route_cache, _result_to_dict
    folder = Path(args.task_dir)
    from test_runner.recovery_audit import event
    from test_runner.recover_batch import atomic
    from uuid import uuid4
    audit_dir = out.parent / (out.stem + '.audit')
    attempt_id = uuid4().hex
    context = {'attempt_id': attempt_id, 'source_run_id': source.get('run_id'), 'task_id': args.task_id, 'source_results': str(Path(args.results).resolve()), 'source_sha256': hashlib.sha256(Path(args.results).read_bytes()).hexdigest()}
    event(audit_dir / 'events', 'recovery_started', **context, failed_step=(row.get('agent_protocol') or {}).get('step'))

    def sink(value):
        if value['phase'] == 'api_attempt':
            event(audit_dir / 'events', 'api_attempt', **context, model_step=value['model_step'], call_round=value['call_round'], attempt=value['attempt'])
        else:
            atomic(audit_dir / 'checkpoint.json', value)
            event(audit_dir / 'events', 'checkpoint_saved', **context, phase=value['phase'], safe_to_resume=value['safe_to_resume'])
    try:
        result = run_task(folder, route_cache_path=_resolve_route_cache('', folder.parent), model_key=args.model_key, config_yaml=args.config, dynamic_event_mode=row.get('dynamic_event_mode', 'shared_world'), recovery_record=row, checkpoint_sink=sink)
    except Exception as exc:
        event(audit_dir / 'events', 'recovery_finished', **context, state='blocked', error=str(exc))
        raise
    from shared.io import write_json
    write_json(out, {'recovery_policy': 'failed_turn_continuation_v2_per_round', 'attempt_id': attempt_id, 'source_results': str(Path(args.results).resolve()), 'source_run_id': source.get('run_id'), 'source_task_id': args.task_id, 'source_failed_step': (row.get('agent_protocol') or {}).get('step'), 'result': _result_to_dict(result)})
    event(audit_dir / 'events', 'recovery_finished', **context, grade=result.grade, model_steps=result.agent_protocol.get('model_steps'), tokens=result.tokens_used)
if __name__ == '__main__':
    main()
