#!/usr/bin/env python3
"""CLI entry point for character generation.

Usage:
    # Production mode (LLM)
    python3 -m world_module.character.cli \\
        --world outputs/mapseeds/tokyo/complex/world.json \\
        --count 20

    # Demo mode (no LLM, validation only)
    python3 -m world_module.character.cli \\
        --world outputs/mapseeds/tokyo/complex/world.json \\
        --demo --count 5

    # Custom output path
    python3 -m world_module.character.cli \\
        --world outputs/mapseeds/tokyo/complex/world.json \\
        --count 30 --seed 100 --out ./my_chars.json

    # Re-generate relationships only
    python3 -m world_module.character.cli \\
        --world outputs/mapseeds/tokyo/complex/world.json \\
        --rel-only \\
        --input outputs/characters/tokyo_complex/complex/characters.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure repo root on path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from world_module.character.config import CharacterGeneratorConfig
from world_module.character.llm_generator import LLMGenerator
from world_module.character.demo_generator import DemoGenerator


DEFAULT_CONFIG_YAML = "config.yaml"
DEFAULT_OUTPUT_ROOT = "outputs/characters"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate virtual characters for a city world.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Production mode (LLM)
  python -m world_module.character.cli --world outputs/mapseeds/tokyo/complex/world.json --count 20

  # Demo mode (no LLM)
  python -m world_module.character.cli --world outputs/mapseeds/tokyo/complex/world.json --demo --count 5

  # Custom output
  python -m world_module.character.cli --world outputs/mapseeds/tokyo/complex/world.json --count 30 --out ./my_chars.json
""",
    )

    parser.add_argument(
        "--world", required=True,
        help="Path to world.json (required — provides city context and district names).",
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_YAML,
        help=f"Path to config.yaml with LLM API keys. Default: {DEFAULT_CONFIG_YAML}",
    )
    parser.add_argument(
        "--stage-model", default="character_generation",
        help="config.yaml stage_models key for character generation.",
    )
    parser.add_argument(
        "--count", type=int, default=15,
        help="Number of characters to generate. Default: 15.",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed (for future use). Default: 42.",
    )
    parser.add_argument(
        "--out", default="",
        help="Custom output path. Default: auto (outputs/characters/{city}/{variant}/).",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Run in demo mode (no LLM, fixed templates for validation).",
    )
    parser.add_argument(
        "--rel-only", action="store_true",
        help="Re-generate relationships only from existing characters.json (skip Steps 1-3).",
    )
    parser.add_argument(
        "--input", default="",
        help="Path to existing characters.json for --rel-only mode.",
    )
    parser.add_argument(
        "--exclude-characters", default="",
        help="Existing characters.json whose names must not be generated in this supplemental batch.",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # Validate world.json exists (skip for rel-only if input is given)
    world_path = Path(args.world)
    if not world_path.exists() and not args.rel_only:
        print(f"Error: world.json not found: {world_path}", file=sys.stderr)
        print("Generate the world first:", file=sys.stderr)
        print("  python -m world_module.map.cli --seed seeds/tokyo_complex_seed.json", file=sys.stderr)
        return 1

    # Build config
    excluded_names: list[str] = []
    if args.exclude_characters:
        try:
            from shared.io import read_json
            excluded_names = [
                str(item.get("name", "")).strip()
                for item in read_json(args.exclude_characters).get("characters", [])
                if isinstance(item, dict) and str(item.get("name", "")).strip()
            ]
        except Exception as exc:
            print(f"Error: cannot read --exclude-characters: {exc}", file=sys.stderr)
            return 1

    cfg = CharacterGeneratorConfig(
        world_path=str(world_path.resolve()) if world_path.exists() else "",
        config_yaml=args.config,
        count=args.count,
        seed=args.seed,
        output_root=args.out if args.out else DEFAULT_OUTPUT_ROOT,
        stage_model_key=args.stage_model,
        excluded_character_names=excluded_names,
    )

    # Run generator
    try:
        if args.rel_only:
            if not args.input:
                print("Error: --input is required with --rel-only", file=sys.stderr)
                return 1
            gen = LLMGenerator(cfg)
            gen.generate_relationships_only(args.input)
        elif args.demo:
            gen = DemoGenerator(cfg)
            gen.generate()
        else:
            gen = LLMGenerator(cfg)
            gen.generate()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
