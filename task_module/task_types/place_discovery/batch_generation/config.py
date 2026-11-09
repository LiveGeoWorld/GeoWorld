"""Configuration for evidence-preserved place_discovery batch generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shared.io import read_json


CHARACTER_CITY_ALIASES: dict[str, str] = {
    "tokyo_corridor_smoke": "tokyo",
}

DEFAULT_CITY_IDS = ("shanghai_smoke", "tokyo_corridor_smoke", "london_smoke")
DEFAULT_CITY_WEIGHTS = {city_id: 1.0 for city_id in DEFAULT_CITY_IDS}
DEFAULT_CITY_LANGUAGE_MAP = {
    "shanghai_smoke": "zh-CN",
    "tokyo_corridor_smoke": "ja-JP",
    "london_smoke": "en-GB",
}
DEFAULT_SERVICE_TYPE_WEIGHTS = {
    "restaurant_reservation": 0.35,
    "cafe_reservation": 0.25,
    "hotel_booking": 0.25,
    "attraction_visit": 0.15,
}


@dataclass
class DiscoveryBatchGenerationConfig:
    """Controls place_discovery batch generation."""

    count: int = 10
    seed: int = 123
    output_root: str = "outputs/benchmarks/place_discovery"
    run_id: str = ""

    city_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_CITY_WEIGHTS))
    service_type_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SERVICE_TYPE_WEIGHTS))
    quality_min_search_ambiguity: int = 2
    quality_max_search_ambiguity: int = 8
    max_service_attempts: int = 40

    prompt_language: str = "auto"
    city_language_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CITY_LANGUAGE_MAP))
    review_translation_language: str = "zh-CN"
    use_llm_prompt: bool = True
    config_yaml: str = "config.yaml"
    prompt_model_stage: str = "vision_parse"
    prompt_temperature: float = 0.7
    fail_on_llm_error: bool = False
    allow_deterministic_fallback: bool = False
    review_status: str = "draft"

    variant: str = "complex"
    schema_version: str = "v2.curated.place_discovery.batch.v1"
    use_characters: bool = True

    @classmethod
    def from_json(cls, path: str | Path) -> "DiscoveryBatchGenerationConfig":
        payload = read_json(path)
        allowed = {field.name for field in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {key: value for key, value in payload.items() if key in allowed}
        return cls(**kwargs)

    @property
    def output_dir(self) -> Path:
        run = self.run_id or f"batch_seed_{self.seed}"
        return Path(self.output_root) / run

    def language_for_city(self, city_id: str) -> str:
        if self.prompt_language and self.prompt_language != "auto":
            return self.prompt_language
        return self.city_language_map.get(city_id, "en")


def pipeline_root() -> Path:
    return Path(__file__).resolve().parents[4]


def infer_world_path(city_id: str) -> Path:
    root = pipeline_root()
    candidates = [
        root / "outputs" / "mapseeds" / city_id / "base" / "world.json",
        root / "outputs" / "mapseeds" / city_id / "complex" / "world.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"No world.json found for city {city_id!r}")


def infer_characters_path(city_id: str, variant: str = "complex") -> Path | None:
    root = pipeline_root()
    alias = CHARACTER_CITY_ALIASES.get(city_id, city_id)
    candidates = [
        root / "outputs" / "characters" / alias / variant / "characters.json",
        root / "outputs" / "characters" / alias / "complex" / "characters.json",
        root / "outputs" / "characters" / alias / "base" / "characters.json",
        root / "outputs" / "characters" / city_id / variant / "characters.json",
        root / "outputs" / "characters" / city_id / "complex" / "characters.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def parse_weight_map(value: str | None, default: dict[str, float]) -> dict[str, float]:
    if not value:
        return dict(default)
    result: dict[str, float] = {}
    for part in value.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"Invalid weight entry {part!r}; expected name=weight")
        key, raw_weight = part.split("=", 1)
        result[key.strip()] = float(raw_weight)
    return result


def parse_string_map(value: str | None, default: dict[str, str]) -> dict[str, str]:
    if not value:
        return dict(default)
    result: dict[str, str] = {}
    for part in value.split(","):
        if not part.strip():
            continue
        if "=" not in part:
            raise ValueError(f"Invalid map entry {part!r}; expected name=value")
        key, raw_value = part.split("=", 1)
        result[key.strip()] = raw_value.strip()
    return result


def as_jsonable_config(config: DiscoveryBatchGenerationConfig) -> dict[str, Any]:
    return {
        "count": config.count,
        "seed": config.seed,
        "city_weights": config.city_weights,
        "service_type_weights": config.service_type_weights,
        "quality_min_search_ambiguity": config.quality_min_search_ambiguity,
        "quality_max_search_ambiguity": config.quality_max_search_ambiguity,
        "max_service_attempts": config.max_service_attempts,
        "prompt_language": config.prompt_language,
        "city_language_map": config.city_language_map,
        "review_translation_language": config.review_translation_language,
        "use_llm_prompt": config.use_llm_prompt,
        "config_yaml": config.config_yaml,
        "prompt_model_stage": config.prompt_model_stage,
        "prompt_temperature": config.prompt_temperature,
        "allow_deterministic_fallback": config.allow_deterministic_fallback,
        "use_characters": config.use_characters,
    }
