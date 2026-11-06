"""CLI for evidence-preserved place_grounding batch generation.

Run from ``pipeline_v2``:

    python -m task_module.task_types.place_grounding.batch_generation --count 9
"""

from __future__ import annotations

import argparse

from task_module.task_types.place_grounding.batch_generation.config import (
    BatchGenerationConfig,
    DEFAULT_CITY_WEIGHTS,
    DEFAULT_CITY_LANGUAGE_MAP,
    parse_string_map,
    parse_weight_map,
)
from task_module.task_types.place_grounding.batch_generation.pipeline import generate_batch


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate evidence-preserved place_grounding drafts.")
    parser.add_argument("--config", default="", help="Optional JSON config file")
    parser.add_argument("--count", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--output-root", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--cities", default="", help="Comma-separated city ids with uniform weights")
    parser.add_argument("--city-weights", default="", help="city=weight,city=weight")
    parser.add_argument("--difficulty-weights", default="", help="(deprecated) ignored; use ambiguity filtering")
    parser.add_argument("--quality-min-ambiguity", type=int, default=None)
    parser.add_argument("--quality-max-ambiguity", type=int, default=None)
    parser.add_argument("--category-weights", default="", help="park=1,hospital=1,subway_station=1")
    parser.add_argument("--image-pool-dir", default="")
    parser.add_argument("--no-google-streetview", action="store_true", help="Disable Google Street View fetch (quality gate will fail image source)")
    parser.add_argument("--streetview-output-dir", default="")
    parser.add_argument("--streetview-radius-m", type=int, default=None)
    parser.add_argument("--no-llm-prompt", action="store_true", help="Use deterministic fallback prompt writer")
    parser.add_argument("--prompt-model-stage", default="")
    parser.add_argument("--prompt-temperature", type=float, default=None)
    parser.add_argument("--prompt-language", default="", help="Prompt language or 'auto' for city-local language")
    parser.add_argument("--city-language-map", default="", help="city=language,city=language, e.g. shanghai_smoke=zh-CN,tokyo_corridor_smoke=ja-JP")
    parser.add_argument("--review-translation-language", default="", help="Language used for human-review translations")
    parser.add_argument("--fail-on-llm-error", action="store_true")
    parser.add_argument("--config-yaml", default="")
    args = parser.parse_args()

    config = BatchGenerationConfig.from_json(args.config) if args.config else BatchGenerationConfig()
    if args.count is not None:
        config.count = args.count
    if args.seed is not None:
        config.seed = args.seed
    if args.output_root:
        config.output_root = args.output_root
    if args.run_id:
        config.run_id = args.run_id
    if args.cities:
        config.city_weights = {city.strip(): 1.0 for city in args.cities.split(",") if city.strip()}
    if args.city_weights:
        config.city_weights = parse_weight_map(args.city_weights, DEFAULT_CITY_WEIGHTS)
    if args.difficulty_weights:
        print("[GroundingBatch] warning: --difficulty-weights is deprecated and ignored")
    if args.quality_min_ambiguity is not None:
        config.quality_min_ambiguity = args.quality_min_ambiguity
    if args.quality_max_ambiguity is not None:
        config.quality_max_ambiguity = args.quality_max_ambiguity
    if args.category_weights:
        config.category_weights = parse_weight_map(args.category_weights, config.category_weights)
    if args.image_pool_dir:
        config.image_pool_dir = args.image_pool_dir
    if args.no_google_streetview:
        config.use_google_streetview = False
    if args.streetview_output_dir:
        config.streetview_output_dir = args.streetview_output_dir
    if args.streetview_radius_m is not None:
        config.streetview_radius_m = args.streetview_radius_m
    if args.no_llm_prompt:
        config.use_llm_prompt = False
    if args.prompt_model_stage:
        config.prompt_model_stage = args.prompt_model_stage
    if args.prompt_temperature is not None:
        config.prompt_temperature = args.prompt_temperature
    if args.prompt_language:
        config.prompt_language = args.prompt_language
    if args.city_language_map:
        config.city_language_map = parse_string_map(args.city_language_map, DEFAULT_CITY_LANGUAGE_MAP)
    if args.review_translation_language:
        config.review_translation_language = args.review_translation_language
    if args.fail_on_llm_error:
        config.fail_on_llm_error = True
    if args.config_yaml:
        config.config_yaml = args.config_yaml

    report = generate_batch(config)
    print(f"[GroundingBatch] generated {report['count']} drafts")
    print(f"[GroundingBatch] passed quality: {report['passed_count']}/{report['count']}")
    print(f"[GroundingBatch] output: {report['output_dir']}")
    for path in report.get("curated_paths", []):
        print(f"[GroundingBatch] curated: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
