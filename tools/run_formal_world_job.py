"""Run one auditable World Studio construction job outside an interactive shell.

The World Studio runner keeps its job orchestration in daemon threads. This
wrapper stays alive until that runner reaches a terminal state, so a formal
validation build cannot be left as a stale "running" record when its invoking
terminal closes.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

from vis import server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", required=True)
    parser.add_argument("--world-id", required=True)
    parser.add_argument("--geo-provider", default="auto")
    parser.add_argument("--expansion-city-id", default="")
    parser.add_argument("--targets", required=True, help="JSON cumulative targets")
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    args = parser.parse_args()

    targets = json.loads(args.targets)
    job_id = server._start_generation(
        args.seed,
        args.geo_provider,
        False,
        args.world_id,
        args.expansion_city_id,
        targets if args.expansion_city_id else None,
        None if args.expansion_city_id else targets,
    )
    print(json.dumps({"event": "job_started", "job_id": job_id}, ensure_ascii=False), flush=True)

    deadline = time.monotonic() + args.timeout_seconds
    last_state: tuple[str | None, str | None] | None = None
    while time.monotonic() < deadline:
        job = server.JOBS[job_id]
        state = (job.get("status"), job.get("phase"))
        if state != last_state:
            print(json.dumps({"event": "job_state", "job_id": job_id,
                              "status": state[0], "phase": state[1]}, ensure_ascii=False), flush=True)
            last_state = state
        if job.get("status") not in {"pending", "running"}:
            print(json.dumps({"event": "job_finished", "job": server._job_response(job)},
                             ensure_ascii=False), flush=True)
            return 0 if job.get("status") == "completed" else 1
        time.sleep(2)

    job = server.JOBS[job_id]
    server._cancel_world_job(job_id)
    print(json.dumps({"event": "job_timeout_cancel_requested", "job_id": job_id},
                     ensure_ascii=False), flush=True)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
