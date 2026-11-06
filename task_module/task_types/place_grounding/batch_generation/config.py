"""Configuration for place_grounding batch generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shared.io import read_json


DEFAULT_CITY_IDS = ("shanghai_smoke", "tokyo_corridor_smoke", "london_smoke")
DEFAULT_CITY_WEIGHTS = {city_id: 1.0 for city_id in DEFAULT_CITY_IDS}
DEFAULT_CITY_LANGUAGE_MAP = {
    "shanghai_smoke": "zh-CN",
    "tokyo_corridor_smoke": "ja-JP",
    "london_smoke": "en-GB",
}


@dataclass
class BatchGenerationConfig:
    """Controls evidence-preserved grounding batch generation."""

    count: int = 10
    seed: int = 123
    output_root: str = "outputs/benchmarks/place_grounding"
    run_id: str = ""

    city_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_CITY_WEIGHTS))
    granularity_weights: dict[str, float] = field(
        default_factory=lambda: {"exact_poi": 0.4, "nearby_poi": 0.4, "district_area": 0.2}
    )
    category_weights: dict[str, float] = field(default_factory=dict)
    quality_min_ambiguity: int = 2
    quality_max_ambiguity: int = 8
    max_place_attempts: int = 40

    # Deprecated — kept for JSON config backward compatibility only.
    target_type_weights: dict[str, float] = field(default_factory=lambda: {"poi": 1.0})
    difficulty_weights: dict[str, float] = field(
        default_factory=lambda: {"easy": 0.25, "medium": 0.55, "hard": 0.20}
    )

    image_pool_dir: str = "outputs/place_grounding_examples/streetview_candidates"
    use_google_streetview: bool = True
    streetview_output_dir: str = "outputs/benchmarks/place_grounding/batch_assets/streetview"
    streetview_headings: list[int] = field(default_factory=lambda: [0, 90, 180, 270])
    streetview_fov: int = 80
    streetview_pitch: int = 0
    streetview_size: str = "640x640"
    streetview_radius_m: int = 60
    prompt_language: str = "en"
    city_language_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_CITY_LANGUAGE_MAP))
    review_translation_language: str = "zh-CN"
    use_llm_prompt: bool = True
    config_yaml: str = "config.yaml"
    prompt_model_stage: str = "vision_parse"
    prompt_temperature: float = 0.7
    fail_on_llm_error: bool = False
    review_status: str = "draft"

    # Export-compatible curated source settings.
    variant: str = "complex"
    schema_version: str = "v2.curated.place_grounding.batch.v1"

    @classmethod
    def from_json(cls, path: str | Path) -> "BatchGenerationConfig":
        payload = read_json(path)
        allowed = {field.name for field in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        kwargs = {key: value for key, value in payload.items() if key in allowed}
        return cls(**kwargs)

    @property
    def output_dir(self) -> Path:
        run = self.run_id or f"batch_seed_{self.seed}"
        return Path(self.output_root) / run

    def language_for_city(self, city_id: str) -> str:
        """New tasks use English; legacy city maps remain readable only."""
        return "en"


def parse_weight_map(value: str | None, default: dict[str, float]) -> dict[str, float]:
    """Parse ``a=0.5,b=0.5`` CLI values into a weight map."""
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
    """Parse ``a=x,b=y`` CLI values into a string map."""
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
    candidates = [
        root / "outputs" / "characters" / city_id / variant / "characters.json",
        root / "outputs" / "characters" / city_id / "complex" / "characters.json",
        root / "outputs" / "characters" / city_id / "base" / "characters.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def as_jsonable_config(config: BatchGenerationConfig) -> dict[str, Any]:
    return {
        "count": config.count,
        "seed": config.seed,
        "city_weights": config.city_weights,
        "granularity_weights": config.granularity_weights,
        "category_weights": config.category_weights,
        "quality_min_ambiguity": config.quality_min_ambiguity,
        "quality_max_ambiguity": config.quality_max_ambiguity,
        "max_place_attempts": config.max_place_attempts,
        "image_pool_dir": config.image_pool_dir,
        "use_google_streetview": config.use_google_streetview,
        "streetview_output_dir": config.streetview_output_dir,
        "streetview_headings": config.streetview_headings,
        "streetview_radius_m": config.streetview_radius_m,
        "prompt_language": config.prompt_language,
        "city_language_map": config.city_language_map,
        "review_translation_language": config.review_translation_language,
        "use_llm_prompt": config.use_llm_prompt,
        "config_yaml": config.config_yaml,
        "prompt_model_stage": config.prompt_model_stage,
        "prompt_temperature": config.prompt_temperature,
    }
