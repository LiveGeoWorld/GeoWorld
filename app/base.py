"""App specification data models for GeoAgentBench.

Defines the contract between world.json objects and agent interactions.
Each app exposes functions with defined args, checks, and oracle actions.

Pattern follows ARE (Meta Agents Research Environments):
  - Each app = one AppSpec instance in its own .py file
  - Functions are typed as read/write with structured arg/check definitions
  - ALL_APPS in __init__.py provides the unified registry
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppArg:
    """A single function argument definition."""
    name: str                 # "district_id"
    desc: str                 # "区域ID"
    type_hint: str = "string" # "string" | "int" | "float" | "dict"
    required: bool = True
    default: Any = None


@dataclass
class AppCheck:
    """A single verification criterion for judging agent output."""
    check_type: str           # "action_executed" | "field_equals" | "field_not_empty" | "field_contains" | "field_in_set"
    field: str = ""           # e.g. "response.results" | "args.party_size"
    expected: Any = None      # expected value for "field_equals"
    tolerance: str = ""       # e.g. "15min" | "500m" for soft checks
    weight: str = "hard"      # "hard" | "soft"


@dataclass
class AppFunction:
    """A single function exposed by an app."""
    name: str                       # "search_services"
    label_zh: str                   # "搜索服务"
    description_zh: str = ""        # Full description
    access: str = "read"            # "read" | "write"
    args: list[AppArg] = field(default_factory=list)
    returns_desc: str = ""          # Human description of return value
    oracle_action_type: str = ""    # Corresponding OracleAffordance type
    hard_checks: list[AppCheck] = field(default_factory=list)
    soft_checks: list[AppCheck] = field(default_factory=list)

    def get_arg_names(self) -> list[str]:
        return [a.name for a in self.args]

    def get_required_arg_names(self) -> list[str]:
        return [a.name for a in self.args if a.required]


@dataclass
class AppSpec:
    """Complete specification of a benchmark app.

    This is a design-time contract. The actual runtime implementation
    may be in Python (ARE) or another language. The AppSpec defines
    what the agent should expect from this app.
    """
    name: str                         # "LocalServiceApp"
    label_zh: str = ""                # "本地服务"
    description: str = ""             # What this app does
    version: str = "v2.app.v1"

    # World object → app state mapping
    world_projections: list[str] = field(default_factory=list)
    # e.g. ["Place", "ServiceEndpoint"] for LocalServiceApp

    observes: list[str] = field(default_factory=list)
    # What state categories this app observes

    # State model
    state_fields: dict[str, str] = field(default_factory=dict)
    # e.g. {"services": "dict[service_id, ServiceState]"}
    # Human-readable description of internal state structure

    # Functions
    functions: list[AppFunction] = field(default_factory=list)

    # ── Lookup helpers ──

    def get_function(self, name: str) -> AppFunction | None:
        for f in self.functions:
            if f.name == name:
                return f
        return None

    def list_functions(self) -> list[str]:
        return [f.name for f in self.functions]

    def list_read_functions(self) -> list[str]:
        return [f.name for f in self.functions if f.access == "read"]

    def list_write_functions(self) -> list[str]:
        return [f.name for f in self.functions if f.access == "write"]
