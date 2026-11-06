"""Agent interface — abstract base for benchmark agents."""
from __future__ import annotations
from abc import ABC, abstractmethod
from app.runtime.environment import Environment
from test_runner.schema import AgentRunResult

class AgentInterface(ABC):
    """An agent that can execute a benchmark task against the environment."""

    def __init__(self, env: Environment, user_prompt: str):
        self.env = env
        self.user_prompt = user_prompt
        self._final_answer: dict | None = None

    @abstractmethod
    def run(self) -> AgentRunResult:
        """Execute the task and return results."""
        ...
