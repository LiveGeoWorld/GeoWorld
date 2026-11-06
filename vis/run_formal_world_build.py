"""Run one World Studio production build and keep its in-process job alive."""

from __future__ import annotations

import argparse
import time

from vis import server


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "awaiting_registration_decision"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", required=True)
    parser.add_argument("--world-id", required=True)
    parser.add_argument("--geo-provider", default="auto")
    parser.add_argument("--expand-city-id", default="")
    parser.add_argument("--districts", type=int, required=True)
    parser.add_argument("--corridors", type=int, required=True)
    parser.add_argument("--places", type=int, required=True)
    parser.add_argument("--characters", type=int, required=True)
    parser.add_argument("--resume-job", default="", help="Resume a persisted formal World Studio job")
    args = parser.parse_args()
    targets = {
        "districts": args.districts,
        "corridors": args.corridors,
        "places": args.places,
        "characters": args.characters,
    }
    if args.resume_job:
        previous = server._load_persisted_job(args.resume_job)
        if previous is None:
            raise SystemExit(f"Persisted job does not exist: {args.resume_job}")
        job_id = server._start_resumed_generation(previous)
    else:
        job_id = server._start_generation(
            args.seed,
            args.geo_provider,
            True,
            args.world_id,
            args.expand_city_id,
            targets if args.expand_city_id else None,
            None if args.expand_city_id else targets,
        )
    print(f"FORMAL_WORLD_JOB={job_id}", flush=True)
    previous = None
    while True:
        job = server.JOBS[job_id]
        current = (job.get("progress") or {}).get("current") or {}
        marker = (job.get("status"), current.get("stage"), current.get("step"), current.get("label"), len(job.get("output") or []))
        if marker != previous:
            print("STATUS=" + str({
                "status": job.get("status"),
                "phase": job.get("phase"),
                "stage": current.get("stage"),
                "step": current.get("step"),
                "label": current.get("label"),
                "output_lines": len(job.get("output") or []),
            }), flush=True)
            if job.get("output"):
                print("LAST=" + job["output"][-1], flush=True)
            previous = marker
        if job.get("status") in TERMINAL_STATUSES:
            print("FORMAL_WORLD_FINAL=" + str({
                "job_id": job_id,
                "status": job.get("status"),
                "error": job.get("error"),
                "failure_detail": job.get("failure_detail"),
                "candidate_counts": job.get("candidate_counts"),
                "target_gaps": job.get("target_gaps"),
                "registration": job.get("registration"),
            }), flush=True)
            return 0 if job.get("status") == "completed" else 1
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
