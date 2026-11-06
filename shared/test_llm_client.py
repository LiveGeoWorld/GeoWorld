"""Contract tests for configured model endpoint routing."""

from __future__ import annotations

import json
import pytest

from shared.llm_client import OpenAICompatibleChatClient


class _Response:
    status = 200

    def read(self) -> bytes:
        return b'{"choices":[{"message":{"content":"ok"}}]}'


class _Connection:
    created: list["_Connection"] = []

    def __init__(self, host: str, timeout: int) -> None:
        self.host = host
        self.timeout = timeout
        self.request_args = None
        self.closed = False
        self.created.append(self)

    def request(self, *args) -> None:
        self.request_args = args

    def getresponse(self) -> _Response:
        return _Response()

    def close(self) -> None:
        self.closed = True


def test_openai_compatible_client_uses_configured_dashscope_host(monkeypatch) -> None:
    _Connection.created.clear()
    monkeypatch.setattr("shared.llm_client.getproxies", lambda: {})
    monkeypatch.setattr("shared.llm_client.http.client.HTTPSConnection", _Connection)
    client = OpenAICompatibleChatClient(
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="test-key",
        model="qwen-plus",
    )

    response = client.call_chat([{"role": "user", "content": "hello"}], extra={"max_tokens": 8})

    connection = _Connection.created[0]
    method, path, body, headers = connection.request_args
    assert response["choices"][0]["message"]["content"] == "ok"
    assert connection.host == "dashscope.aliyuncs.com"
    assert method == "POST"
    assert path == "/compatible-mode/v1/chat/completions"
    assert json.loads(body)["model"] == "qwen-plus"
    assert headers["Authorization"] == "Bearer test-key"
    assert connection.closed is True


@pytest.mark.parametrize('content,json_mode,expected', [
    ('```json\n{"ok":true}\n```', True, '{"ok":true}'),
    ('```\n{"ok":true}\n```', True, '{"ok":true}'),
    ('{"ok":true}', True, '{"ok":true}'),
    ('```json\n{"ok":true}\n```', False, '```json\n{"ok":true}\n```'),
    ('Explanation\n```json\n{"ok":true}\n```', True, 'Explanation\n```json\n{"ok":true}\n```'),
    ('```json\n{invalid}\n```', True, '```json\n{invalid}\n```'),
    ('```json\n[]\n```', True, '```json\n[]\n```'),
])
def test_json_mode_whole_response_fence(monkeypatch, content, json_mode, expected):
    body = json.dumps({'choices': [{'message': {'content': content}}]}).encode()
    monkeypatch.setattr('shared.llm_client.getproxies', lambda: {})
    monkeypatch.setattr(_Response, 'read', lambda self: body)
    monkeypatch.setattr('shared.llm_client.http.client.HTTPSConnection', _Connection)
    client = OpenAICompatibleChatClient(base_url='https://example.test/v1', api_key='test', model='test')
    extra = {'response_format': {'type': 'json_object'}} if json_mode else {}
    result = client.call_chat([{'role': 'user', 'content': 'JSON'}], extra=extra)
    assert result['choices'][0]['message']['content'] == expected
    assert client.last_exchange['raw_body'] == body.decode()
