"""Map module — generate executable city worlds from seeds.

Usage:
    from world_module.map import MapGenerator, MapGeneratorConfig, CitySeed, load_seed

    config = MapGeneratorConfig(config_yaml="config.yaml", geo_provider="auto")
    gen = MapGenerator(config)
    result = gen.generate("seeds/tokyo_complex_seed.json")
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from world_module.map.config import MapGeneratorConfig
    from world_module.map.generator import GenerateResult, MapGenerator
    from world_module.map.seed import CitySeed, load_seed

__all__ = [
    "MapGenerator",
    "MapGeneratorConfig",
    "GenerateResult",
    "CitySeed",
    "load_seed",
]


def __getattr__(name: str):
    if name in {"MapGenerator", "GenerateResult"}:
        from world_module.map.generator import GenerateResult, MapGenerator

        return {"MapGenerator": MapGenerator, "GenerateResult": GenerateResult}[name]
    if name == "MapGeneratorConfig":
        from world_module.map.config import MapGeneratorConfig

        return MapGeneratorConfig
    if name in {"CitySeed", "load_seed"}:
        from world_module.map.seed import CitySeed, load_seed

        return {"CitySeed": CitySeed, "load_seed": load_seed}[name]
    raise AttributeError(f"module 'world_module.map' has no attribute {name!r}")
