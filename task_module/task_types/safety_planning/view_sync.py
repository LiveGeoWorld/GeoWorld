"""Recover task-local public views from committed generation history only."""

from datetime import datetime, timezone
from pathlib import Path

from shared.io import read_json, write_json
from .event_view import freeze_event_view


def sync_committed_views(output, index):
    output = Path(output)
    owners = {entry["owner"] for entry in index.entries}
    view = freeze_event_view(world_binding=index.world_binding, evidence_index=index)
    marker = output / "safety_view_sync.json"
    status = {"status": "pending", "content_hash": view["content_hash"],
              "committed_task_ids": sorted(owners), "completed_task_ids": [],
              "started_at": datetime.now(timezone.utc).isoformat()}
    write_json(marker, status)
    try:
        folders = {}
        for path in (output / "tasks").glob("*/task.json"):
            task = read_json(path)
            owner = task.get("task_id")
            if owner not in owners:
                continue
            if owner in folders:
                raise ValueError("Duplicate committed Safety task folder")
            folders[owner] = path.parent
        if set(folders) != owners:
            raise ValueError("Committed Safety history lacks its task folder")
        for owner in sorted(owners):
            write_json(folders[owner] / "safety_event_view.json", view)
            status["completed_task_ids"].append(owner)
            write_json(marker, status)
        status["status"] = "completed"
    except BaseException as exc:
        status.update(status="failed" if isinstance(exc, Exception) else "interrupted",
                      error_class=type(exc).__name__)
        raise
    finally:
        status["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(marker, status)
