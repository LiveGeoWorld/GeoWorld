"""Exercise local proxy transport without external model calls."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from test_runner.agent.llm_agent import LLMAgent


@pytest.mark.parametrize("provider,path", [
    ("openai_responses", "/v1/responses"),
    ("openai_compatible", "/v1/chat/completions"),
])
def test_local_http_proxy_round_trip(provider, path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, self.headers["Authorization"], body))
            response = {
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
                "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            }
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        agent = object.__new__(LLMAgent)
        agent._base_url = f"http://127.0.0.1:{server.server_port}/v1"
        agent._request_timeout_seconds = 5
        agent._model = "test-model"
        agent._api_key = "local-test-key"
        agent._reasoning_effort = "high"
        agent._provider = provider
        messages = [{"role": "user", "content": "Hello"}]
        result = agent._call_llm(messages, [])
        assert result["choices"][0]["message"]["content"] == "OK"
        assert received[0][:2] == (path, "Bearer local-test-key")
        assert received[0][2]["model"] == "test-model"
        assert received[0][2].get("input", received[0][2].get("messages")) == messages
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_https_endpoint_keeps_tls_and_default_path(monkeypatch):
    import http.client

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    agent = object.__new__(LLMAgent)
    agent._base_url = "https://api.example.com"
    agent._request_timeout_seconds = 30
    conn, path = agent._api_connection("responses")
    assert isinstance(conn, http.client.HTTPSConnection)
    assert conn.host == "api.example.com"
    assert path == "/v1/responses"
    conn.close()


def test_https_endpoint_honors_http_proxy(monkeypatch):
    import http.client

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "")
    agent = object.__new__(LLMAgent)
    agent._base_url = "https://api.example.com/v1"
    agent._request_timeout_seconds = 30

    conn, path = agent._api_connection("chat/completions")

    assert isinstance(conn, http.client.HTTPSConnection)
    assert conn.host == "127.0.0.1"
    assert conn.port == 7890
    assert conn._tunnel_host == "api.example.com"
    assert path == "/v1/chat/completions"
    conn.close()
