"""Runtime App base layer for GeoAgentBench.

This is the *executable* counterpart to the declarative ``AppSpec`` contracts in
``app/*.py``. Where ``AppSpec`` describes *what* an app exposes (the contract an
agent can rely on), a :class:`RuntimeApp` actually *runs* the behaviour against a
loaded world and records the resulting events.

Design mirrors GAIA2 / ARE environment apps:
  - Each app owns its own internal state and a set of registered tools.
  - Every tool call produces an :class:`AppEvent` with an explicit
    :class:`OperationType` (READ vs WRITE vs user input/output), so the judge can
    distinguish information-gathering steps from world-mutating / answer steps.
  - State is introspectable (:meth:`get_state`) and resettable
    (:meth:`reset`), which keeps episodes reproducible.

Tools are plain methods decorated with :func:`app_tool`. Each tool receives the
parsed ``args`` dict and returns a JSON-serialisable ``response`` dict.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

class OperationType(str, Enum):
    """Semantic category of an app tool call.

    The judge uses this to separate *evidence gathering* (READ) from the
    *committing* steps that actually decide task success (WRITE / USER_OUTPUT).
    """
    READ = 'read'
    WRITE = 'write'
    USER_INPUT = 'user_input'
    USER_OUTPUT = 'user_output'

@dataclass
class ToolMeta:
    """Static metadata attached to a runtime tool method."""
    name: str
    operation: OperationType
    summary: str = ''

@dataclass
class AppEvent:
    """A single recorded tool invocation on a runtime app."""
    app: str
    function: str
    operation: str
    args: dict[str, Any]
    response: dict[str, Any]
    index: int

    def to_dict(self) -> dict[str, Any]:
        return {'app': self.app, 'function': self.function, 'operation': self.operation, 'args': self.args, 'response': self.response, 'index': self.index}

def app_tool(operation: OperationType, *, summary: str='') -> Callable:
    """Mark a :class:`RuntimeApp` method as an invokable tool.

    Example::

        @app_tool(OperationType.READ, summary="搜索地点")
        def search_places(self, args):
            ...
    """

    def decorator(func: Callable) -> Callable:
        func._tool_meta = ToolMeta(name=func.__name__, operation=operation, summary=summary)
        return func
    return decorator

class RuntimeApp:
    """Base class for executable benchmark apps.

    Subclasses set :attr:`name` and define tool methods decorated with
    :func:`app_tool`. The base class discovers them, dispatches calls, and keeps
    an ordered event log.
    """
    name: str = 'App'

    def __init__(self) -> None:
        self.events: list[AppEvent] = []
        self._tools: dict[str, ToolMeta] = self._discover_tools()

    def _discover_tools(self) -> dict[str, ToolMeta]:
        tools: dict[str, ToolMeta] = {}
        for attr_name in dir(type(self)):
            attr = getattr(type(self), attr_name, None)
            meta = getattr(attr, '_tool_meta', None)
            if isinstance(meta, ToolMeta):
                tools[meta.name] = meta
        return tools

    def supports(self, function: str) -> bool:
        return function in self._tools

    def call(self, function: str, args: dict[str, Any]) -> dict[str, Any]:
        """Invoke a registered tool, record the event, and return its response."""
        meta = self._tools.get(function)
        if meta is None:
            return {'status': 'unsupported', 'app': self.name, 'function': function}
        method = getattr(self, function)
        response = method(dict(args or {})) or {}
        self.events.append(AppEvent(app=self.name, function=function, operation=meta.operation.value, args=dict(args or {}), response=response, index=len(self.events)))
        return response

    def list_tools(self) -> list[str]:
        return list(self._tools)

    def tool_operation(self, function: str) -> str:
        meta = self._tools.get(function)
        return meta.operation.value if meta else ''

    def write_events(self) -> list[AppEvent]:
        """Events that committed a decision (WRITE / USER_OUTPUT)."""
        committing = {OperationType.WRITE.value, OperationType.USER_OUTPUT.value}
        return [event for event in self.events if event.operation in committing]

    def get_state(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of mutable app state."""
        return {}

    def load_state(self, state: dict[str, Any]) -> None:
        """Restore mutable app state from a snapshot."""

    def reset(self) -> None:
        """Clear recorded events; subclasses should also clear mutable state."""
        self.events.clear()
