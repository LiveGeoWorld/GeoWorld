"""Canonical on-disk layout for managed benchmark task batches.

The directory path is a routing boundary, while provenance sidecars retain the
immutable generation facts.  A batch is always an opaque directory bundle:
callers must move it as a whole and must never rebuild it from ``task.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator


def _segment(value: str, label: str) -> str:
    text = str(value or "").strip()
    if not text or text in {".", ".."} or "/" in text or "\\" in text:
        raise ValueError(f"Invalid {label}: {value!r}")
    return text


def city_scope_dir(root: str | Path, task_type: str, world_id: str, city_scope: str) -> Path:
    """Return ``<root>/<type>/<world>/<city>`` for a managed task scope."""
    return (
        Path(root)
        / _segment(task_type, "task type")
        / _segment(world_id, "world id")
        / _segment(city_scope, "city scope")
    )


def batch_dir(root: str | Path, task_type: str, world_id: str, city_scope: str, batch_id: str) -> Path:
    return city_scope_dir(root, task_type, world_id, city_scope) / _segment(batch_id, "batch id")


def task_dirs(batch: Path) -> list[Path]:
    root = batch / "tasks"
    if not root.is_dir():
        return []
    return sorted(
        entry for entry in root.iterdir()
        if entry.is_dir() and not entry.name.startswith("_") and (entry / "task.json").is_file()
    )


def iter_batches(root: str | Path, *, task_type: str = "", world_id: str = "", city_scope: str = "") -> Iterator[Path]:
    """Yield canonical managed batch directories, never legacy folders."""
    base = Path(root)
    type_dirs = [base / task_type] if task_type else sorted(base.iterdir()) if base.is_dir() else []
    for type_dir in type_dirs:
        if not type_dir.is_dir() or type_dir.name.startswith("_"):
            continue
        world_dirs = [type_dir / world_id] if world_id else sorted(type_dir.iterdir())
        for world_dir in world_dirs:
            if not world_dir.is_dir() or world_dir.name.startswith("_"):
                continue
            city_dirs = [world_dir / city_scope] if city_scope else sorted(world_dir.iterdir())
            for city_dir in city_dirs:
                if not city_dir.is_dir() or city_dir.name.startswith("_"):
                    continue
                for candidate in sorted(city_dir.iterdir()):
                    if candidate.name == "latest" or candidate.name.startswith("_") or not candidate.is_dir():
                        continue
                    if task_dirs(candidate):
                        yield candidate


def latest_batch(root: str | Path, task_type: str, world_id: str, city_scope: str) -> Path | None:
    """Return the newest managed batch for one world/type/city scope.

    ``latest`` is an optional convenience link.  It is unavailable on many
    Windows workspaces, so the non-link path must use the immutable recorded
    creation time rather than the opaque batch directory name.
    """
    city_dir = city_scope_dir(root, task_type, world_id, city_scope)
    link = city_dir / "latest"
    if link.is_dir() and task_dirs(link):
        return link.resolve()
    batches = [candidate for candidate in iter_batches(root, task_type=task_type, world_id=world_id, city_scope=city_scope)]
    return max(batches, key=_batch_sort_key) if batches else None


def _batch_sort_key(batch: Path) -> tuple[str, str]:
    """Match the formal batch ordering used by audit and evaluation readers."""
    created_at = ""
    for name in ("batch_metadata.json", "world_provenance.json"):
        path = batch / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("created_at"):
            created_at = str(payload["created_at"])
            break
    if not created_at:
        created_at = str(batch.stat().st_mtime_ns)
    return created_at, batch.name
