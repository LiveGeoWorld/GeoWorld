"""Private, per-attempt checkpoints; never part of a model's input."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
from functools import wraps
import json
import os
from pathlib import Path
import time
import traceback
from typing import Any
from uuid import uuid4

from shared.io import write_json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditWriteError(RuntimeError):
    """An audit I/O failure must not be retried as a model rejection."""


def _save(path: Path, record: dict, secrets: tuple[str, ...] = ()) -> None:
    text = json.dumps(record, ensure_ascii=False)
    for secret in secrets:
        if secret:
            text = text.replace(json.dumps(secret, ensure_ascii=False)[1:-1], '[REDACTED]')
    try:
        if os.name == 'nt':
            resolved = str(path.resolve())
            if not resolved.startswith('\\\\?\\'):
                path = Path('\\\\?\\UNC\\' + resolved[2:] if resolved.startswith('\\\\') else '\\\\?\\' + resolved)
        write_json(path, json.loads(text))
    except Exception as exc:
        raise AuditWriteError(f'Could not checkpoint itinerary audit: {path}') from exc


class AttemptAudit:
    def __init__(self, directory: Path, context: dict[str, Any]):
        self.directory = Path(directory)
        self.context = copy.deepcopy(context)
        self.calls: list[CallAudit] = []
        self.started = time.monotonic()
        self.record = {'schema_version': 'geoword.itinerary.attempt_audit.v1',
                       'context': self.context, 'status': 'running', 'started_at': _now()}
        self._checkpoint()

    def _checkpoint(self):
        self.record['call_count'] = len(self.calls)
        _save(self.directory / 'attempt.json', self.record,
              tuple(call.secret for call in self.calls))

    def start_call(self, role, model, endpoint, messages, extra, *, secret='', role_attempt=None):
        call = CallAudit(self, role, model, endpoint, messages, extra, secret, role_attempt)
        self.calls.append(call)
        self._checkpoint()
        return call

    def finish(self, status: str, reason: str = '', **details):
        self.record.update(status=status, reason=reason, finished_at=_now(),
                           elapsed_seconds=round(time.monotonic() - self.started, 4),
                           llm_totals=audit_totals([self]), **details)
        self._checkpoint()


class CallAudit:
    def __init__(self, owner, role, model, endpoint, messages, extra, secret, role_attempt):
        self.secret = secret if isinstance(secret, str) else ''
        self.started = time.monotonic()
        self.path = owner.directory / 'calls' / f'{len(owner.calls) + 1:04d}_{role}.json'
        self.record = {
            'schema_version': 'geoword.itinerary.call_audit.v1', 'context': owner.context,
            'role': role, 'role_attempt': role_attempt, 'model': str(model),
            'endpoint_name': str(endpoint), 'status': 'requesting', 'started_at': _now(),
            'messages': copy.deepcopy(messages), 'request_options': copy.deepcopy(extra),
            'usage': None, 'usage_status': 'not_reported',
        }
        self.checkpoint()

    def checkpoint(self):
        _save(self.path, self.record, (self.secret,))

    def received(self, response: Any, exchange: dict | None = None):
        if isinstance(exchange, dict):
            self.record.update({key: exchange[key] for key in ('http_status', 'raw_body') if key in exchange})
        self.record.update(status='response_received', response=copy.deepcopy(response))
        if isinstance(response, dict):
            usage = response.get('usage')
            if isinstance(usage, dict):
                self.record.update(usage=usage, usage_status='reported')
            self.record['returned_model'] = response.get('model')
            choices = response.get('choices') or []
            if choices and isinstance(choices[0], dict) and isinstance(choices[0].get('message'), dict):
                self.record['messages'].append(copy.deepcopy(choices[0]['message']))
        self.checkpoint()

    def finish(self, status, **details):
        self.record.update(status=status, finished_at=_now(),
                           elapsed_seconds=round(time.monotonic() - self.started, 4), **details)
        self.checkpoint()

    def fail(self, exc: BaseException, phase: str, exchange: dict | None = None):
        if isinstance(exchange, dict):
            self.record.update({key: exchange[key] for key in ('http_status', 'raw_body') if key in exchange})
        self.finish('failed' if isinstance(exc, Exception) else 'interrupted',
                    failure_phase=phase, error_class=type(exc).__name__, error_message=str(exc),
                    traceback=''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


def audit_totals(attempts):
    totals = {'prompt': 0, 'completion': 0, 'total': 0, 'duration_s': 0.0,
              'calls': 0, 'usage_not_reported_calls': 0}
    for attempt in attempts:
        for call in attempt.calls:
            record = call.record
            totals['calls'] += 1
            totals['duration_s'] += float(record.get('elapsed_seconds') or 0)
            usage = record.get('usage')
            if not isinstance(usage, dict):
                totals['usage_not_reported_calls'] += 1
                continue
            prompt = int(usage.get('prompt_tokens', usage.get('input_tokens', 0)) or 0)
            completion = int(usage.get('completion_tokens', usage.get('output_tokens', 0)) or 0)
            totals['prompt'] += prompt
            totals['completion'] += completion
            totals['total'] += int(usage.get('total_tokens', prompt + completion) or 0)
    totals['duration_s'] = round(totals['duration_s'], 4)
    return totals


def audited_generation(method):
    """Retain the active attempt when any stage raises or is interrupted."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        self._itinerary_audit_run = uuid4().hex[:12]
        self._itinerary_attempt_audit = None
        self._itinerary_audits = []
        status = 'completed'
        try:
            return method(self, *args, **kwargs)
        except BaseException as exc:
            status = 'failed' if isinstance(exc, Exception) else 'interrupted'
            audit = self._itinerary_attempt_audit
            if audit is not None and not isinstance(exc, AuditWriteError):
                if audit.calls and audit.calls[-1].record['status'] in {'requesting', 'response_received', 'parsed'}:
                    audit.calls[-1].fail(exc, 'generation')
                audit.finish(status, str(exc),
                             error_class=type(exc).__name__, traceback=traceback.format_exc())
            raise
        finally:
            if self._itinerary_audits:
                _save(self.output_dir / 'construction_audit' / self._itinerary_audit_run / 'summary.json',
                      {'status': status, 'finished_at': _now(), 'llm_totals': audit_totals(self._itinerary_audits),
                       'attempts': [{'context': a.context, 'status': a.record['status'],
                                     'path': str(a.directory.relative_to(self.output_dir))}
                                    for a in self._itinerary_audits]})
    return wrapped
