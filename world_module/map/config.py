"""Configuration for MapGenerator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class MapGeneratorConfig:
    """One-time configuration for the map generation pipeline."""

    # Required: path to config.yaml with API keys and model endpoints
    config_yaml: str = "config.yaml"

    # Geo enrichment provider
    #   "auto"   — try Google, fall back to OSM
    #   "google" — Google Maps only
    #   "osm"    — OpenStreetMap only (free, no API key needed)
    geo_provider: Literal["auto", "google", "osm"] = "auto"

    # Named config endpoint override. Empty means stage_models.world_discovery.
    discovery_model: str = ""

    # Parallel workers
    max_workers: int = 4
    discovery_max_workers: int = 4
    # Web-search requests are deliberately serialized by default: provider-side
    # search is long-running and unreliable under the normal discovery fan-out.
    discovery_web_search_max_workers: int = 1
    # These are per-request read budgets, never a whole-build deadline.
    discovery_timeout_seconds: int = 300
    discovery_web_search_timeout_seconds: int = 600
    discovery_max_attempts: int = 2
    # World Studio provides a job identifier so same-city attempts cannot
    # overwrite each other's discovery evidence.
    discovery_session_id: str = ""
    # Each user-requested continuation starts a new bounded repair batch.
    discovery_repair_batch: int = 1

    # Output root directory (products go under outputs/mapseeds/{city_id}/{variant}/)
    output_root: str = "./outputs/mapseeds"

    # Force rebuild (ignore cached intermediates)
    force: bool = False
