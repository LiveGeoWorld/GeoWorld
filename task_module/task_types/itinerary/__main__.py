"""python -m task_module.task_types.itinerary"""

from task_module.task_types.itinerary.generator import ItineraryGenerator
import argparse
import sys
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.io import read_json, write_json
from world_module.map._internal.factual_service_index import materialize_factual_services


def _resolve_world_path(city: str) -> str | None:
    """Try city + suffix × variant combinations to find world.json."""
    for suffix in ["", "_smoke", "_complex", "_large", "_base", "_corridor_smoke", "_review_smoke"]:
        for variant in ["complex", "base"]:
            p = f"outputs/mapseeds/{city}{suffix}/{variant}/world.json"
            if Path(p).exists():
                return p
    return None


def _resolve_chars_path(city: str) -> str | None:
    """Try city + suffix × variant combinations to find characters.json."""
    for suffix in ["", "_smoke", "_complex", "_large", "_base", "_corridor_smoke", "_review_smoke"]:
        for variant in ["complex", "base", "demo"]:
            p = f"outputs/characters/{city}{suffix}/{variant}/characters.json"
            if Path(p).exists():
                return p
        p = f"outputs/characters/{city}{suffix}/demo/characters_validation.json"
        if Path(p).exists():
            return p
    return None


def main():
    parser = argparse.ArgumentParser(description="Generate itinerary benchmark tasks")
    parser.add_argument("--city", default="tokyo", help="City seed ID (e.g. shanghai, tokyo, tokyo_complex)")
    parser.add_argument("--count", type=int, default=10, help="Number of tasks")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--output", default="", help="Custom output directory")
    parser.add_argument("--config", default="config.yaml", help="Config YAML path")
    parser.add_argument(
        "--stage-model-key", default="scenario_patch",
        help="Configured model endpoint used by construction roles and the task writer",
    )
    parser.add_argument(
        "--route-cache-only", action="store_true",
        help="Disable Google Routes and reject candidates whose required routes are not cached",
    )
    args = parser.parse_args()

    env_events = []
    judge_nodes = []

    world_path_str = _resolve_world_path(args.city)
    if not world_path_str:
        print(f"Error: no world.json found for city '{args.city}'")
        print("Tried: outputs/mapseeds/{city}[_smoke|_complex|...]/[base|complex]/world.json")
        print("Run map_module first to generate the world seed.")
        return 1

    chars_path_str = _resolve_chars_path(args.city)
    if not chars_path_str:
        print(f"Error: no characters.json found for city '{args.city}'")
        print("Run character_module first to generate characters.")
        return 1

    print(f"world: {world_path_str}")
    print(f"characters: {chars_path_str}")

    world = materialize_factual_services(read_json(world_path_str))
    characters = read_json(chars_path_str)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.output:
        output_dir = args.output
    else:
        output_dir = f"outputs/benchmarks/itinerary/{args.city}/{ts}_batch"

    generator = ItineraryGenerator(
        world=world, characters=characters,
        env_events=env_events, judge_nodes=judge_nodes,
        output_dir=output_dir, seed=args.seed,
        config_yaml=args.config,
        stage_model_key=args.stage_model_key,
        route_cache_only=args.route_cache_only,
    )

    tasks = generator.generate(count=args.count)
    print(f"\nGenerated {len(tasks)} tasks → {output_dir}/tasks/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
