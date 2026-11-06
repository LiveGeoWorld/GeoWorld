from __future__ import annotations

import http.client

from typing import Any

from test_runner.agent.llm_agent import LLMAgent, _HTTPStatusError
from app.runtime.environment import Environment
from app.apps.base import BaseApp


class _TerminalToolApp(BaseApp):
    name = "TerminalToolApp"

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [{
            "name": "submit",
            "description": "Submit the completed answer.",
            "parameters": {
                "type": "object", "additionalProperties": False,
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            },
        }]

    def submit(self, content: str) -> dict[str, Any]:
        self.env.terminate("submitted")
        return {"status": "submitted", "content": content}


def _agent_with_response(response: dict[str, Any]) -> LLMAgent:
    env = Environment()
    env.register_app(_TerminalToolApp())
    agent = object.__new__(LLMAgent)
    agent.env = env
    agent.user_prompt = "Complete this task."
    agent.max_steps = 2
    agent._input_images = []
    agent._model = "test-model"
    agent._max_protocol_retries = 0
    agent._call_llm = lambda _messages, _tools: response
    return agent


def test_prompt_declares_text_react_protocol_and_current_tool_schema() -> None:
    agent = _agent_with_response({})
    prompt = agent._build_system_prompt(agent.env.get_tool_schemas())

    assert "Use only the shown tool names and exact JSON parameters." in prompt
    assert "Do not invent tools, internal identifiers, or arguments." in prompt
    assert "STRICT ACTION FORMAT:" in prompt
    assert "A bare JSON object, plain text, or a question without this complete wrapper is invalid." in prompt
    assert "FINAL SUBMISSION:" in prompt
    assert "call exactly `UserInterfaceApp__final_answer`" in prompt
    assert "include every required field at the top level" in prompt
    assert '`plan.selection = {"name": "...", "location": {"lat": ..., "lng": ...}}`' in prompt
    assert "never rename them to `latitude` or `longitude`" in prompt
    assert "<end_action>" in prompt
    assert "Output exactly one Thought and exactly one Action block" in prompt
    assert "Stop immediately after the first `<end_action>`" in prompt
    assert "Never output a second Thought, Action, Observation" in prompt
    assert "submit" in prompt
    assert '"content"' in prompt
    assert "AVAILABLE TOOLS:" in prompt
    assert "AVAILABLE TOOLS FOR THIS TURN" not in prompt


def test_itinerary_prompt_appends_ordered_poi_output_contract_only_for_itinerary() -> None:
    agent = _agent_with_response({})
    agent.task_type = "itinerary"

    prompt = agent._build_system_prompt(agent.env.get_tool_schemas())

    assert "ITINERARY TASK OUTPUT CONTRACT" in prompt
    assert "plan.selection =" not in prompt
    assert '"selected_pois": [' in prompt
    assert "Include exactly one `selected_pois` entry for every activity" in prompt
    assert "Do not add entries for fixed places already specified by the user" in prompt
    assert "copy `name`, `location.lat`, and `location.lng` exactly" in prompt

    agent.task_type = "multi_entity"
    other_prompt = agent._build_system_prompt(agent.env.get_tool_schemas())
    assert "ITINERARY TASK OUTPUT CONTRACT" not in other_prompt

    agent.env.configure_application_catalog({"TerminalToolApp": "Submit answers."})
    agent.task_type = "itinerary"
    catalog_prompt = agent._build_system_prompt(agent.env.get_tool_schemas())
    assert "INSTALLED APPLICATIONS:" in catalog_prompt
    assert "plan.selection =" not in catalog_prompt
    assert "ITINERARY TASK OUTPUT CONTRACT" in catalog_prompt


def test_region_output_contract_is_appended_without_discovery_app_hints() -> None:
    agent = _agent_with_response({})
    agent.task_type = "region_aware"
    contract = agent._task_output_contract()
    assert "REGION TASK OUTPUT CONTRACT" in contract
    assert "MapApp" not in contract
    assert "LocalServiceApp" not in contract
    assert "set plan.selection to null" in contract
    assert contract in agent._build_system_prompt(agent.env.get_tool_schemas())
    agent.env.configure_application_catalog({"TerminalToolApp": "Submit answers."})
    assert contract in agent._build_system_prompt(agent.env.get_tool_schemas())
    agent.task_type = "itinerary"
    assert "REGION TASK OUTPUT CONTRACT" not in agent._build_system_prompt(agent.env.get_tool_schemas())


def test_safety_output_contract_in_both_tool_catalog_modes() -> None:
    agent = _agent_with_response({})
    agent.task_type = "safety_planning"
    contract = agent._task_output_contract()
    assert '"safety_information"' in contract
    assert "character for character" in contract
    rules = contract.split("Output rules:", 1)[1]
    assert len([line for line in rules.splitlines() if line.startswith("- ")]) == 5
    assert "Do not paraphrase, summarize, translate" not in rules
    assert "decoded string must remain identical" in contract
    assert '"selection"' in contract
    assert all(app not in contract for app in ("MapApp", "WeatherApp", "CityNewsApp"))
    assert contract in agent._build_system_prompt(agent.env.get_tool_schemas())
    agent.env.configure_application_catalog({"TerminalToolApp": "Submit answers."})
    assert contract in agent._build_system_prompt(agent.env.get_tool_schemas())
    agent.task_type = "region_aware"
    assert "SAFETY TASK OUTPUT CONTRACT" not in agent._task_output_contract()


def test_text_react_action_executes_under_the_same_agent_loop() -> None:
    agent = _agent_with_response({
        "choices": [{"message": {"content": (
            "Thought: The answer is ready.\n\nAction:\n"
            '{"action":"submit","action_input":{"content":"done"}}\n<end_action>'
        )}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    })

    result = agent.run()

    assert result.termination == {"reason": "submitted"}
    assert len(result.trace) == 1
    assert result.trace[0].tool == "submit"
    assert result.trace[0].llm_output["transport"] == "text_react"
    assert result.agent_protocol == {}


def test_responses_output_text_reads_standard_message_content() -> None:
    data = {
        "output": [
            {"type": "reasoning", "content": []},
            {"type": "message", "content": [
                {"type": "output_text", "text": "Thought: ready."},
                {"type": "output_text", "text": "Action follows."},
            ]},
        ],
    }

    assert LLMAgent._responses_output_text(data) == "Thought: ready.\nAction follows."


def test_intermediate_ends_round_then_early_final_ends_task():
    import json
    from app.apps.dynamic_user_interface import DynamicUserInterfaceApp as UserInterfaceApp
    agent = _agent_with_response({})
    agent.env = Environment()
    app = UserInterfaceApp()
    agent.env.register_app(app)
    agent.env.init_events("2026-09-25T11:00:00+00:00", planning_deadline="2026-09-25T12:00:00+00:00")
    agent.task_type = "dynamic_replanning"
    agent.max_steps = 7
    calls = iter([("get_simulation_time", {}), ("get_simulation_time", {}),
                  ("final_answer", {"feasible": True, "explanation": "current", "answer_type": "intermediate",
                                    "next_query_time": "2026-09-25T11:30:00+00:00"}),
                  ("get_simulation_time", {}),
                  ("final_answer", {"feasible": True, "explanation": "finished", "answer_type": "final",
                                    "plan": {"event_information": ["Observed update"]}})])
    def response(_messages, _tools):
        name, args = next(calls)
        content = "Thought: act.\nAction:\n" + json.dumps({"action": name, "action_input": args}) + "\n<end_action>"
        return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {}}
    agent._call_llm = response
    result = agent.run()
    assert len(result.trace) == 5
    assert result.trace[2].result["status"] == "intermediate_recorded"
    assert result.trace[3].result["virtual_time"] == "2026-09-25T11:30:00+00:00"
    assert len(result.answer_versions) == 2
    assert result.initial_plan["explanation"] == "current"
    assert not result.termination
    assert result.final_answer["explanation"] == "finished"


def test_intermediate_only_does_not_become_final_on_step_limit():
    import json
    from app.apps.dynamic_user_interface import DynamicUserInterfaceApp
    agent = _agent_with_response({})
    agent.env = Environment()
    agent.env.register_app(DynamicUserInterfaceApp())
    agent.env.init_events("2026-09-25T11:00:00+00:00", planning_deadline="2026-09-25T12:00:00+00:00")
    agent.task_type = "dynamic_replanning"
    agent.max_steps = 1
    content = "Thought: return later.\nAction:\n" + json.dumps({"action": "final_answer", "action_input": {
        "feasible": True, "explanation": "current", "answer_type": "intermediate",
        "next_query_time": "2026-09-25T11:30:00+00:00"}}) + "\n<end_action>"
    agent._call_llm = lambda *_: {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
    result = agent.run()
    assert result.final_answer is None
    assert len(result.answer_versions) == 1
    assert result.termination["category"] == "step_limit_reached"


def test_responses_output_text_keeps_legacy_top_level_field() -> None:
    assert LLMAgent._responses_output_text({"output_text": "legacy"}) == "legacy"


def test_no_action_reply_is_auditable_protocol_failure() -> None:
    agent = _agent_with_response({
        "choices": [{"message": {"content": "I would search before answering."}, "finish_reason": "stop"}],
        "usage": {},
    })

    result = agent.run()

    assert result.trace == []
    assert result.termination == {"reason": "agent_protocol_failure", "category": "no_action"}
    assert result.agent_protocol == {
        "status": "no_action", "step": 1,
        "finish_reason": "stop", "response": "I would search before answering.",
    }


def _text_reply(text):
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 7}}


def test_call_feedback_recovers_without_executing_rejected_call():
    import json
    agent = _agent_with_response({})
    agent._max_protocol_retries = 2
    agent.max_steps = 3
    replies = iter([
        _text_reply('I will submit.'),
        _text_reply('Action:\n' + json.dumps({"action": "unknown", "action_input": {}})),
        _text_reply('Action:\n' + json.dumps({"action": "submit", "action_input": {"content": "done"}})),
    ])
    contexts = []
    def call(messages, tools):
        contexts.append(list(messages))
        return next(replies)
    agent._call_llm = call
    result = agent.run()
    assert len(result.trace) == 1
    assert result.trace[0].tool == 'submit'
    assert result.termination == {"reason": "submitted"}
    assert len(result.agent_protocol['errors']) == 2
    assert result.agent_protocol['model_steps'] == 3
    assert result.tokens_used['total'] == 21
    assert 'tool_not_available' in contexts[2][-1]['content']
    assert '"executed": false' in contexts[1][-1]['content']


def test_call_feedback_is_bounded_and_does_not_rewrite_arguments():
    agent = _agent_with_response(_text_reply(
        'Action:\n{"action":"submit","action_input":{"content":"done","hidden":1}}'))
    agent._max_protocol_retries = 2
    agent.max_steps = 10
    result = agent.run()
    assert not result.trace
    assert result.termination['category'] == 'invalid_action'
    assert len(result.agent_protocol['errors']) == 3
    assert result.agent_protocol['errors'][-1]['retry_allowed'] is False
    assert result.agent_protocol['model_steps'] == 3


def test_feedback_cannot_extend_total_step_budget():
    agent = _agent_with_response(_text_reply('Only a thought.'))
    agent._max_protocol_retries = 2
    agent.max_steps = 1
    result = agent.run()
    assert not result.trace
    assert result.agent_protocol['errors'][0]['retry_allowed'] is False
    assert result.agent_protocol['model_steps'] == 1


def test_text_action_rejects_unknown_or_extra_arguments() -> None:
    agent = _agent_with_response({})
    schemas = agent.env.get_tool_schemas()

    assert LLMAgent._validate_tool_call("submit", {"content": "ok"}, schemas) == (True, "")
    assert LLMAgent._validate_tool_call("submit", {"content": "ok", "hidden": 1}, schemas) == (
        False, "unexpected_arguments:hidden",
    )
    assert LLMAgent._validate_tool_call("unknown", {}, schemas) == (False, "tool_not_available")


def test_text_action_accepts_a_stop_sequence_stripped_end_marker() -> None:
    action = LLMAgent._text_action(
        'Thought: submit now.\nAction:\n{"action":"submit","action_input":{"content":"done"}}'
    )
    assert action == {"name": "submit", "arguments": {"content": "done"}}


def test_static_toolbox_prompt_is_stable_for_the_episode() -> None:
    env = Environment()
    env.register_app(_TerminalToolApp())
    agent = object.__new__(LLMAgent)
    agent.env = env
    agent.user_prompt = "Complete this task."
    agent.max_steps = 2
    agent._input_images = []
    agent._model = "test-model"
    agent._native_tool_transport = False
    inputs: list[list[dict[str, Any]]] = []

    def respond(messages, _tools):
        inputs.append(messages)
        return {
            "choices": [{"message": {"content": (
                "Thought: ready.\nAction:\n"
                '{"action":"submit","action_input":{"content":"done"}}\n<end_action>'
            )}, "finish_reason": "stop"}],
            "usage": {},
        }

    agent._call_llm = respond
    agent.run()

    assert len(inputs) == 1
    assert inputs[0][0]["content"] == agent._build_system_prompt(env.get_tool_schemas())


def test_transient_model_disconnect_retries_before_returning_a_response() -> None:
    agent = object.__new__(LLMAgent)
    agent._provider = "openai_compatible"
    agent._max_transient_retries = 2
    agent._retry_backoff_seconds = 0
    attempts = 0

    def respond(_messages, _tools):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise http.client.RemoteDisconnected("peer closed")
        return {"choices": [{"message": {"content": "OK"}}], "usage": {}}

    agent._call_chat = respond
    response = agent._call_llm([], [])

    assert response == {"choices": [{"message": {"content": "OK"}}], "usage": {}}
    assert attempts == 2
    assert [item["status"] for item in agent._last_api_attempts] == ["error", "success"]
    assert agent._last_api_attempts[0]["retryable"] is True


def test_non_retryable_http_error_is_not_repeated() -> None:
    agent = object.__new__(LLMAgent)
    agent._provider = "openai_compatible"
    agent._max_transient_retries = 2
    agent._retry_backoff_seconds = 0
    attempts = 0

    def reject(_messages, _tools):
        nonlocal attempts
        attempts += 1
        raise _HTTPStatusError(401, "invalid credentials")

    agent._call_chat = reject
    assert agent._call_llm([], []) is None
    assert attempts == 1
    assert agent._last_api_attempts[0]["retryable"] is False


def test_exhausted_api_attempts_are_infrastructure_failure_not_model_failure() -> None:
    agent = _agent_with_response({})
    agent._call_llm = lambda _messages, _tools: None
    agent._last_api_attempts = [{
        "attempt": 1, "status": "error", "retryable": True,
        "error_type": "RemoteDisconnected", "error": "peer closed", "duration_ms": 1,
    }]

    result = agent.run()

    assert result.termination == {
        "reason": "evaluation_infrastructure_failure", "category": "model_api",
    }
    assert result.grade == "INFRASTRUCTURE_FAILURE"
    assert result.agent_protocol["status"] == "infrastructure_failure"
    assert result.agent_protocol["api_attempts"][0]["error_type"] == "RemoteDisconnected"


def test_empty_answer_retries_and_records_diagnostic():
    from unittest.mock import patch
    agent = object.__new__(LLMAgent)
    agent._provider = "openai_compatible"
    agent._max_transient_retries = 1
    agent._retry_backoff_seconds = 0
    replies = iter([
        {"choices": [{"message": {"content": None, "reasoning_content": "private"}, "finish_reason": "stop"}]},
        {"choices": [{"message": {"content": "invalid but nonempty"}, "finish_reason": "stop"}]},
    ])
    agent._call_chat = lambda *_: next(replies)
    with patch('test_runner.agent.llm_agent.time.sleep') as sleep:
        response = agent._call_llm([], [])
    sleep.assert_called_once_with(10.0)
    assert response["choices"][0]["message"]["content"] == "invalid but nonempty"
    assert agent._last_api_attempts[0]["status"] == "empty_response"
    assert agent._last_api_attempts[0]["reasoning_length"] == 7
    assert "private" not in str(agent._last_api_attempts)


def test_empty_answer_exhaustion_returns_api_failure():
    from unittest.mock import patch
    agent = object.__new__(LLMAgent)
    agent._provider = "openai_compatible"
    agent._max_transient_retries = 1
    agent._retry_backoff_seconds = 0
    agent._call_chat = lambda *_: {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
    with patch('test_runner.agent.llm_agent.time.sleep') as sleep:
        assert agent._call_llm([], []) is None
    assert [call.args[0] for call in sleep.call_args_list] == [10.0, 30.0]
    assert sum(a["status"] == "empty_response" for a in agent._last_api_attempts) == 3


def test_action_marker_in_thought_does_not_hide_real_call():
    block = LLMAgent._format_action("MapApp__search_places", {"query": "bakery Manhattan New York"})
    prose = 'Thought: Use the Action: line and end with <end_action>.\n\n'
    assert LLMAgent._text_action(prose + block) == {
        "name": "MapApp__search_places", "arguments": {"query": "bakery Manhattan New York"}}
    assert LLMAgent._text_action(prose + block.replace('Action:\n', 'Action: ')) is not None
    assert LLMAgent._text_action('Thought: Mention Action: but do not make a call.') is None
