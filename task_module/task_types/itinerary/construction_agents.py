"""Role-separated construction Agents for incremental itinerary DAG building."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from shared.llm_client import OpenAICompatibleChatClient
from task_module.task_types.itinerary.call_audit import AttemptAudit, AuditWriteError, CallAudit
from task_module.task_types.itinerary.local_transition import validate_transition


ROLE_PROMPTS = {
    "direction": (
        "You are the activity-direction Agent in an itinerary construction pipeline. "
        "Read only the current DAG state. Choose one supplied service_type, or choose "
        "personal_activity when a POI is not appropriate. Never name or select a POI. "
        "Return JSON: {\"decision\": \"service_type|personal_activity\", "
        "\"reason\": \"brief reason\"}."
    ),
    "duration": (
        "You are the activity-duration Agent. A concrete factual POI has already been "
        "selected by code. Choose exactly one supplied legal_end_time after considering "
        "the current state, scenario and POI type. The selected POI includes "
        "required_service_types. When that list has more than one value, write an "
        "activity_name that naturally uses both the primary capability and the selected "
        "secondary capability; do not reduce it to only one of them. Each legal value closes one or more "
        "continuous planning windows. Do not change the POI or route. "
        "Return JSON: {\"activity_name\": \"natural label\", "
        "\"end_time\": \"HH:MM\", \"reason\": \"brief reason\"}."
    ),
    "completion": (
        "You are the non-POI timeline-completion Agent. Fill the next continuous interval "
        "with a realistic personal activity grounded in the traveler, scenario, current "
        "location and remaining time. In personal mode, choose exactly one supplied legal_end_time. Do not "
        "invent hotel facilities or external places. "
        "In personal mode return JSON: {\"activity_name\": \"natural label\", "
        "\"end_time\": \"HH:MM\", \"reason\": \"brief reason\"}. "
        "In poi_transition mode, the next POI and factual route are fixed. Decide when to depart "
        "and start the POI activity, with grounded personal activities before departure at the "
        "current location and optional waiting after arrival. Early or later arrival is allowed; "
        "do not force opening-time arrival. Do not use a closed POI's services while waiting. "
        "Cover every minute of both personal intervals; empty intervals use empty lists. "
        "Respect opening intervals and the remaining travel to the next fixed boundary. "
        "Do not choose another POI, invent facilities, change route duration, create public "
        "constraints, or decide the POI activity end. Use 24-hour HH:mm clock times. "
        "Return exactly {\"route_ref\":\"provided reference\",\"departure_time\":\"HH:mm\","
        "\"activity_start\":\"HH:mm\",\"before_departure\":[{\"start\":\"HH:mm\","
        "\"end\":\"HH:mm\",\"activity_name\":\"personal activity\"}],"
        "\"after_arrival\":[{\"start\":\"HH:mm\",\"end\":\"HH:mm\","
        "\"activity_name\":\"personal activity\"}]}."
    ),
    "overnight": (
        "You are the overnight timeline-completion Agent. Name one realistic, continuous "
        "overnight personal activity from 22:00 until the supplied next-day activation time. "
        "Use the locked hotel and traveler context, but do not invent facilities. Return JSON: "
        "{\"activity_name\": \"natural label\", \"reason\": \"brief reason\"}."
    ),
}


@dataclass
class ItineraryConstructionAgents:
    """Three independent roles sharing a configured transport client."""

    client: OpenAICompatibleChatClient | None
    decisions: list[dict[str, Any]] = field(default_factory=list)
    audit: AttemptAudit | None = None
    _last_call: CallAudit | None = field(default=None, init=False, repr=False)

    def choose_direction(
        self, state: dict[str, Any], service_types: list[str]
    ) -> dict[str, Any]:
        fallback: dict[str, Any] = {}
        for attempt in range(2):
            result = self._call("direction", {
                "current_dag_state": state,
                "allowed_service_types": service_types,
                "validation_feedback": "" if attempt == 0 else "Use exactly one allowed value.",
            }, fallback, role_attempt=attempt + 1)
            valid = result.get("decision") in {*service_types, "personal_activity"}
            self._validation(valid, 'decision_not_in_allowed_values', result.get('decision'),
                             [*service_types, 'personal_activity'])
            if valid:
                return result
        return fallback

    def choose_duration(
        self,
        state: dict[str, Any],
        poi: dict[str, Any],
        *,
        legal_end_times: list[str],
    ) -> dict[str, Any]:
        fallback: dict[str, Any] = {}
        for attempt in range(2):
            result = self._call("duration", {
                "current_dag_state": state,
                "selected_poi": poi,
                "legal_end_times": legal_end_times,
                "validation_feedback": "" if attempt == 0 else "Return exactly one supplied legal_end_time.",
            }, fallback, role_attempt=attempt + 1)
            valid = str(result.get("end_time") or "") in legal_end_times
            self._validation(valid, 'end_time_not_in_legal_end_times', result.get('end_time'), legal_end_times)
            if valid:
                return result
        return fallback

    def complete_timeline(
        self,
        state: dict[str, Any],
        *,
        legal_end_times: list[str],
    ) -> dict[str, Any]:
        fallback: dict[str, Any] = {}
        for attempt in range(2):
            result = self._call("completion", {
                "current_dag_state": state,
                "legal_end_times": legal_end_times,
                "validation_feedback": "" if attempt == 0 else "Choose one legal end time and do not invent facilities.",
            }, fallback, role_attempt=attempt + 1)
            valid = str(result.get("end_time") or "") in legal_end_times
            self._validation(valid, 'end_time_not_in_legal_end_times', result.get('end_time'), legal_end_times)
            if valid:
                return result
        return fallback

    def complete_overnight(self, state: dict[str, Any], next_day_start: str) -> dict[str, Any]:
        fallback: dict[str, Any] = {}
        for attempt in range(2):
            result = self._call("overnight", {
                "current_dag_state": state,
                "start_time": "22:00",
                "next_day_start": next_day_start,
                "validation_feedback": "" if attempt == 0 else "Return a grounded activity_name without inventing facilities.",
            }, fallback, role_attempt=attempt + 1)
            valid = bool(str(result.get("activity_name") or "").strip())
            self._validation(valid, 'activity_name_empty', result.get('activity_name'), 'non-empty activity_name')
            if valid:
                return result
        return fallback

    def complete_transition(self, context: dict[str, Any]) -> dict[str, Any]:
        feedback = ''
        for attempt in range(2):
            result = self._call('completion', {
                'mode': 'poi_transition', **context, 'validation_feedback': feedback,
            }, {}, role_attempt=attempt + 1)
            feedback = validate_transition(result, context)
            self._validation(not feedback, 'invalid_local_transition', result, feedback)
            if not feedback:
                return result
        return {}

    def _validation(self, valid, code, actual, expected):
        if self._last_call is not None and self._last_call.record['status'] == 'parsed':
            self._last_call.finish('completed' if valid else 'rejected',
                                   validation_errors=[] if valid else [
                                       {'code': code, 'actual': actual, 'expected': expected}])

    def _call(self, role: str, payload: dict[str, Any], fallback: dict[str, Any], *, role_attempt=1) -> dict[str, Any]:
        self._last_call = None
        if self.client is None:
            result = dict(fallback)
            self.decisions.append({"role": role, "input": payload, "output": result, "source": "fallback"})
            return result
        messages = [
            {"role": "system", "content": ROLE_PROMPTS[role]},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        options = {"temperature": 0.4, "response_format": {"type": "json_object"}}
        if self.audit is not None:
            self._last_call = self.audit.start_call(
                role, getattr(self.client, 'model', ''), getattr(self.client, 'endpoint_name', ''),
                messages, options, secret=getattr(self.client, 'api_key', ''), role_attempt=role_attempt)
        phase = 'transport'
        try:
            raw = self.client.call_chat(messages, extra=options)
            if self._last_call is not None:
                self._last_call.received(raw, getattr(self.client, 'last_exchange', None))
            phase = 'response_parse'
            content = ((raw.get("choices") or [{}])[0].get("message") or {}).get("content", "")
            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("construction Agent output must be an object")
            if self._last_call is not None:
                self._last_call.record.update(status='parsed', parsed_result=result)
                self._last_call.checkpoint()
            self.decisions.append({"role": role, "input": payload, "output": result, "source": "llm"})
            return result
        except AuditWriteError:
            raise
        except BaseException as exc:
            if self._last_call is not None:
                self._last_call.fail(exc, phase, getattr(self.client, 'last_exchange', None))
            if not isinstance(exc, Exception):
                raise
            result = dict(fallback)
            self.decisions.append({
                "role": role, "input": payload, "output": result,
                "source": "fallback", "error": type(exc).__name__,
            })
            return result
