"""JSON IO helpers for v7."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


_WRITE_LOCKS: dict[str, threading.Lock] = {}
_WRITE_LOCKS_GUARD = threading.Lock()


def _write_lock(path: Path) -> threading.Lock:
    """Return the process-local lock for one output path."""
    key = str(path.resolve()).casefold()
    with _WRITE_LOCKS_GUARD:
        return _WRITE_LOCKS.setdefault(key, threading.Lock())


def to_plain(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_plain(v) for v in obj]
    return obj


def write_json(path: Path | str, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Readers such as the World Studio progress poller must never observe a
    # partially written JSON document.  os.replace is atomic on one volume.
    # A progress writer and a build worker may persist the same artifact from
    # different threads in one process. A PID-only name would make them write
    # the same temporary file concurrently, corrupting otherwise atomic JSON.
    with _write_lock(path):
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.{os.getpid()}.", suffix=".tmp", dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(to_plain(payload), ensure_ascii=False, indent=2))
            # Windows can briefly deny replacement while a browser poller has
            # just opened the prior file. The retry is scoped to the atomic
            # final replacement and never exposes the temporary document.
            for attempt in range(4):
                try:
                    os.replace(temporary_path, path)
                    break
                except PermissionError:
                    if attempt == 3:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        finally:
            if temporary_path.exists():
                temporary_path.unlink()


def read_json(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    # On Windows, a reader can occasionally hold the previous file handle
    # while another process atomically replaces a high-frequency job-status
    # file. Retry the read itself, rather than exposing a transient parse
    # failure to the World Studio poller. A genuinely malformed artifact still
    # raises after this short, bounded window.
    last_error: OSError | json.JSONDecodeError | None = None
    for attempt in range(4):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt == 3:
                raise
            time.sleep(0.02 * (attempt + 1))
    assert last_error is not None
    raise last_error


def write_text(path: Path | str, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
