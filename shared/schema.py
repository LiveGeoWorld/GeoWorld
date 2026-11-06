"""Lightweight v7 schema.

These dataclasses define the shared language between the world builder,
ScenarioDirector, app projection compiler, oracle compiler, and judge compiler.
They are intentionally conservative: fields should exist only when a downstream
consumer can use them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


Consumer = Literal[
    "sampler",
    "director",
    "app_state",
    "env_event",
    "agent_visible",
    "oracle",
    "judge",
    "provenance",
]

ConstraintHardness = Literal["hard", "soft", "preference", "hidden"]
Visibility = Literal["visible", "hidden", "delayed", "conditional"]
Confidence = Literal["low", "medium", "high", "unknown"]
EventTriggerType = Literal[
    "time",
    "weather",
    "traffic",
    "transit",
    "service_state",
    "actor_message",
    "actor_location",
    "actor_preference",
    "information_update",
    "manual",
]
TransitionOp = Literal[
    "set",
    "add",
    "subtract",
    "multiply",
    "append",
    "remove",
    "remove_some",
    "mark_visible",
    "mark_hidden",
]
Affordance = Literal[
    "searchable",
    "routable",
    "reservable",
    "bookable",
    "cancelable",
    "modifiable",
    "saveable",
    "messageable",
    "schedulable",
    "confirmable",
]


@dataclass
class Provenance:
    schema_version: str = "v7.global.v1"
    source: list[str] = field(default_factory=list)
    built_at: str | None = None
    generator: str | None = None
    quality_flags: list[str] = field(default_factory=list)


@dataclass
class ConsumerMark:
    field_path: str
    consumers: list[Consumer]
    note: str = ""


@dataclass
class Geometry:
    kind: Literal["point", "polygon", "polyline", "h3_cells", "bbox"]
    data: dict[str, Any]


@dataclass
class Relation:
    target_type: str
    target_id: str
    relation_type: str
    weight: float | None = None
    note: str = ""


@dataclass
class Constraint:
    constraint_id: str
    source_type: str
    source_id: str
    target_type: str
    hardness: ConstraintHardness
    content: str
    visibility: Visibility = "visible"
    visible_via: list[str] = field(default_factory=list)


@dataclass
class Observability:
    visible_via: list[str] = field(default_factory=list)
    available_at: str = "scenario_start"
    visibility: Visibility = "visible"
    confidence: Confidence = "unknown"
    staleness: str = "unknown"
    conflict_group: str | None = None


@dataclass
class OracleAffordance:
    oracle_action_type: str
    app: str
    function: str
    required_args: list[str] = field(default_factory=list)
    judge_strategy: str = "hard_args"


@dataclass
class City:
    city_id: str
    name: str
    timezone: str
    geometry: Geometry | None = None
    supported_apps: list[str] = field(default_factory=list)
    provenance: Provenance = field(default_factory=Provenance)


@dataclass
class District:
    district_id: str
    city_id: str
    name: str
    geometry: Geometry
    role_tags: list[str] = field(default_factory=list)
    dominant_flows: list[str] = field(default_factory=list)
    risk_profile: dict[str, Any] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)
    control_tags: dict[str, list[str]] = field(default_factory=dict)
    director_notes: dict[str, Any] = field(default_factory=dict)
    provenance: Provenance = field(default_factory=Provenance)


@dataclass
class Corridor:
    """City mobility corridor (v2).

    A corridor is a city mobility backbone or service axis that connects
    districts. Old worlds can still provide only ``geometry``/``route_polyline``;
    v2 worlds additionally carry verified route geometry and task impact fields.
    """

    corridor_id: str
    city_id: str
    name: str
    corridor_type: str
    geometry: Geometry
    node_ids: list[str] = field(default_factory=list)
    corridor_class: str = "transport_corridor"
    mode: str = "mixed"
    base_mobility_model: dict[str, Any] = field(default_factory=dict)
    event_hooks: list[str] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    control_tags: dict[str, list[str]] = field(default_factory=dict)
    director_notes: dict[str, Any] = field(default_factory=dict)
    semantic_role: str = ""
    served_district_ids: list[str] = field(default_factory=list)
    served_district_evidence: list[dict[str, Any]] = field(default_factory=list)
    route_polyline: dict[str, Any] = field(default_factory=dict)
    route_geometry: dict[str, Any] = field(default_factory=dict)
    route_runtime_ref: dict[str, Any] = field(default_factory=dict)
    impact_model: dict[str, Any] = field(default_factory=dict)
    alternative_corridor_ids: list[str] = field(default_factory=list)
    provenance: Provenance = field(default_factory=Provenance)


@dataclass
class Place:
    place_id: str
    city_id: str
    name: str
    location: dict[str, float]
    primary_type: str
    address: str = ""
    district_id: str | None = None
    near_corridor_ids: list[str] = field(default_factory=list)
    map_projection: dict[str, Any] = field(default_factory=dict)
    relations: list[Relation] = field(default_factory=list)
    control_tags: dict[str, list[str]] = field(default_factory=dict)
    director_notes: dict[str, Any] = field(default_factory=dict)
    provenance: Provenance = field(default_factory=Provenance)


@dataclass
class ServiceEndpoint:
    service_id: str
    place_id: str
    service_type: str
    affordances: list[Affordance] = field(default_factory=list)
    state_model: dict[str, Any] = field(default_factory=dict)
    constraints: list[Constraint] = field(default_factory=list)
    oracle_affordances: list[OracleAffordance] = field(default_factory=list)
    app_projection: dict[str, Any] = field(default_factory=dict)
    event_hooks: list[str] = field(default_factory=list)
    control_tags: dict[str, list[str]] = field(default_factory=dict)
    director_notes: dict[str, Any] = field(default_factory=dict)
    provenance: Provenance = field(default_factory=Provenance)


@dataclass
class Actor:
    actor_id: str
    name: str
    relationship_to_user: str
    role_in_scenario: str = ""
    preferences: dict[str, Any] = field(default_factory=dict)
    mobility: dict[str, Any] = field(default_factory=dict)
    availability: dict[str, Any] = field(default_factory=dict)
    communication: dict[str, Any] = field(default_factory=dict)
    knowledge_state: dict[str, Any] = field(default_factory=dict)
    constraints: list[Constraint] = field(default_factory=list)
    app_projection: dict[str, Any] = field(default_factory=dict)
    event_hooks: list[str] = field(default_factory=list)
    director_notes: dict[str, Any] = field(default_factory=dict)
    provenance: Provenance = field(default_factory=Provenance)


@dataclass
class InformationItem:
    info_id: str
    content_type: str
    about: dict[str, str]
    content: str
    observability: Observability
    supports_constraints: list[str] = field(default_factory=list)
    app_payload: dict[str, Any] = field(default_factory=dict)
    provenance: Provenance = field(default_factory=Provenance)


@dataclass
class StateTransition:
    target_ref: str
    field_path: str
    op: TransitionOp
    value: Any


@dataclass
class InformationEffect:
    info_id: str | None = None
    channel: str = ""
    visibility_delay_seconds: int = 0
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class EventTemplate:
    event_template_id: str
    name: str
    trigger_type: EventTriggerType
    scope_type: str
    preconditions: list[str] = field(default_factory=list)
    state_transitions: list[StateTransition] = field(default_factory=list)
    information_effects: list[InformationEffect] = field(default_factory=list)
    app_event_projection: dict[str, Any] = field(default_factory=dict)
    director_notes: dict[str, Any] = field(default_factory=dict)
    provenance: Provenance = field(default_factory=Provenance)


@dataclass
class TaskIntent:
    user_prompt: str
    city_id: str
    service_goal: str | None = None
    spatial_goal: str | None = None
    actor_goals: list[str] = field(default_factory=list)
    time_window: dict[str, Any] = field(default_factory=dict)
    constraints: list[Constraint] = field(default_factory=list)
    required_outcome: str = ""


@dataclass
class WorldSlice:
    city_id: str
    district_ids: list[str] = field(default_factory=list)
    corridor_ids: list[str] = field(default_factory=list)
    place_ids: list[str] = field(default_factory=list)
    service_ids: list[str] = field(default_factory=list)
    actor_ids: list[str] = field(default_factory=list)
    information_ids: list[str] = field(default_factory=list)
    event_template_ids: list[str] = field(default_factory=list)


@dataclass
class TimelineEventIR:
    event_id: str
    event_type: Literal["USER", "ENV", "AGENT", "CONDITION"]
    app: str
    function: str
    args: dict[str, Any] = field(default_factory=dict)
    dependencies: list[str] = field(default_factory=list)
    delay_seconds: float | None = None
    absolute_time: float | None = None
    event_time_comparator: str | None = None


@dataclass
class JudgePlan:
    hard_checks: list[dict[str, Any]] = field(default_factory=list)
    soft_checks: list[dict[str, Any]] = field(default_factory=list)
    time_tolerance: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


@dataclass
class FeasibilityReport:
    feasible: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_app_contracts: list[str] = field(default_factory=list)


@dataclass
class GeoScenarioIR:
    scenario_id: str
    task_intent: TaskIntent
    world_slice: WorldSlice
    initial_state_plan: dict[str, Any] = field(default_factory=dict)
    information_plan: dict[str, Any] = field(default_factory=dict)
    timeline: list[TimelineEventIR] = field(default_factory=list)
    oracle_plan: list[TimelineEventIR] = field(default_factory=list)
    judge_plan: JudgePlan = field(default_factory=JudgePlan)
    feasibility_report: FeasibilityReport | None = None
    provenance: Provenance = field(default_factory=Provenance)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
