"""Benchmark execution adapter over the runtime App layer.

``AppRuntime`` keeps the same ``dispatch(action, args, node)`` surface the runner
and LLM agent already use, but the actual behaviour now lives in the reusable
runtime apps under ``app/runtime`` (:class:`MapAppRuntime`,
:class:`UserInterfaceAppRuntime`). This adapter:

  - instantiates those apps from the loaded :class:`TaskSession`;
  - routes ``MapApp.*`` / ``UserInterfaceApp.*`` calls to them;
  - routes ``LocalServiceApp.*`` calls to the read-only service runtime;
  - resolves the oracle ``<...>`` placeholder for the final user answer, using
    the judge node — a benchmark concern the clean apps must stay unaware of.
"""
from __future__ import annotations
from typing import Any
from app.runtime import LocalServiceAppRuntime, MapAppRuntime, UserInterfaceAppRuntime
from test_runner.place.session import TaskSession

class AppRuntime:
    """Dispatches the subset of apps used by curated place tasks."""

    def __init__(self, session: TaskSession) -> None:
        self.session = session
        self.places = session.world.get('places', [])
        self.services = session.world.get('services', [])
        self.map_app = MapAppRuntime(places=self.places, characters=session.world_slice.get('characters', []), cities=session.world.get('cities', []))
        self.local_service = LocalServiceAppRuntime(services=self.services, places=self.places, cities=session.world.get('cities', []))
        self.user_interface = UserInterfaceAppRuntime()

    def dispatch(self, action: str, args: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
        (app, function) = action.split('.', 1)
        if app == 'MapApp' and self.map_app.supports(function):
            return self.map_app.call(function, args)
        if app == 'LocalServiceApp' and self.local_service.supports(function):
            return self.local_service.call(function, args)
        if app == 'UserInterfaceApp' and function == 'send_message_to_user':
            return self._send_message_to_user(args, node)
        if app == 'UserInterfaceApp' and self.user_interface.supports(function):
            return self.user_interface.call(function, args)
        return {'status': 'unsupported', 'app': app, 'function': function}

    def _send_message_to_user(self, args: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
        return self.user_interface.call('send_message_to_user', args)

    @property
    def messages(self) -> list[dict[str, Any]]:
        """Agent-authored messages, kept for backward compatibility."""
        return [{'content': message.get('content', '')} for message in self.user_interface.conversation if message.get('role') == 'agent']

    def _oracle_message_for_node(self, node: dict[str, Any]) -> str:
        expected_parts: list[str] = []
        for check in [*node.get('hard_checks', []), *node.get('soft_checks', [])]:
            if check.get('field') != 'args.content':
                continue
            value = check.get('expected')
            if isinstance(value, str):
                expected_parts.extend((part.strip() for part in value.split(',') if part.strip()))
        if not expected_parts:
            expected_parts.extend(_gold_names(self.session.gold))
        label = node.get('label_zh') or node.get('description_zh') or '结果'
        return f'{label}: ' + '；'.join(dict.fromkeys(expected_parts))

    def _place_by_id(self, place_id: str) -> dict[str, Any] | None:
        for place in self.places:
            if place.get('place_id') == place_id:
                return place
        return None

def _gold_names(gold: dict[str, Any]) -> list[str]:
    if gold.get('task_type') == 'place_grounding':
        name = gold.get('gold', {}).get('place_name', '')
        return [name] if name else []
    return [c.get('name', '') for c in gold.get('gold', {}).get('candidates', []) if c.get('name')]
