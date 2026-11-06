"""CitySeed definition and loader for v2 world generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from pathlib import Path
from typing import Any

from shared.io import read_json
from shared.place_type_catalog import categories_for_query_types, default_category_ids, query_types_for_categories


@dataclass
class CitySeed:
    city_id: str
    city_name: str
    country: str
    center: dict[str, float]
    timezone: str
    language: str = "zh-CN"
    # Optional source-page titles for build-time regional evidence.  These are
    # provider-facing aliases only; city_id remains the storage identity.
    wikivoyage_titles: list[str] = field(default_factory=list)
    bbox: dict[str, float] | None = None
    district_count: int = 8
    corridor_count: int = 8
    places_per_district: int = 18
    # The exact number of formal POIs to retain after discovery and quality
    # filtering. Zero preserves compatibility with legacy seeds.
    target_place_count: int = 0
    # Geo search stops requesting further candidate batches once the provisional
    # in-bounds, de-duplicated pool reaches this multiple of the formal target.
    # The small buffer leaves the quality gate room to remove dirty POIs without
    # turning every build into an unbounded collection pass.
    poi_candidate_multiplier: float = 1.25
    # Expansion-only source identities already present in the active city
    # version. Geo enrichment excludes these before it counts candidate budget.
    # They are source ids, not generated place_ids, so re-running an unfinished
    # expansion cannot consume its new-POI target with the previous version.
    excluded_place_ids: list[str] = field(default_factory=list)
    search_radius_m: int = 1800
    google_max_workers: int = 6
    corridor_node_sample_limit: int = 3
    corridor_band_limit: int = 2
    corridor_place_types_per_band: int = 3
    corridor_text_queries_per_band: int = 2
    corridor_place_radius_m: int = 1200
    # Legacy input retained for old seed compatibility. Every retained Google
    # Place ID now receives the same factual Details request.
    google_place_details_limit: int = 120
    google_reviews_per_place: int = 3
    # Place data-quality gate: drop/flag dirty places before world build.
    validate_places: bool = True
    # Use reverse geocoding to verify address↔coordinate consistency
    # (Google only; ignored for the OSM provider).
    validate_place_geocode: bool = True
    llm_semantic_shards: bool = True
    llm_semantic_max_workers: int = 8
    llm_semantic_timeout_seconds: int = 120
    llm_district_place_limit: int = 18
    llm_corridor_place_limit: int = 18
    # ``place_categories`` is the user-selected World Studio scope.  The
    # matching Google query filters stay in ``place_types`` so discovery code
    # and historical seed files remain compatible.
    place_categories: list[str] = field(default_factory=list)
    place_types: list[str] = field(default_factory=list)
    notes: str = ""
    # Optional output-directory override for isolated repair batches.  The
    # stable city_id remains untouched so discovery checkpoints can be reused.
    output_variant: str = ""
    # Count validation must know whether this output is a complete first city,
    # an additive city batch, or a POI-only supplement.  Do not infer scope
    # from the requested counts: a valid expansion can intentionally add two
    # districts and must not be judged as an undersized complete city.
    quality_profile: str = "initial_city"

    def __post_init__(self) -> None:
        if self.place_categories:
            self.place_categories = list(dict.fromkeys(self.place_categories))
            self.place_types = query_types_for_categories(self.place_categories)
        elif self.place_types:
            # Historical seeds carry only raw Google query types.
            self.place_types = list(dict.fromkeys(self.place_types))
            self.place_categories = categories_for_query_types(self.place_types)
        else:
            self.place_categories = default_category_ids()
            self.place_types = query_types_for_categories(self.place_categories)

    @property
    def lat(self) -> float:
        return float(self.center["lat"])

    @property
    def lng(self) -> float:
        return float(self.center["lng"])

    @property
    def variant(self) -> str:
        """Derive variant name from city_id suffix."""
        if self.output_variant:
            return self.output_variant
        parts = self.city_id.split("_")
        if len(parts) >= 2 and parts[-1] in ("base", "large", "complex"):
            return parts[-1]
        return "base"

    @property
    def formal_place_target(self) -> int:
        """Return the requested formal POI count for this build."""
        return max(1, int(self.target_place_count or (self.district_count * self.places_per_district)))

    @property
    def poi_candidate_budget(self) -> int:
        """Return the bounded provisional POI pool size for geo retrieval."""
        multiplier = min(1.25, max(1.20, float(self.poi_candidate_multiplier or 1.25)))
        return max(self.formal_place_target, ceil(self.formal_place_target * multiplier))


def load_seed(path: str | Path) -> CitySeed:
    """Load a CitySeed from a JSON file."""
    data = read_json(path)
    required = {"city_id", "city_name", "country", "center", "timezone"}
    missing = sorted(required - set(data))
    if missing:
        raise ValueError(f"Seed missing required fields: {missing}")
    if "lat" not in data["center"] or "lng" not in data["center"]:
        raise ValueError("Seed center must contain lat and lng")
    # Historical seed files only contain raw Google query types.  Preserve
    # them exactly and derive their display categories at the UI boundary.
    return CitySeed(**data)


def seed_to_prompt_payload(seed: CitySeed) -> dict[str, Any]:
    """Minimal seed payload for LLM prompts."""
    return {
        "city_id": seed.city_id,
        "city_name": seed.city_name,
        "country": seed.country,
        "center": seed.center,
        "bbox": seed.bbox,
        "district_count": seed.district_count,
        "corridor_count": seed.corridor_count,
        "place_categories": seed.place_categories,
        "place_types": seed.place_types,
        "places_per_district": seed.places_per_district,
        "target_place_count": seed.target_place_count,
        "poi_candidate_multiplier": seed.poi_candidate_multiplier,
        "corridor_node_sample_limit": seed.corridor_node_sample_limit,
        "corridor_band_limit": seed.corridor_band_limit,
        "google_place_details_limit": seed.google_place_details_limit,
        "google_reviews_per_place": seed.google_reviews_per_place,
        "llm_semantic_shards": seed.llm_semantic_shards,
        "llm_semantic_max_workers": seed.llm_semantic_max_workers,
        "notes": seed.notes,
    }
