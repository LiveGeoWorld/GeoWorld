"""python -m task_module.task_types.region_aware"""

from task_module.task_types.region_aware.generator import RegionAwareGenerator
import argparse, sys
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from shared.io import read_json
from task_module.task_types.region_aware.kb_manager import CulturalKB, list_available_cities


def _resolve_world_path(city: str) -> str | None:
    for suffix in ["", "_smoke", "_complex", "_large", "_base"]:
        for variant in ["complex", "base"]:
            p = f"outputs/mapseeds/{city}{suffix}/{variant}/world.json"
            if Path(p).exists(): return p
    return None

def _resolve_chars_path(city: str) -> str | None:
    for suffix in ["", "_smoke", "_complex", "_large", "_base"]:
        for variant in ["complex", "base", "demo"]:
            p = f"outputs/characters/{city}{suffix}/{variant}/characters.json"
            if Path(p).exists(): return p
    return None

def main():
    parser = argparse.ArgumentParser(description="Generate region_aware tasks")
    parser.add_argument("--city", default="shanghai", help="Map city ID (for world/characters)")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default="")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--admission-mode", default="prototype_validated",
                        choices=["prototype_validated", "audit_approved_only"],
                        help="原型可用自动校验候选；发布只允许最终审核通过的规则")
    parser.add_argument("--dynamic-source-ready", action="store_true",
                        help="Require a verified post-availability successor for Dynamic sourcing")
    parser.add_argument("--init-kb", action="store_true", help="Initialize KB for this city")
    parser.add_argument("--list-cities", action="store_true", help="List cities with KB")
    args = parser.parse_args()

    if args.list_cities:
        print("Cities with cultural KB:")
        for c in list_available_cities():
            kb = CulturalKB(c)
            print(f"  {c}: {kb.count_approved()} approved constraints in {len(kb.scenes)} scenes")
        return 0

    if args.init_kb:
        _init_city_kb(args.city)
        return 0

    wp = _resolve_world_path(args.city)
    if not wp: print(f"Error: no world.json for '{args.city}'"); return 1
    cp = _resolve_chars_path(args.city)
    if not cp: print(f"Error: no characters.json for '{args.city}'"); return 1
    print(f"world: {wp}\ncharacters: {cp}")

    world = read_json(wp)
    characters = read_json(cp)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = args.output or f"outputs/benchmarks/region_aware/{args.city}/{ts}_batch"

    gen = RegionAwareGenerator(world=world, characters=characters, env_events=[], judge_nodes=[],
                                output_dir=output_dir, seed=args.seed, config_yaml=args.config,
                                admission_mode=args.admission_mode,
                                dynamic_source_ready=args.dynamic_source_ready)
    tasks = gen.generate(count=args.count)
    print(f"\nGenerated {len(tasks)} tasks → {output_dir}/tasks/")
    return 0


def _init_city_kb(city: str):
    """Pre-populate KB with base cultural constraints for a city."""
    kb = CulturalKB(city)
    defaults = _BASE_CONSTRAINTS.get(city, [])
    for entry in defaults:
        kb.add_constraint(entry["scene"], entry["rule"], entry["type"], entry.get("time"), entry.get("severity", "hard"))
    kb.save()
    print(f"Initialized KB for {city}: {len(defaults)} constraints, {len(kb.scenes)} scenes")
    print("Constraints are draft. Review and approve via vis server.")


_BASE_CONSTRAINTS = {
    "tokyo_complex": [
        {"scene": "new_year", "type": "time_restriction", "rule": "初詣 1月1-3日神社拥挤，很多餐厅歇业",
         "time": {"mode": "date_range", "start": "01-01", "end": "01-03"}, "severity": "soft"},
        {"scene": "general", "type": "etiquette", "rule": "进入日式餐厅和民宿需脱鞋",
         "time": {"mode": "year_round"}, "severity": "soft"},
        {"scene": "summer_festival", "type": "time_restriction", "rule": "夏祭傍晚开始，白天无活动",
         "time": {"mode": "daily", "start": "17:00", "end": "22:00"}, "severity": "soft"},
    ],
    "shanghai_smoke": [
        {"scene": "spring_festival", "type": "time_restriction", "rule": "除夕至初三很多餐厅歇业",
         "time": {"mode": "date_range", "start": "01-28", "end": "01-31"}, "severity": "hard"},
        {"scene": "spring_festival", "type": "dietary", "rule": "除夕年夜饭需提前预订，当天临时不可得",
         "time": {"mode": "date_range", "start": "01-28", "end": "01-28"}, "severity": "hard"},
        {"scene": "general", "type": "etiquette", "rule": "商务宴请座次讲究主宾分坐，面门为上座",
         "time": {"mode": "year_round"}, "severity": "soft"},
    ],
    "london_smoke": [
        {"scene": "christmas", "type": "time_restriction", "rule": "12月25日全城公共交通停运，餐厅歇业",
         "time": {"mode": "date_range", "start": "12-25", "end": "12-25"}, "severity": "hard"},
        {"scene": "general", "type": "time_restriction", "rule": "周日很多店铺提前关门，营业时间缩短",
         "time": {"mode": "daily_weekly", "day": "sunday"}, "severity": "soft"},
        {"scene": "general", "type": "etiquette", "rule": "餐厅用餐通常加收12.5%服务费",
         "time": {"mode": "year_round"}, "severity": "soft"},
    ],
}

if __name__ == "__main__":
    raise SystemExit(main())
