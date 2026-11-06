from __future__ import annotations
from typing import Any
'Base app for the test sandbox.'

class BaseApp:
    """Abstract app that agents can interact with via tool calls."""
    name: str = ''

    def __init__(self):
        self.env = None

    def set_environment(self, env) -> None:
        self.env = env

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return JSON schemas for all tools this app exposes."""
        return []
