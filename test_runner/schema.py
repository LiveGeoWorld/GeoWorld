"""Data models for the test_runner module."""
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

@dataclass
class MetricResult:
    """Result of a single evaluation metric."""
    metric: str
    label: str
    pass_: bool
    detail: str
    score: int = 0
    verification_status: str = ''

@dataclass
class AgentRunResult:
    """Complete result of running an agent on one task."""
    task_id: str
    user_prompt: str
    gt_route_id: str
    gt_route_summary: dict[str, Any] = field(default_factory=dict)
    dynamic_event_mode: str = 'shared_world'
    gt_route_detail: dict[str, Any] = field(default_factory=dict)
    core_places: list[dict[str, Any]] = field(default_factory=list)
    trace: list[ToolCallRecord] = field(default_factory=list)
    final_answer: dict[str, Any] | None = None
    initial_plan: dict[str, Any] | None = None
    metrics: list[MetricResult] = field(default_factory=list)
    total_metrics: int = 0
    passed_metrics: int = 0
    duration_s: float = 0.0
    tokens_used: dict[str, int] = field(default_factory=dict)
    model_name: str = ''
    dynamic_event_states: dict[str, str] = field(default_factory=dict)
    dynamic_causal_log: list[dict[str, Any]] = field(default_factory=list)
    answer_versions: list[dict[str, Any]] = field(default_factory=list)
    evaluation_world_snapshot: dict[str, Any] = field(default_factory=dict)
    termination: dict[str, Any] = field(default_factory=dict)
    agent_protocol: dict[str, Any] = field(default_factory=dict)
    recovery_checkpoint: dict[str, Any] = field(default_factory=dict)
    input_modalities: list[str] = field(default_factory=lambda : ['text'])
    input_asset_hashes: list[str] = field(default_factory=list)
    dynamic_information_mode: str = 'pull'
    explicit_update_deliveries: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_score(self) -> int:
        if self.total_metrics == 0:
            return 0
        return round(self.passed_metrics / self.total_metrics * 100)

    @property
    def grade(self) -> str:
        if self.termination.get('reason') == 'evaluation_infrastructure_failure':
            return 'INFRASTRUCTURE_FAILURE'
        if self.termination.get('reason') == 'agent_protocol_failure' and self.termination.get('category') in {'no_action', 'invalid_action'}:
            return 'FAIL'
        route_metrics = {m.metric: m.pass_ for m in self.metrics}
        if {'feasible_consistency', 'plan_structure', 'waypoint_coverage', 'mode_valid', 'time_constraint', 'walking_constraint', 'tool_efficiency'}.issubset(route_metrics):
            return 'PASS' if route_metrics['feasible_consistency'] and self.passed_metrics >= 5 else 'FAIL'
        if any((metric.metric == 'required_structured_output' and (not metric.pass_) for metric in self.metrics)):
            return 'FAIL'
        dynamic_metrics = {metric.metric: metric.pass_ for metric in self.metrics if metric.metric in {'replan_executed', 'disruption_detected'}}
        if dynamic_metrics and (not all((dynamic_metrics.get(name) is True for name in ('replan_executed', 'disruption_detected')))):
            return 'FAIL'
        if self.total_score >= 70:
            return 'PASS'
        elif self.total_score >= 40:
            return 'PARTIAL'
        return 'FAIL'

@dataclass
class RunSummary:
    """Top-level summary of a batch run."""
    run_id: str = ''
    dynamic_event_mode: str = 'shared_world'
    dynamic_information_mode: str = 'pull'
    model_key: str = ''
    model_name: str = ''
    city_id: str = ''
    world_id: str = ''
    required_city_versions: dict[str, int] = field(default_factory=dict)
    task_batch_provenance_path: str = ''
    started_at: str = ''
    finished_at: str = ''
    total_duration_s: float = 0.0
    token_usage: dict[str, int] = field(default_factory=dict)
    task_count: int = 0
    passed: int = 0
    infrastructure_failed: int = 0
    grades: dict[str, int] = field(default_factory=dict)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    tasks_by_type: dict[str, dict] = field(default_factory=dict)
