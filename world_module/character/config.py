"""Configuration for character generation."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CharacterGeneratorConfig:
    """Shared configuration for all character generators.

    LLM model is determined by config.yaml → stage_models.scenario_patch.
    """

    # Required: path to world.json
    world_path: str = ""

    # Output control
    output_root: str = "./outputs/characters"

    # Generation params
    count: int = 15
    seed: int = 42

    # LLM config (for LLM generator)
    config_yaml: str = "config.yaml"
    stage_model_key: str = "character_generation"
    # Per-request budgets. Relationship generation has a larger single-call
    # payload than allocation and profile generation.
    llm_timeout_seconds: int = 300
    relationship_timeout_seconds: int = 600
    # A profile generator may repeat a name within a batch or use a name that
    # is already registered by an earlier city version.  Repair locally before
    # relationship generation instead of discovering the shortfall at attach.
    profile_identity_repair_attempts: int = 2
    excluded_character_names: list[str] = field(default_factory=list)
