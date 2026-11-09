"""CLI for evidence-preserved place_discovery batch generation."""

from __future__ import annotations

import argparse

from task_module.task_types.place_discovery.batch_generation.config import (
    DEFAULT_CITY_LANGUAGE_MAP,
    DEFAULT_CITY_WEIGHTS,
    DEFAULT_SERVICE_TYPE_WEIGHTS,
    DiscoveryBatchGenerationConfig,
    parse_string_map,
    parse_weight_map,
)
from task_module.task_types.place_discovery.batch_generation.pipeline import generate_batch


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate evidence-preserved place_discovery drafts.")
    parser.add_argument("--config", default="", help="Optional JSON config file")
    parser.add_argument("--count", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--output-root", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--cities", default="", help="Comma-separated city ids with uniform weights")
    parser.add_argument("--city-weights", default="", help="city=weight,city=weight")
    parser.add_argument("--service-type-weights", default="", help="restaurant_reservation=1,cafe_reservation=1")
    parser.add_argument("--difficulty-weights", default="", help="(deprecated) ignored; use search ambiguity filtering")
    parser.add_argument("--quality-min-search-ambiguity", type=int, default=None)
    parser.add_argument("--quality-max-search-ambiguity", type=int, default=None)
    parser.add_argument("--no-llm-prompt", action="store_true")
    parser.add_argument("--allow-deterministic-fallback", action="store_true",
                        help="Allow template fallback for local debugging only")
    parser.add_argument("--prompt-language", default="", help="Prompt language or 'auto' for city-local language")
    parser.add_argument("--city-language-map", default="")
    parser.add_argument("--review-translation-language", default="")
    parser.add_argument("--prompt-model-stage", default="")
    parser.add_argument("--prompt-temperature", type=float, default=None)
    parser.add_argument("--fail-on-llm-error", action="store_true")
    parser.add_argument("--config-yaml", default="")
    args = parser.parse_args()

    config = DiscoveryBatchGenerationConfig.from_json(args.config) if args.config else DiscoveryBatchGenerationConfig()
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
    if args.service_type_weights:
        config.service_type_weights = parse_weight_map(args.service_type_weights, DEFAULT_SERVICE_TYPE_WEIGHTS)
    if args.difficulty_weights:
        print("[DiscoveryBatch] warning: --difficulty-weights is deprecated and ignored")
    if args.quality_min_search_ambiguity is not None:
        config.quality_min_search_ambiguity = args.quality_min_search_ambiguity
    if args.quality_max_search_ambiguity is not None:
        config.quality_max_search_ambiguity = args.quality_max_search_ambiguity
    if args.no_llm_prompt:
        config.use_llm_prompt = False
    if args.allow_deterministic_fallback:
        config.allow_deterministic_fallback = True
    if args.prompt_language:
        config.prompt_language = args.prompt_language
    if args.city_language_map:
        config.city_language_map = parse_string_map(args.city_language_map, DEFAULT_CITY_LANGUAGE_MAP)
    if args.review_translation_language:
        config.review_translation_language = args.review_translation_language
    if args.prompt_model_stage:
        config.prompt_model_stage = args.prompt_model_stage
    if args.prompt_temperature is not None:
        config.prompt_temperature = args.prompt_temperature
    if args.fail_on_llm_error:
        config.fail_on_llm_error = True
    if args.config_yaml:
        config.config_yaml = args.config_yaml

    report = generate_batch(config)
    print(f"[DiscoveryBatch] generated {report['count']} drafts")
    print(f"[DiscoveryBatch] passed quality: {report['passed_count']}/{report['count']}")
    print(f"[DiscoveryBatch] output: {report['output_dir']}")
    for path in report.get("curated_paths", []):
        print(f"[DiscoveryBatch] curated: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
