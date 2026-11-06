"""Evidence package construction for grounding batch generation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from app.runtime.geo import distance_m
from task_module.task_types.place_grounding.batch_generation.config import pipeline_root
from task_module.task_types.place_grounding.batch_generation.sampler import SampledTarget
from task_module.task_types.place_grounding.batch_generation.streetview import (
    StreetViewRequest,
    fetch_streetview_assets,
)


def build_evidence_package(
    *,
    target: SampledTarget,
    world: dict[str, Any],
    image_pool_dir: str,
    index: int,
    use_google_streetview: bool = True,
    streetview_output_dir: str = "outputs/benchmarks/place_grounding/batch_assets/streetview",
    streetview_headings: list[int] | None = None,
    streetview_size: str = "640x640",
    streetview_fov: int = 80,
    streetview_pitch: int = 0,
    streetview_radius_m: int = 60,
    progress_callback: Callable[[str, str, str], None] | None = None,
) -> dict[str, Any]:
    """Build the evidence/provenance package used to write a prompt."""
    place = target.place
    loc = place.get("location", {})
    image_assets = _image_assets_for_target(
        target=target,
        place=place,
        loc=loc,
        image_pool_dir=image_pool_dir,
        index=index,
        use_google_streetview=use_google_streetview,
        streetview_output_dir=streetview_output_dir,
        streetview_headings=streetview_headings or [0, 90, 180, 270],
        streetview_size=streetview_size,
        streetview_fov=streetview_fov,
        streetview_pitch=streetview_pitch,
        streetview_radius_m=streetview_radius_m,
        progress_callback=progress_callback,
    )
    anchors = _nearby_anchors(loc, world, exclude_id=place.get("place_id", ""), exclude_name=place.get("name", ""))
    landscape = target.ambiguity_landscape
    place_evidence = _place_evidence(place, anchors, landscape)
    return {
        "image_assets": image_assets,
        "place_evidence": place_evidence,
        "ambiguity_landscape": landscape,
        "generation_notes": [
            "This draft is generated from Bench map fields and Google Street View evidence when available.",
            "Prompt-writing reference images must be distinct from the final evaluation image bound to image_path.",
            "StreetView recency, viewpoint quality, occlusion, and target consistency require manual approval.",
        ],
    }


def final_task_image_path(evidence: dict[str, Any]) -> str:
    """Shared image selection for authoring and task export; preserve existing order."""
    assets = evidence.get("image_assets", [])
    for asset in assets:
        if asset.get("role") in {"evaluation", "primary"} and asset.get("path") and asset.get("quality") != "fetch_failed":
            return asset["path"]
    for asset in assets:
        if asset.get("role") not in {"prompt_reference", "reference"} and asset.get("path") and asset.get("quality") != "fetch_failed":
            return asset["path"]
    return ""


def prompt_evidence_map_from_prompt_parts(prompt_parts: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        key: [f"place_evidence.{key}" for _item in values]
        for key, values in prompt_parts.items()
        if values
    }


def _choose_image_path(place: dict[str, Any], image_pool_dir: str, index: int) -> str:
    root = pipeline_root()
    pool = root / image_pool_dir
    if not pool.exists():
        return image_pool_dir
    images = sorted(
        [
            path
            for path in pool.iterdir()
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ]
    )
    if not images:
        return image_pool_dir
    name = str(place.get("name", ""))
    for path in images:
        if name and name in path.stem:
            return str(path.relative_to(root))
    return str(images[index % len(images)].relative_to(root))


def _image_assets_for_target(
    *,
    target: SampledTarget,
    place: dict[str, Any],
    loc: dict[str, Any],
    image_pool_dir: str,
    index: int,
    use_google_streetview: bool,
    streetview_output_dir: str,
    streetview_headings: list[int],
    streetview_size: str,
    streetview_fov: int,
    streetview_pitch: int,
    streetview_radius_m: int,
    progress_callback: Callable[[str, str, str], None] | None,
) -> list[dict[str, Any]]:
    target_id = place.get("place_id") or f"target_{index}"
    if use_google_streetview and loc.get("lat") is not None and loc.get("lng") is not None:
        return fetch_streetview_assets(
            StreetViewRequest(
                lat=float(loc["lat"]),
                lng=float(loc["lng"]),
                target_id=target_id,
                output_dir=streetview_output_dir,
                headings=streetview_headings,
                size=streetview_size,
                fov=streetview_fov,
                pitch=streetview_pitch,
                radius_m=streetview_radius_m,
            ),
            progress_callback=progress_callback,
        )
    return [
        {
            "role": "evaluation",
            "path": _choose_image_path(place, image_pool_dir, index),
            "source": "legacy_streetview_candidate_pool",
            "capture_date": "",
            "viewpoint": {},
            "quality": "legacy_pool_requires_manual_rebinding",
            "gold_coordinate": loc,
        }
    ]


def _nearby_anchors(
    loc: dict[str, Any],
    world: dict[str, Any],
    exclude_id: str = "",
    exclude_name: str = "",
    limit: int = 3,
) -> list[dict[str, Any]]:
    if not loc:
        return []
    ranked = []
    for place in world.get("places", []):
        if place.get("place_id") == exclude_id or not place.get("location"):
            continue
        if _same_name(place.get("name", ""), exclude_name):
            continue
        ranked.append((distance_m(loc, place.get("location", {})), place))
    ranked.sort(key=lambda item: item[0])
    return [_place_brief(place, loc, distance=round(distance)) for distance, place in ranked[:limit]]


def _place_evidence(
    place: dict[str, Any],
    anchors: list[dict[str, Any]],
    landscape: dict[str, Any],
) -> dict[str, Any]:
    projection = place.get("map_projection", {})
    confusable_names = [item.get("name", "") for item in landscape.get("confusable_candidates", [])]
    return {
        "place_id": place.get("place_id", ""),
        "name": place.get("name", ""),
        "address": place.get("address", ""),
        "primary_type": place.get("primary_type", ""),
        "types": projection.get("types", []),
        "district_id": place.get("district_id", ""),
        "city_id": place.get("city_id", ""),
        "location": place.get("location", {}),
        "nearby_anchors": anchors,
        "confusable_names": confusable_names,
        "allowed_for_prompt": [
            "address",
            "primary_type",
            "types",
            "district_id",
            "nearby_anchors",
            "confusable_names",
        ],
    }


def _place_brief(place: dict[str, Any], loc: dict[str, Any], distance: int | None = None) -> dict[str, Any]:
    value = {
        "place_id": place.get("place_id", ""),
        "name": place.get("name", ""),
        "type": place.get("primary_type", ""),
        "district_id": place.get("district_id", ""),
        "address": place.get("address", ""),
    }
    if distance is None and loc and place.get("location"):
        distance = round(distance_m(loc, place.get("location", {})))
    if distance is not None:
        value["distance_m"] = distance
    return value


def _same_name(a: str, b: str) -> bool:
    left = _norm_name(a)
    right = _norm_name(b)
    return bool(left and right and (left == right or left in right or right in left))


def _norm_name(value: str) -> str:
    return re.sub(r"[\s,，。.'’`\"()（）-]+", "", str(value or "").casefold())
