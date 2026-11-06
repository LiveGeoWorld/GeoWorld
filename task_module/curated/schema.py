"""Schema helpers for curated task examples.

The curated layer keeps hand-authored benchmark examples structured before
they are exported into the existing task folder format.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SUPPORTED_TASK_TYPES = {"place_grounding", "place_discovery"}
TASK_SPECS_FILENAMES = ("task_specs.json", "curated.json")


class CuratedValidationError(ValueError):
    """Raised when a curated examples file is malformed."""


def pipeline_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_curated_dir() -> Path:
    return pipeline_root() / "task_module" / "task_types"


def has_curated_examples(
    task_type: str,
    curated_examples_dir: str | Path | None = None,
    city_id: str | None = None,
) -> bool:
    """Backward-compatible alias for has_task_specs."""
    return has_task_specs(task_type, curated_examples_dir, city_id=city_id)


def load_task_specs(
    task_type: str,
    specs_path: str | Path,
    city_id: str | None = None,
) -> dict[str, Any]:
    """Load a task specs bundle from an explicit file path."""
    path = Path(specs_path)
    if not path.is_file():
        raise CuratedValidationError(f"Task specs file not found: {path}")
    payload = _read_json(path)
    validate_curated_payload(payload, task_type, base_dir=path.parent)
    if city_id and payload.get("city_id") not in ("", city_id):
        raise CuratedValidationError(
            f"{path} is for city_id={payload.get('city_id')!r}, not {city_id!r}"
        )
    payload["_source_path"] = str(path)
    return payload


def has_task_specs(
    task_type: str,
    specs_dir: str | Path | None = None,
    city_id: str | None = None,
) -> bool:
    """Return True when a task specs bundle exists for the task type."""
    try:
        path = _resolve_examples_path(task_type, specs_dir)
    except CuratedValidationError:
        return False
    if not path.exists():
        return False
    if not city_id:
        return True
    try:
        payload = _read_json(path)
    except CuratedValidationError:
        return False
    return payload.get("city_id") in ("", city_id)


def load_curated_examples(
    task_type: str,
    curated_examples_dir: str | Path | None = None,
    city_id: str | None = None,
) -> dict[str, Any]:
    path = _resolve_examples_path(task_type, curated_examples_dir)
    payload = _read_json(path)
    validate_curated_payload(payload, task_type, base_dir=path.parent)
    if city_id and payload.get("city_id") not in ("", city_id):
        raise CuratedValidationError(
            f"{path} is for city_id={payload.get('city_id')!r}, not {city_id!r}"
        )
    payload["_source_path"] = str(path)
    return payload


def validate_curated_payload(
    payload: dict[str, Any],
    expected_task_type: str,
    base_dir: str | Path | None = None,
) -> None:
    if expected_task_type not in SUPPORTED_TASK_TYPES:
        raise CuratedValidationError(f"Unsupported curated task type: {expected_task_type}")
    if not isinstance(payload, dict):
        raise CuratedValidationError("Curated payload must be a JSON object")
    if payload.get("task_type") != expected_task_type:
        raise CuratedValidationError(
            f"Expected task_type={expected_task_type!r}, got {payload.get('task_type')!r}"
        )
    examples = payload.get("examples")
    if not isinstance(examples, list) or not examples:
        raise CuratedValidationError("Curated payload must contain a non-empty examples list")

    root = Path(base_dir) if base_dir else default_curated_dir()
    for index, example in enumerate(examples, start=1):
        _validate_common_example(example, index)
        if expected_task_type == "place_grounding":
            _validate_grounding_example(example, index, root)
        else:
            _validate_discovery_example(example, index)


def resolve_asset_path(path_value: str, base_dir: str | Path | None = None) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    root = Path(base_dir) if base_dir else pipeline_root()
    return (root / path).resolve()


def _validate_common_example(example: dict[str, Any], index: int) -> None:
    _require_str(example, "example_id", index)
    _require_str(example, "user_prompt", index)
    _require_str(example, "difficulty", index)
    if not isinstance(example.get("evaluation"), dict):
        raise CuratedValidationError(f"Example {index}: evaluation must be an object")


def _validate_grounding_example(example: dict[str, Any], index: int, base_dir: Path) -> None:
    _require_str(example, "image_path", index)
    image_path = resolve_asset_path(example["image_path"], base_dir=pipeline_root())
    if not image_path.exists():
        raise CuratedValidationError(f"Example {index}: image_path does not exist: {image_path}")
    if example.get("prompt_reference_image_path"):
        reference_path = resolve_asset_path(example["prompt_reference_image_path"], base_dir=pipeline_root())
        if not reference_path.exists():
            raise CuratedValidationError(
                f"Example {index}: prompt_reference_image_path does not exist: {reference_path}"
            )
        if reference_path == image_path:
            raise CuratedValidationError(
                f"Example {index}: prompt_reference_image_path must differ from image_path"
            )
    if example.get("virtual_map_entity"):
        raise CuratedValidationError(
            f"Example {index}: virtual_map_entity is not allowed; use a real MapApp candidate"
        )

    target_type = example.get("grounding_target_type")
    if target_type not in {"poi", "observer_location"}:
        raise CuratedValidationError(
            f"Example {index}: grounding_target_type must be 'poi' or 'observer_location'"
        )
    granularity = example.get("acceptable_granularity")
    if granularity not in {"exact_poi", "nearby_poi", "district_area"}:
        raise CuratedValidationError(
            f"Example {index}: acceptable_granularity must be exact_poi/nearby_poi/district_area"
        )

    _require_non_empty_list(example, "evidence_channels", index)
    _require_non_empty_list(example, "visual_clues", index)
    _require_non_empty_list(example, "text_clues", index)
    _require_list(example, "negative_clues", index)
    candidate_scope = example.get("candidate_scope")
    if not isinstance(candidate_scope, dict) or not candidate_scope:
        raise CuratedValidationError(f"Example {index}: candidate_scope must be a non-empty object")
    _require_str(candidate_scope, "city_id", index, prefix="candidate_scope")

    gold = example.get("gold")
    if not isinstance(gold, dict):
        raise CuratedValidationError(f"Example {index}: gold must be an object")
    _require_str(gold, "place_name", index, prefix="gold")
    _require_str(gold, "candidate_id", index, prefix="gold")
    _validate_coord(gold.get("coordinate"), index, "gold.coordinate")
    radius = gold.get("acceptable_radius_m")
    if radius is not None and (not isinstance(radius, int) or radius <= 0):
        raise CuratedValidationError(f"Example {index}: gold.acceptable_radius_m must be positive int")
    if granularity in {"nearby_poi", "district_area"} or target_type == "observer_location":
        if not isinstance(radius, int) or radius <= 0:
            raise CuratedValidationError(
                f"Example {index}: gold.acceptable_radius_m is required for {target_type}/{granularity}"
            )
    if not isinstance(gold.get("anchor_places"), list) or not gold["anchor_places"]:
        raise CuratedValidationError(f"Example {index}: gold.anchor_places must be a non-empty list")
    if target_type == "poi":
        _require_str(gold, "place_id", index, prefix="gold")
        if gold.get("candidate_id") != gold.get("place_id"):
            raise CuratedValidationError(f"Example {index}: poi gold.candidate_id must equal gold.place_id")
    if target_type == "observer_location" and not gold.get("candidate_id", "").startswith("map_location:"):
        raise CuratedValidationError(
            f"Example {index}: observer_location gold.candidate_id must be map_location:<character_id>"
        )

    constraints = example.get("grounding_constraints") or example.get("nearby_discovery_constraints")
    if not isinstance(constraints, list) or not constraints:
        raise CuratedValidationError(
            f"Example {index}: grounding_constraints must be a non-empty list"
        )


def _validate_discovery_example(example: dict[str, Any], index: int) -> None:
    start = example.get("start")
    if not isinstance(start, dict):
        raise CuratedValidationError(f"Example {index}: start must be an object")
    if start.get("coordinate") is not None:
        _validate_coord(start.get("coordinate"), index, "start.coordinate")

    gold = example.get("gold")
    if not isinstance(gold, dict):
        raise CuratedValidationError(f"Example {index}: gold must be an object")
    candidates = gold.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise CuratedValidationError(f"Example {index}: gold.candidates must be a non-empty list")
    for cand_index, candidate in enumerate(candidates, start=1):
        _require_str(candidate, "name", index, prefix=f"gold.candidates[{cand_index}]")
        _validate_coord(candidate.get("coordinate"), index, f"gold.candidates[{cand_index}].coordinate")
        if not isinstance(candidate.get("reasons"), list) or not candidate["reasons"]:
            raise CuratedValidationError(
                f"Example {index}: gold.candidates[{cand_index}].reasons must be non-empty"
            )

    if not isinstance(example.get("ranking_criteria"), list) or not example["ranking_criteria"]:
        raise CuratedValidationError(f"Example {index}: ranking_criteria must be a non-empty list")


def _validate_coord(value: Any, index: int, field: str) -> None:
    if not isinstance(value, dict):
        raise CuratedValidationError(f"Example {index}: {field} must be an object")
    lat = value.get("lat")
    lng = value.get("lng")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        raise CuratedValidationError(f"Example {index}: {field}.lat/lng must be numeric")
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise CuratedValidationError(f"Example {index}: {field} is outside valid lat/lng bounds")


def _require_str(obj: dict[str, Any], key: str, index: int, prefix: str = "") -> None:
    label = f"{prefix}.{key}" if prefix else key
    if not isinstance(obj.get(key), str) or not obj[key].strip():
        raise CuratedValidationError(f"Example {index}: {label} must be a non-empty string")


def _require_list(obj: dict[str, Any], key: str, index: int, prefix: str = "") -> None:
    label = f"{prefix}.{key}" if prefix else key
    if not isinstance(obj.get(key), list):
        raise CuratedValidationError(f"Example {index}: {label} must be a list")


def _require_non_empty_list(obj: dict[str, Any], key: str, index: int, prefix: str = "") -> None:
    _require_list(obj, key, index, prefix=prefix)
    label = f"{prefix}.{key}" if prefix else key
    if not obj.get(key):
        raise CuratedValidationError(f"Example {index}: {label} must be a non-empty list")


def resolve_specs_path_for_type(specs_path: str | Path, task_type: str) -> Path:
    """Resolve a specs bundle path for one task type."""
    path = Path(specs_path)
    if path.is_file():
        return path
    for filename in TASK_SPECS_FILENAMES:
        candidates = [
            path / task_type / "examples" / filename,
            path / task_type / filename,
            path / filename,
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
    return path / task_type / "examples" / TASK_SPECS_FILENAMES[0]


def _resolve_examples_path(task_type: str, curated_examples_dir: str | Path | None) -> Path:
    if task_type not in SUPPORTED_TASK_TYPES:
        raise CuratedValidationError(f"Unsupported curated task type: {task_type}")
    root = Path(curated_examples_dir) if curated_examples_dir else default_curated_dir()
    if root.is_file():
        return root
    candidates: list[Path] = []
    for filename in TASK_SPECS_FILENAMES:
        candidates.extend([
            root / task_type / "examples" / filename,
            root / task_type / filename,
            root / "examples" / filename,
            root / filename,
        ])
    candidates.append(root / f"{task_type}.json")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise CuratedValidationError(f"Curated examples file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise CuratedValidationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise CuratedValidationError(f"Curated examples file must contain a JSON object: {path}")
    return data
