"""Batch-local state used when a benchmark build appends tasks.

Construction phase only: this module never changes a finished task.  It
inspects the canonical ``tasks/`` folders so a later build can reserve new
indices and avoid reusing evidence sources already present in the batch.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.io import read_json, write_json


def task_directories(output_dir: str | Path) -> list[Path]:
    root = Path(output_dir) / "tasks"
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir() and path.name.startswith("task_"))


def existing_task_count(output_dir: str | Path) -> int:
    return len(task_directories(output_dir))


def incomplete_task_directories(output_dir: str | Path) -> list[Path]:
    """Return abandoned task folders which make an append unsafe."""
    return [path for path in task_directories(output_dir) if not (path / "task.json").is_file()]


def next_task_index(output_dir: str | Path) -> int:
    """Return the next monotonic task number; never fill historical gaps."""
    indices: list[int] = []
    # A user-withdrawn Dynamic task may no longer have a task directory,
    # while its immutable commit journal still reserves the old number.
    # Reusing that number would collide with commit_episode's journal.
    paths = task_directories(output_dir) + list(
        (Path(output_dir) / "generation_log" / "episode_commits").glob("task_*.json")
    )
    for path in paths:
        parts = path.name.split("_", 2)
        if len(parts) < 2:
            continue
        try:
            indices.append(int(parts[1]))
        except ValueError:
            continue
    return max(indices, default=0) + 1


def existing_gold_ids(output_dir: str | Path, field: str) -> set[str]:
    """Read approved/constructed evidence specs without depending on a manifest."""
    ids: set[str] = set()
    for task_dir in task_directories(output_dir):
        spec_path = task_dir / "task_spec.json"
        if not spec_path.is_file():
            continue
        try:
            spec = read_json(spec_path)
        except Exception:  # A partially written historical task is ignored here.
            continue
        value = _gold_id_from_spec(spec, field)
        if value:
            ids.add(str(value))
    return ids


def _gold_id_from_spec(spec: Any, field: str) -> str:
    """Resolve a task type's canonical Gold identifier from an evidence spec.

    Grounding records expose ``gold.place_id`` directly. Discovery evaluates a
    ranked Top-3 list, so its canonical Top-1 service lives in
    ``gold.candidates[0].service_id`` rather than a flat ``gold.service_id``.
    """
    if not isinstance(spec, dict):
        return ""
    gold = spec.get("gold") or {}
    if not isinstance(gold, dict):
        return ""
    direct = gold.get(field, "")
    if direct:
        return str(direct)
    if field == "service_id":
        candidates = gold.get("candidates") or []
        if isinstance(candidates, list):
            ranked = sorted(
                (candidate for candidate in candidates if isinstance(candidate, dict)),
                key=lambda candidate: int(candidate.get("rank", 9999)),
            )
            for candidate in ranked:
                service_id = candidate.get("service_id", "")
                if service_id:
                    return str(service_id)
        acceptable_top1 = gold.get("acceptable_top1") or []
        if isinstance(acceptable_top1, list) and acceptable_top1:
            return str(acceptable_top1[0])
    return ""


def ensure_batch_seed(output_dir: str | Path, preferred_seed: int | None = None) -> int:
    """Create one random batch seed once, then reuse the persisted value."""
    path = Path(output_dir) / "batch_metadata.json"
    metadata = read_json(path) if path.is_file() else {}
    existing = metadata.get("batch_seed") if isinstance(metadata, dict) else None
    if isinstance(existing, int) and existing > 0:
        return existing
    if not isinstance(metadata, dict):
        metadata = {}
    seed = int(preferred_seed or 0) or (secrets.randbits(63) or 1)
    metadata["batch_seed"] = seed
    write_json(path, metadata)
    return seed


def _derived_seed(domain: str, *parts: Any) -> int:
    payload = "|".join([domain, *(str(part) for part in parts)])
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


def task_seed(batch_seed_value: int, world_id: str, city_id: str, task_type: str, task_index: int) -> int:
    """Derive a stable independent seed for one formal task slot."""
    return _derived_seed(
        "geoword.task-seed.v1", batch_seed_value, world_id, city_id, task_type, task_index,
    )


def attempt_seed(task_seed_value: int, attempt_index: int) -> int:
    """Derive a stable retry stream without affecting another task slot."""
    return _derived_seed("geoword.attempt-seed.v1", task_seed_value, attempt_index)


def append_seed(base_seed: int, output_dir: str | Path) -> int:
    """Legacy compatibility alias; new construction uses persisted task seeds."""
    return ensure_batch_seed(output_dir)


def batch_seed(base_seed: int, output_dir: str | Path) -> int:
    """Legacy compatibility alias for the persisted random batch seed."""
    return ensure_batch_seed(output_dir)


def record_generation_run(output_dir: str | Path, run: dict[str, Any]) -> None:
    """Append immutable construction provenance for this batch run."""
    path = Path(output_dir) / "generation_runs.json"
    history: list[dict[str, Any]] = []
    if path.is_file():
        try:
            existing = read_json(path)
            if isinstance(existing, list):
                history = existing
        except Exception:
            # Preserve the current build's provenance even if a legacy sidecar
            # was malformed; do not overwrite the primary task artifacts.
            history = []
    history.append({"recorded_at": datetime.now(timezone.utc).isoformat(), **run})
    write_json(path, history)
