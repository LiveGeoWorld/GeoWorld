"""Shared module — base classes, IO, config, maps, LLM, validation."""

from shared.schema import (
    City, District, Corridor, Place, ServiceEndpoint,
    Actor, InformationItem, EventTemplate, GeoScenarioIR,
    StateTransition, InformationEffect, Constraint,
    TaskIntent, WorldSlice, TimelineEventIR, JudgePlan, FeasibilityReport,
    Geometry, Relation, Provenance, ConsumerMark, Observability, OracleAffordance,
)
from shared.io import read_json, write_json, to_plain
from shared.config_loader import PipelineConfig
from shared.constants import (
    PLACE_COLOR, ACTOR_COLOR, DISTRICT_COLOR, CORRIDOR_COLOR,
    TYPE_TO_CATEGORY, CATEGORY_LABELS_ZH, CATEGORY_LABELS_EN,
    PLACE_TYPE_ALIASES,
)

__all__ = [
    "City", "District", "Corridor", "Place", "ServiceEndpoint",
    "Actor", "InformationItem", "EventTemplate", "GeoScenarioIR",
    "StateTransition", "InformationEffect", "Constraint",
    "TaskIntent", "WorldSlice", "TimelineEventIR", "JudgePlan", "FeasibilityReport",
    "Geometry", "Relation", "Provenance", "ConsumerMark", "Observability", "OracleAffordance",
    "read_json", "write_json", "to_plain",
    "PipelineConfig",
    "PLACE_COLOR", "ACTOR_COLOR", "DISTRICT_COLOR", "CORRIDOR_COLOR",
    "TYPE_TO_CATEGORY", "CATEGORY_LABELS_ZH", "CATEGORY_LABELS_EN",
    "PLACE_TYPE_ALIASES",
]
