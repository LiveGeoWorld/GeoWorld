"""Task-local evidence asset materialization."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from shared.io import read_json, write_json


_JSON_FILES = ("task.json", "task_spec.json", "curated_source.json", "spec_provenance.json")
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _as_source_path(value: str) -> Path | None:
    if not value or "batch_assets" not in value.replace("\\", "/"):
        return None
    raw = Path(value)
    candidate = raw if raw.is_absolute() else _workspace_root() / raw
    try:
        resolved = candidate.resolve()
        resolved.relative_to(_workspace_root())
    except ValueError:
        return None
    return resolved if resolved.is_file() and resolved.suffix.lower() in _IMAGE_SUFFIXES else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rewrite(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _rewrite(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite(item, replacements) for item in value]
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        return replacements.get(normalized, value)
    return value


def localize_task_assets(task_dir: Path) -> dict[str, Any]:
    """Copy legacy shared Street View assets into one task and rewrite JSON paths."""
    task_dir = task_dir.resolve()
    replacements: dict[str, str] = {}
    records: list[dict[str, str]] = []
    asset_root = task_dir / "assets" / "streetview"
    for json_path in (task_dir / name for name in _JSON_FILES):
        if not json_path.is_file():
            continue
        try:
            payload = read_json(json_path)
        except Exception:
            continue
        for source_value in _string_values(payload):
            source = _as_source_path(source_value)
            if source is None:
                continue
            relative = source.relative_to(_workspace_root()).as_posix()
            if relative in replacements:
                continue
            target = asset_root / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(source, target)
            local = target.relative_to(task_dir).as_posix()
            replacements[relative] = local
            replacements[str(source).replace("\\", "/")] = local
            records.append({"source_path": relative, "task_path": local, "sha256": _sha256(target)})
    if not replacements:
        return {"localized": 0, "assets": []}
    for json_path in (task_dir / name for name in _JSON_FILES):
        if json_path.is_file():
            try:
                write_json(json_path, _rewrite(read_json(json_path), replacements))
            except Exception:
                continue
    write_json(task_dir / "asset_provenance.json", {
        "schema_version": "geoword.task.asset_provenance.v1",
        "assets": records,
        "note": "Task-local copies preserve the original Google Street View bytes; source paths were rewritten only to make this task bundle self-contained.",
    })
    return {"localized": len(records), "assets": records}


def _string_values(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)
    elif isinstance(value, str):
        yield value
