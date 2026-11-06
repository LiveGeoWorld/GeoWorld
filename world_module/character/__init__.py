"""Virtual character module — culture-aware character + relationship generation.

Production:   python -m world_module.character.cli --world .../world.json --count 20
Demo (no LLM): python -m world_module.character.cli --world .../world.json --demo --count 5
"""

from world_module.character.config import CharacterGeneratorConfig
from world_module.character.character_schema import (
    Character, CharacterPreferences, CharacterMobility,
    CharacterAvailability, CharacterCommunication, CharacterConstraint,
    Relationship, RelationshipGraph, RelationshipGraphEdge, RelationshipGraphNode,
    CharacterSet,
)
from world_module.character.llm_generator import LLMGenerator
from world_module.character.demo_generator import DemoGenerator

__all__ = [
    "CharacterGeneratorConfig",
    "Character", "CharacterSet",
    "CharacterPreferences", "CharacterMobility", "CharacterAvailability",
    "CharacterCommunication", "CharacterConstraint",
    "Relationship", "RelationshipGraph",
    "RelationshipGraphEdge", "RelationshipGraphNode",
    "LLMGenerator",
    "DemoGenerator",
]
