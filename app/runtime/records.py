from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ToolCallRecord:
    """A single tool call made by the agent."""
    seq: int
    timestamp: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    llm_input: dict[str, Any] | None = None
    llm_output: dict[str, Any] | None = None
