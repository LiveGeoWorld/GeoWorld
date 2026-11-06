"""MapGenerator — the main entry point for map-world generation.

Usage:
    from world_module.map import MapGenerator, MapGeneratorConfig

    config = MapGeneratorConfig(config_yaml="config.yaml")
    gen = MapGenerator(config)
    result = gen.generate("seeds/tokyo_complex_seed.json")
    # → outputs/mapseeds/tokyo_complex/complex/world.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Ensure repo root on path for vendored module compat
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.config_loader import PipelineConfig
from shared.io import read_json, write_json
from shared.maps_client import GoogleMapsClient
from shared.tool_index import build_world_tool_index

from world_module.map.config import MapGeneratorConfig
from world_module.map.seed import CitySeed, load_seed
from world_module.map._internal import place_quality
from world_module.map._internal.discovery import DiscoverySessionRunner
from world_module.map._internal.google_geo_enricher import GoogleGeoEnricher
from world_module.map._internal.open_geo_enricher import OpenStreetMapGeoEnricher
from world_module.map._internal.quality_gate import check_worldgen_quality
from world_module.map._internal.substrate_builder import build_world
from world_module.map._internal.factual_service_index import build_factual_service_index_artifact
from world_module.map._internal.yunwu_city_search import YunwuWebSearchDiscovery


def _emit_progress(stage: str, state: str, *, label: str = "", completed: int | None = None,
                   total: int | None = None) -> None:
    event = {"stage": stage, "state": state, "label": label}
    if completed is not None:
        event["completed"] = completed
    if total is not None:
        event["total"] = total
    print("[geoword-progress] " + json.dumps(event, ensure_ascii=False), flush=True)


@dataclass
class GenerateResult:
    world: dict[str, Any]
    world_path: str
    quality: dict[str, Any]
    tool_index_path: str
    factual_service_index_path: str
    manifest_path: str
    output_dir: str
    counts: dict[str, int]


class MapGenerator:
    """Generate a complete v7 city world from a CitySeed."""

    def __init__(self, config: MapGeneratorConfig | None = None):
        self.config = config or MapGeneratorConfig()

    # ── Main entry point ──

    def generate(
        self,
        seed_input: str | Path | CitySeed,
        *,
        force: bool = False,
    ) -> GenerateResult:
        """Generate a full world from a city seed.

        Args:
            seed_input: Path to seed JSON, or CitySeed object.
            force: If True, re-run discovery even if cached.

        Returns:
            GenerateResult with world dict, file paths, and quality report.
        """
        started_at = datetime.now(timezone.utc).isoformat()
        started_monotonic = time.monotonic()
        seed = seed_input if isinstance(seed_input, CitySeed) else load_seed(seed_input)

        # Determine output paths
        out_dir = Path(self.config.output_root) / seed.city_id / seed.variant
        out_dir.mkdir(parents=True, exist_ok=True)
        intermediates_dir = Path(self.config.output_root) / seed.city_id / "intermediates"
        intermediates_dir.mkdir(parents=True, exist_ok=True)

        world_path = out_dir / "world.json"
        quality_path = out_dir / "quality_report.json"
        tool_index_path = out_dir / "tool_index.json"
        factual_service_index_path = out_dir / "factual_service_index.json"
        manifest_path = out_dir / "map_manifest.json"
        latest_manifest_path = Path(self.config.output_root) / seed.city_id / "latest_map_manifest.json"

        # Stage 1: Discovery (LLM + web search)
        _emit_progress("map_discovery", "running", label="发现城市结构")
        discovery = self.discover(seed, force=force)
        _emit_progress("map_discovery", "completed", label="城市结构发现完成")

        # Stage 2: Geo enrichment (Google Maps / OSM)
        _emit_progress("geo_enrichment", "running", label="检索地理地点与道路")
        enriched = self.enrich(seed, discovery)
        _emit_progress("geo_enrichment", "completed", label="地理检索完成",
                       completed=len(enriched.get("places", [])), total=len(enriched.get("places", [])))

        # Stage 2.5: Place data-quality gate — drop/flag dirty places so they
        # never spawn services, information items, or events downstream.
        _emit_progress("place_quality", "running", label="校验与去重地点")
        enriched = self.clean_places(seed, enriched)
        enriched = self.supplement_places_to_target(seed, enriched)
        enriched = self.limit_places_to_target(seed, enriched)
        enriched = self.enrich_final_place_details(seed, enriched)
        place_quality_report = enriched.get("place_quality", {})
        _emit_progress("place_quality", "completed", label="地点质量校验完成",
                       completed=place_quality_report.get("kept"), total=place_quality_report.get("total"))

        # Stage 3: Build world
        _emit_progress("world_assembly", "running", label="装配城市世界")
        world = self.build(seed, enriched)
        from world_module.opening_hours_completion import complete_world_hours
        _emit_progress("opening_hours", "running", label="补全地点营业时间")
        world, service_cache = complete_world_hours(
            world, output_dir=out_dir, config_path=self.config.config_yaml,
            endpoint_name=self.config.discovery_model,
            progress_callback=lambda row: _emit_progress(
                "opening_hours", "running", label="补全地点营业时间",
                completed=row["completed"], total=row["total"]),
        )
        _emit_progress("opening_hours", "completed", label="营业时间补全完成")
        _emit_progress("world_assembly", "completed", label="城市世界装配完成")

        # Stage 4: Quality gate
        _emit_progress("map_quality", "running", label="执行地图质量门禁")
        quality = self.validate(seed, world)
        _emit_progress("map_quality", "completed" if quality.get("ok") else "failed",
                       label="地图质量门禁完成" if quality.get("ok") else "地图质量门禁失败")

        # Write outputs
        write_json(world_path, world)
        from world_module.opening_hours_completion import save_completed_service_cache
        save_completed_service_cache(world_path, service_cache)
        write_json(quality_path, quality)
        write_json(factual_service_index_path, build_factual_service_index_artifact(world))
        tool_index = build_world_tool_index(world)
        write_json(tool_index_path, tool_index)
        manifest = self._build_manifest(
            seed=seed,
            world_path=world_path,
            quality_path=quality_path,
            tool_index_path=tool_index_path,
            factual_service_index_path=factual_service_index_path,
            output_dir=out_dir,
            quality=quality,
            world=world,
        )
        manifest["operation_ledger"] = {
            "schema_version": "geoword.operation_ledger.v1", "operation": "map_generation",
            "started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(time.monotonic() - started_monotonic, 3),
            "geo_provider": enriched.get("geo_provider", self.config.geo_provider),
            "external_services": enriched.get("external_service_telemetry", {
                "cost_status": "not_observed", "money_estimate": None,
                "note": "No Google Maps client telemetry was available for this provider or fallback path.",
            }),
            "llm": discovery.get("operation_ledger", {"cost_status": "not_observed", "money_estimate": None,
                    "note": "No normalized LLM telemetry was available for this discovery operation."}),
            "poi_candidate_collection": enriched.get("poi_candidate_collection", {
                "status": "not_observed",
                "note": "The configured geo provider did not report bounded POI candidate collection.",
            }),
            "google_place_details": enriched.get("google_place_details_report", {
                "status": "not_observed",
                "note": "No final Google Place Details operation was recorded for this provider.",
            }),
            "opening_hours": read_json(out_dir / "opening_hours_summary.json"),
        }
        write_json(manifest_path, manifest)
        write_json(latest_manifest_path, manifest)

        if not quality.get("ok"):
            errors = quality.get("errors", [])
            raise RuntimeError(
                f"Generated world failed quality gate. See {quality_path}: {errors}"
            )

        counts = quality.get("counts", {})
        print(
            f"[MapGenerator] Generated world: {world_path}\n"
            f"  districts={counts.get('districts', 0)}, corridors={counts.get('corridors', 0)}, "
            f"places={counts.get('places', 0)}, services={counts.get('services', 0)}, "
            f"info={counts.get('information_items', 0)}, "
            f"events={counts.get('event_templates', 0)}"
        )

        return GenerateResult(
            world=world,
            world_path=str(world_path),
            quality=quality,
            tool_index_path=str(tool_index_path),
            factual_service_index_path=str(factual_service_index_path),
            manifest_path=str(manifest_path),
            output_dir=str(out_dir),
            counts=counts,
        )

    # ── Stage 1: Discovery ──

    def discover(
        self,
        seed: CitySeed,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Run discovery phase: LLM + web search to find city skeleton.

        Returns dict with: city_summary, district_candidates, corridor_candidates,
                           place_search_plan, dynamic_storylines
        """
        legacy_session_dir = (
            Path(self.config.output_root)
            / seed.city_id
            / "intermediates"
            / "discovery_session"
        )
        # Discovery artifacts must remain below the Windows path-length limit even
        # when an expansion itself lives beneath a deeply nested world directory.
        configured_session_id = "".join(
            char for char in str(getattr(self.config, "discovery_session_id", "") or "").strip()
            if char.isascii() and (char.isalnum() or char in {"_", "-"})
        )[:64]
        session_key = configured_session_id or hashlib.sha1(seed.city_id.encode("utf-8")).hexdigest()[:10]
        session_dir = Path(self.config.output_root) / "_d" / session_key
        if self.config.force or force:
            import shutil
            if session_dir.exists():
                shutil.rmtree(session_dir)
        elif not session_dir.exists() and legacy_session_dir.exists():
            # Preserve a recoverable legacy session such as the completed city
            # outline from an earlier failed expansion; leave its audit files intact.
            import shutil
            shutil.copytree(legacy_session_dir, session_dir)

        endpoint_name = self.config.discovery_model or self._pipeline_config().stage_model("world_discovery")
        endpoint = self._pipeline_config().model_endpoint(endpoint_name)
        discovery = DiscoverySessionRunner(
            model=endpoint.get("model", "gpt-5.5"),
            endpoint_name=endpoint_name,
            config_path=self._config_path(),
            max_workers=self.config.discovery_max_workers,
            web_search_max_workers=self.config.discovery_web_search_max_workers,
            timeout_seconds=self.config.discovery_timeout_seconds,
            web_search_timeout_seconds=self.config.discovery_web_search_timeout_seconds,
            max_attempts=self.config.discovery_max_attempts,
            force=self.config.force or force,
            structural_repair_batch=self.config.discovery_repair_batch,
        ).run(seed, session_dir)
        return discovery

    # ── Stage 2: Geo enrichment ──

    def enrich(
        self,
        seed: CitySeed,
        discovery: dict[str, Any],
    ) -> dict[str, Any]:
        """Run geo enrichment: Google Maps / OSM to validate coordinates and POIs.

        Returns dict with: seed, discovery, districts, corridors, places,
                           geo_provider
        """
        provider = self.config.geo_provider
        pipeline_config = self._pipeline_config()

        if provider == "osm":
            print("[geo] provider=osm", flush=True)
            return OpenStreetMapGeoEnricher(max_workers=seed.google_max_workers).enrich(seed, discovery)

        if provider == "google":
            print("[geo] provider=google", flush=True)
            self._google_enricher = GoogleGeoEnricher(config=pipeline_config)
            return self._google_enricher.enrich(seed, discovery, attach_place_details=False)

        # auto: try google first, fall back to osm
        print("[geo] provider=auto: trying google", flush=True)
        try:
            self._google_enricher = GoogleGeoEnricher(config=pipeline_config)
            enriched = self._google_enricher.enrich(seed, discovery, attach_place_details=False)
            enriched["geo_provider"] = "google"
            return enriched
        except Exception as exc:
            self._google_enricher = None
            print(f"[geo] google unavailable, fallback=osm ({type(exc).__name__}: {exc})", flush=True)
            return OpenStreetMapGeoEnricher(max_workers=seed.google_max_workers).enrich(seed, discovery)

    # ── Stage 2.5: Place data-quality gate ──

    def clean_places(
        self,
        seed: CitySeed,
        enriched: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate and de-duplicate enriched places before world build.

        Drops places that fail hard checks (no coordinates, outside bbox,
        snapped to city center, address↔coordinate mismatch, duplicate name at
        a far-apart location) and records soft flags on the survivors so the
        quality gate and downstream consumers can see them. Returns ``enriched``
        with a cleaned ``places`` list and an added ``place_quality`` report.
        """
        if not getattr(seed, "validate_places", True):
            return enriched
        places = enriched.get("places", [])
        if not places:
            return enriched

        candidates = []
        for index, place in enumerate(places):
            candidate = place_quality.candidate_from_summary(place)
            candidate.key = f"{index}:{candidate.key}"  # guarantee uniqueness
            candidates.append(candidate)

        reverse_fn = None
        geo_provider = enriched.get("geo_provider", self.config.geo_provider)
        if geo_provider == "google" and getattr(seed, "validate_place_geocode", True):
            try:
                client = GoogleMapsClient(config=self._pipeline_config())
                reverse_fn = place_quality.reverse_admin_via_maps_client(client, language=seed.language)
            except Exception as exc:  # pragma: no cover - defensive
                print(f"[place-quality] reverse geocode unavailable ({exc}); skipping geo check", flush=True)

        verdicts = place_quality.assess_places(
            candidates,
            bbox=seed.bbox,
            city_center=seed.center,
            reverse_admin_fn=reverse_fn,
        )

        kept: list[dict[str, Any]] = []
        for candidate, place in zip(candidates, places):
            verdict = verdicts[candidate.key]
            if verdict.keep:
                kept.append({**place, "quality_flags": list(verdict.flags)} if verdict.flags else place)

        report = place_quality.summarize_verdicts(verdicts)
        enriched["places"] = kept
        enriched["place_quality"] = report
        print(
            f"[place-quality] kept {report['kept']}/{report['total']} places "
            f"(dropped {report['dropped']}); flags={report['flag_counts']}",
            flush=True,
        )
        return enriched

    def limit_places_to_target(self, seed: CitySeed, enriched: dict[str, Any]) -> dict[str, Any]:
        """Keep exactly the requested formal POI count after quality filtering."""
        target = int(getattr(seed, "target_place_count", 0) or 0)
        places = list(enriched.get("places", []) or [])
        if target <= 0 or len(places) <= target:
            return enriched

        def score(place: dict[str, Any]) -> tuple[int, int, float, str]:
            flags = set(place.get("quality_flags", []) or [])
            return (
                0 if flags else 1,
                int(place.get("user_rating_count") or 0),
                float(place.get("rating") or 0),
                str(place.get("google_place_id") or place.get("name") or ""),
            )

        selected = sorted(places, key=score, reverse=True)[:target]
        report = enriched.setdefault("place_quality", {})
        report["target_selection"] = {
            "target": target,
            "candidates_after_quality": len(places),
            "retained": len(selected),
            "excluded": len(places) - len(selected),
        }
        enriched["places"] = selected
        print(f"[place-selection] retained {len(selected)}/{len(places)} formal POIs for target={target}", flush=True)
        return enriched

    def supplement_places_to_target(self, seed: CitySeed, enriched: dict[str, Any]) -> dict[str, Any]:
        """Perform at most two targeted Google top-up rounds after quality filtering.

        Normal collection still stops at the configured 1.20--1.25 candidate
        pool. This recovery path exists because a bounded raw pool can lose
        valid capacity to factual quality checks. A failed recovery remains a
        map-stage failure; no character or registration stage may consume it.
        """
        target = seed.formal_place_target
        places = list(enriched.get("places", []) or [])
        if len(places) >= target or enriched.get("geo_provider") not in {"google", None}:
            return enriched
        enricher = getattr(self, "_google_enricher", None)
        if enricher is None:
            return enriched

        search_plan = enriched.get("discovery", {}).get("place_search_plan", []) or []
        attempted_types = {
            str(place_type)
            for plan in search_plan
            for place_type in plan.get("included_types", [])
        }
        reports: list[dict[str, Any]] = []
        for round_number in range(1, 3):
            before = len(places)
            candidates, report = enricher.collect_targeted_place_supplement(
                seed,
                list(enriched.get("districts", []) or []),
                list(enriched.get("corridors", []) or []),
                places,
                attempted_types=attempted_types,
                round_number=round_number,
            )
            reports.append(report)
            attempted_types.update(report.get("attempted_types", []))
            if not candidates:
                break
            enriched["places"] = [*places, *candidates]
            enriched = self.clean_places(seed, enriched)
            places = list(enriched.get("places", []) or [])
            report["kept_after_quality"] = max(0, len(places) - before)
            report["remaining_after_quality"] = max(0, target - len(places))
            if len(places) >= target:
                break

        collection = enriched.setdefault("poi_candidate_collection", {})
        collection["targeted_supplement"] = reports
        collection["formal_target_met_after_supplement"] = len(places) >= target
        collection["shortfall_after_supplement"] = max(0, target - len(places))
        enriched["external_service_telemetry"] = enricher.maps.telemetry_snapshot()
        if len(places) < target:
            collection["shortfall_report"] = {
                "formal_target": target,
                "actual_after_quality": len(places),
                "remaining": target - len(places),
                "rounds_attempted": len(reports),
                "reason": "bounded_google_targeted_supplement_exhausted",
            }
            print(
                f"[poi-supplement] target not met after {len(reports)} rounds: {len(places)}/{target}",
                flush=True,
            )
        else:
            print(f"[poi-supplement] target met after {len(reports)} round(s): {len(places)}/{target}", flush=True)
        return enriched

    def enrich_final_place_details(self, seed: CitySeed, enriched: dict[str, Any]) -> dict[str, Any]:
        """Fetch Google details only after the formal POI set is fixed."""
        enricher = getattr(self, "_google_enricher", None)
        if enricher is None:
            return enriched
        enriched["places"] = enricher.attach_place_details(seed, list(enriched.get("places", []) or []))
        enriched["external_service_telemetry"] = enricher.maps.telemetry_snapshot()
        enriched["google_place_details_report"] = enricher.place_details_report()
        return enriched

    # ── Stage 3: Build world ──

    def build(
        self,
        seed: CitySeed,
        enriched: dict[str, Any],
    ) -> dict[str, Any]:
        """Assemble the v7 world from enriched data.

        Returns the map-world dict with cities, districts, corridors, places,
        factual POIs, source-backed information_items, and no static service state.
        """
        endpoint_name = self.config.discovery_model or self._pipeline_config().stage_model("world_discovery")
        endpoint = self._pipeline_config().model_endpoint(endpoint_name)
        return build_world(
            seed,
            enriched,
            model=endpoint.get("model", "gpt-5.5"),
            endpoint_name=endpoint_name,
            config_path=self._config_path(),
        )

    # ── Stage 4: Validate ──

    def validate(
        self,
        seed: CitySeed,
        world: dict[str, Any],
    ) -> dict[str, Any]:
        """Run quality gate on the generated world.

        Returns dict with: ok, errors, warnings, counts.
        """
        profile = str(getattr(seed, "quality_profile", "initial_city") or "initial_city")
        expected_places = seed.formal_place_target
        if profile == "poi_supplement":
            return check_worldgen_quality(
                world,
                profile=profile,
                expected_districts=None,
                expected_corridors=None,
                expected_places=expected_places,
            )
        return check_worldgen_quality(
            world,
            profile=profile,
            expected_districts=int(seed.district_count),
            expected_corridors=int(seed.corridor_count),
            expected_places=expected_places,
        )

    # ── Utilities ──

    def load_world(self, city_id: str, variant: str = "complex") -> dict[str, Any]:
        """Load a previously generated world from outputs/mapseeds/."""
        path = Path(self.config.output_root) / city_id / variant / "world.json"
        if not path.exists():
            raise FileNotFoundError(f"World not found: {path}")
        return read_json(path)

    def _config_path(self) -> str | None:
        return str(self.config.config_yaml) if self.config.config_yaml else None

    def _pipeline_config(self) -> PipelineConfig:
        return PipelineConfig(self.config.config_yaml) if self.config.config_yaml else PipelineConfig()

    def _build_manifest(
        self,
        *,
        seed: CitySeed,
        world_path: Path,
        quality_path: Path,
        tool_index_path: Path,
        factual_service_index_path: Path,
        output_dir: Path,
        quality: dict[str, Any],
        world: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a stable handoff manifest for downstream modules."""
        discovery_policy = world.get("generation_metadata", {}).get("discovery_policy")
        if discovery_policy is None:
            discovery_policy = (
                world.get("generation_metadata", {})
                .get("city_summary", {})
                .get("discovery_policy", {})
            )
        return {
            "artifact_type": "map_world_manifest",
            "schema_version": "pipeline_v2.map_manifest.v1",
            "city_id": seed.city_id,
            "city_name": seed.city_name,
            "variant": seed.variant,
            "geo_provider": self.config.geo_provider,
            "output_dir": str(output_dir),
            "paths": {
                "world": str(world_path),
                "quality_report": str(quality_path),
                "tool_index": str(tool_index_path),
                "factual_service_index": str(factual_service_index_path),
            },
            "seed": seed.__dict__,
            "counts": quality.get("counts", {}),
            "quality": {
                "ok": bool(quality.get("ok")),
                "errors": quality.get("errors", []),
                "warnings": quality.get("warnings", []),
            },
            "capabilities": {
                "map_world": True,
                "places": True,
                "services": True,
                "information_items": True,
                "event_templates": True,
                "characters": False,
                "benchmark_tasks": False,
                "full_routing_graph": False,
                "route_runtime": False,
            },
            "handoff_contract": {
                "character_module_input": "paths.world",
                "task_module_input": "paths.world",
                "vis_input": "paths.world",
                "notes": [
                    "Corridors are semantic city corridors, not a complete routing graph.",
                    "Use route_runtime_ref as the future integration point for exact ETA/path evaluation.",
                    "Person/character data must come from world_module.character, not world_module.map.",
                ],
            },
            "discovery_policy": discovery_policy,
        }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a map-world artifact from a CitySeed.")
    parser.add_argument("--seed", required=True, help="Path to CitySeed JSON.")
    parser.add_argument("--config", default="config.yaml", help="Path to pipeline config.yaml.")
    parser.add_argument("--geo-provider", choices=["auto", "google", "osm"], default="auto")
    parser.add_argument("--output-root", default="./outputs/mapseeds")
    parser.add_argument("--discovery-model", default="", help="Named config endpoint override for discovery.")
    parser.add_argument("--discovery-workers", type=int, default=4)
    parser.add_argument("--discovery-web-search-workers", type=int, default=1)
    parser.add_argument("--discovery-timeout", type=int, default=300,
                        help="Per-request read timeout for non-search discovery calls in seconds.")
    parser.add_argument("--discovery-web-search-timeout", type=int, default=600,
                        help="Per-request read timeout for Web Search discovery calls in seconds.")
    parser.add_argument("--discovery-max-attempts", type=int, default=2)
    parser.add_argument("--discovery-session-id", default="", help="Job-scoped discovery evidence directory name.")
    parser.add_argument("--discovery-repair-batch", type=int, default=1,
                        help="Structural repair batch number for a continuation.")
    parser.add_argument("--force", action="store_true", help="Rebuild discovery cache and outputs.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = MapGeneratorConfig(
        config_yaml=args.config,
        geo_provider=args.geo_provider,
        discovery_model=args.discovery_model,
        discovery_max_workers=args.discovery_workers,
        discovery_web_search_max_workers=args.discovery_web_search_workers,
        discovery_timeout_seconds=args.discovery_timeout,
        discovery_web_search_timeout_seconds=args.discovery_web_search_timeout,
        discovery_max_attempts=args.discovery_max_attempts,
        discovery_session_id=args.discovery_session_id,
        discovery_repair_batch=max(1, args.discovery_repair_batch),
        output_root=args.output_root,
        force=args.force,
    )
    result = MapGenerator(config).generate(args.seed, force=args.force)
    print(f"[MapGenerator] Manifest: {result.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
