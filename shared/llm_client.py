"""Configured Responses and Chat Completions clients for model providers."""

from __future__ import annotations

import http.client
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from urllib.request import getproxies, proxy_bypass

from shared.config_loader import PipelineConfig
from shared.costing import apply_llm_pricing_reference
from shared.external_usage import record_external_usage


def api_connection(base_url: str, endpoint: str, timeout: int):
    """Resolve a configured HTTP(S) API while honoring standard proxy env vars."""
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Model base_url must be an absolute HTTP(S) URL")
    path = (parsed.path.rstrip("/") or "/v1") + "/" + endpoint
    hostname = parsed.hostname or ""
    proxy_value = None if proxy_bypass(hostname) else getproxies().get(parsed.scheme)
    if not proxy_value:
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        return connection_type(parsed.netloc, timeout=timeout), path

    proxy = urlparse(proxy_value)
    if proxy.scheme not in {"http", "https"} or not proxy.hostname:
        # http.client cannot speak SOCKS directly. Prefer an explicitly configured
        # HTTP(S) proxy and otherwise retain the previous direct-connect behavior.
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        return connection_type(parsed.netloc, timeout=timeout), path
    if parsed.scheme == "https":
        # HTTPSConnection performs a plaintext CONNECT to the configured proxy,
        # then wraps the resulting tunnel with TLS for the destination host.
        connection = http.client.HTTPSConnection(proxy.hostname, proxy.port, timeout=timeout)
        connection.set_tunnel(parsed.hostname, parsed.port or 443)
        return connection, path
    proxy_type = http.client.HTTPSConnection if proxy.scheme == "https" else http.client.HTTPConnection
    connection = proxy_type(proxy.hostname, proxy.port, timeout=timeout)
    return connection, base_url.rstrip("/") + "/" + endpoint


@dataclass
class YunwuResponsesClient:
    model: str = "gpt-5.5"
    host: str = "127.0.0.1:8317"
    api_key_env: str = "CLIPROXY_API_KEY"
    config_path: str | None = None
    endpoint_name: str = "cliproxy_gpt55_pro"
    timeout_seconds: int = 90
    telemetry: dict[str, Any] = field(default_factory=lambda: {
        "service": "llm", "requests_total": 0, "failures": 0, "elapsed_seconds": 0.0,
        "token_usage": {"prompt": 0, "completion": 0, "total": 0, "cached_input": 0},
        "request_usage": [],
        "cost_status": "pricing_not_configured", "money_estimate": None,
    })
    pricing_reference: dict[str, Any] | None = field(default=None, init=False)

    def telemetry_snapshot(self) -> dict[str, Any]:
        snapshot = apply_llm_pricing_reference(self.telemetry, self.pricing_reference)
        snapshot["elapsed_seconds"] = round(float(snapshot["elapsed_seconds"]), 3)
        return snapshot

    def call(self, user_content: str, *, extra: dict[str, Any] | None = None,
             timeout_seconds: int | None = None) -> dict[str, Any]:
        started_at = time.monotonic()
        self.telemetry["requests_total"] += 1
        api_key = os.environ.get(self.api_key_env)
        model = self.model
        scheme = "http" if self.host == "127.0.0.1:8317" else "https"
        base_url = f"{scheme}://{self.host}/v1"
        try:
            config = PipelineConfig(self.config_path) if self.config_path else PipelineConfig()
            endpoint = config.model_endpoint(self.endpoint_name)
            self.pricing_reference = endpoint.get("pricing_reference")
            model = endpoint.get("model", model)
            base_url = endpoint.get("base_url", base_url)
            if not api_key:
                endpoint = config.model_endpoint(self.endpoint_name)
                api_key = endpoint.get("api_key")
        except Exception:
            if not api_key:
                api_key = None
        if not api_key:
            raise RuntimeError(
                f"Missing model API key. Set {self.api_key_env} or "
                f"model_endpoints.{self.endpoint_name}.api_key in pipeline/config.yaml."
            )

        payload: dict[str, Any] = {
            "model": model,
            "input": [{"role": "user", "content": user_content}],
        }
        if extra:
            payload.update(extra)

        parsed = urlparse(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError(f"Invalid Responses API base_url: {base_url!r}")
        path = parsed.path.rstrip("/") + "/responses"
        conn, path = api_connection(base_url, "responses", timeout_seconds or self.timeout_seconds)
        try:
            conn.request(
                "POST",
                path,
                json.dumps(payload),
                {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            res = conn.getresponse()
            body = res.read().decode("utf-8")
        except Exception:
            self.telemetry["failures"] += 1
            self.telemetry["elapsed_seconds"] += time.monotonic() - started_at
            raise
        finally:
            conn.close()

        try:
            if res.status >= 400:
                raise RuntimeError(f"Responses API error {res.status}: {body}")
            data = json.loads(body)
            usage = data.get("usage", {}) or {}
            prompt = int(usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0)
            completion = int(usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0)
            total = int(usage.get("total_tokens", prompt + completion) or 0)
            cached_input = int(
                usage.get("cached_input_tokens", usage.get("prompt_cache_hit_tokens", 0)) or 0
            )
            for key, value in (
                ("prompt", prompt), ("completion", completion), ("total", total),
                ("cached_input", cached_input),
            ):
                self.telemetry["token_usage"][key] += value
            self.telemetry["request_usage"].append({
                "prompt": prompt, "completion": completion, "total": total,
                "cached_input": cached_input,
            })
            record_external_usage(
                provider="llm", operation="responses", status="success",
                cache_status="not_applicable", elapsed_s=time.monotonic() - started_at,
                usage={"prompt": prompt, "completion": completion, "total": total, "cached_input": cached_input, "request_units": 1},
                model=model, endpoint_name=self.endpoint_name,
            )
            return data
        except Exception:
            self.telemetry["failures"] += 1
            record_external_usage(
                provider="llm", operation="responses", status="failure",
                cache_status="not_applicable", elapsed_s=time.monotonic() - started_at,
                usage={"request_units": 1}, model=model, endpoint_name=self.endpoint_name,
            )
            raise
        finally:
            self.telemetry["elapsed_seconds"] += time.monotonic() - started_at


@dataclass
class OpenAICompatibleChatClient:
    """Small chat-completions client that honors a configured endpoint URL."""

    base_url: str
    api_key: str
    model: str
    timeout_seconds: int = 300
    endpoint_name: str = ""
    pricing_reference: dict[str, Any] | None = None
    last_exchange: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    telemetry: dict[str, Any] = field(default_factory=lambda: {
        "service": "llm", "requests_total": 0, "failures": 0, "elapsed_seconds": 0.0,
        "token_usage": {"prompt": 0, "completion": 0, "total": 0, "cached_input": 0},
        "request_usage": [],
        "cost_status": "pricing_not_configured", "money_estimate": None,
    })

    def telemetry_snapshot(self) -> dict[str, Any]:
        snapshot = apply_llm_pricing_reference(self.telemetry, self.pricing_reference)
        snapshot["elapsed_seconds"] = round(float(snapshot["elapsed_seconds"]), 3)
        return snapshot

    def call_chat(self, messages: list[dict[str, Any]], *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        started_at = time.monotonic()
        self.last_exchange = {}
        self.telemetry["requests_total"] += 1
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError(f"Invalid OpenAI-compatible base_url: {self.base_url!r}")
        path = parsed.path.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if extra:
            payload.update(extra)
        conn, path = api_connection(self.base_url, "chat/completions", self.timeout_seconds)
        try:
            conn.request(
                "POST", path, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                {
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            response = conn.getresponse()
            self.last_exchange['http_status'] = response.status
            raw_body = response.read()
            self.last_exchange['raw_body'] = raw_body.decode('utf-8', errors='replace')
            body = raw_body.decode("utf-8")
        except BaseException as exc:
            self.telemetry["failures"] += 1
            self.telemetry["elapsed_seconds"] += time.monotonic() - started_at
            record_external_usage(
                provider='llm', operation='chat_completions',
                status='failure' if isinstance(exc, Exception) else 'interrupted',
                cache_status='not_applicable', elapsed_s=time.monotonic() - started_at,
                usage={'request_units': 1}, model=self.model, endpoint_name=self.endpoint_name,
                error_class=type(exc).__name__, http_status=self.last_exchange.get('http_status'),
            )
            raise
        finally:
            conn.close()
        try:
            if response.status >= 400:
                raise RuntimeError(f"OpenAI-compatible API error {response.status}: {body}")
            data = json.loads(body)
            # Some compatible gateways wrap JSON-mode replies in Markdown.
            # Remove only a whole-response fence around an already valid object;
            # never repair JSON or extract it from explanatory prose. The exact
            # provider response remains available in last_exchange['raw_body'].
            if (payload.get("response_format") or {}).get("type") == "json_object":
                for choice in data.get("choices") or []:
                    message = choice.get("message") or {}
                    content = message.get("content")
                    if not isinstance(content, str):
                        continue
                    match = re.fullmatch(r"\s*```(?:json)?\s*\n(.*?)\n```\s*", content, re.DOTALL | re.IGNORECASE)
                    if match:
                        candidate = match.group(1).strip()
                        try:
                            decoded = json.loads(candidate)
                        except ValueError:
                            continue
                        if isinstance(decoded, dict):
                            message["content"] = candidate
            usage = data.get("usage", {}) or {}
            prompt = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
            completion = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
            total = int(usage.get("total_tokens", prompt + completion) or 0)
            input_details = usage.get("input_tokens_details", {}) or {}
            cached_input = int(
                usage.get("cached_input_tokens", usage.get("prompt_cache_hit_tokens",
                input_details.get("cached_tokens", 0))) or 0
            )
            for key, value in (
                ("prompt", prompt), ("completion", completion), ("total", total),
                ("cached_input", cached_input),
            ):
                self.telemetry["token_usage"][key] += value
            self.telemetry["request_usage"].append({
                "prompt": prompt, "completion": completion, "total": total,
                "cached_input": cached_input,
            })
            record_external_usage(
                provider="llm", operation="chat_completions", status="success",
                cache_status="not_applicable", elapsed_s=time.monotonic() - started_at,
                usage={"prompt": prompt, "completion": completion, "total": total, "cached_input": cached_input, "request_units": 1},
                model=self.model, endpoint_name=self.endpoint_name,
            )
            return data
        except BaseException as exc:
            self.telemetry["failures"] += 1
            record_external_usage(
                provider="llm", operation="chat_completions", status="failure" if isinstance(exc, Exception) else "interrupted",
                cache_status="not_applicable", elapsed_s=time.monotonic() - started_at,
                usage={"request_units": 1}, model=self.model, endpoint_name=self.endpoint_name,
                error_class=type(exc).__name__, http_status=self.last_exchange.get('http_status'),
            )
            raise
        finally:
            self.telemetry["elapsed_seconds"] += time.monotonic() - started_at
