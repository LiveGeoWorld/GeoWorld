"""Visualization HTTP server — serves Leaflet map + REST API for world data."""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from copy import deepcopy
from http.server import ThreadingHTTPServer as HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse

# Ensure pipeline_v2 package root on path for direct script execution.
PIPELINE_ROOT = Path(__file__).resolve().parents[1]
if str(PIPELINE_ROOT) not in sys.path:
    sys.path.insert(0, str(PIPELINE_ROOT))

from shared.config_loader import PipelineConfig
from shared.io import read_json, write_json
from shared.place_type_catalog import place_type_catalog_payload
from shared.validator import validate_world
from world_module.map._internal.factual_service_index import materialize_factual_services, read_factual_service_index
from world_module.registry import (
    WORLD_ROOT, attach_city_artifact, create_empty_world, detach_city_from_world,
    city_version_context, migrate_city_versions, next_city_version,
    record_city_expansion, record_construction_attempt, select_city_version,
    set_city_lifecycle_status,
)
from world_module.cultural_assets import ensure_cultural_assets
from vis.renderer import (
    actors_to_geojson,
    character_detail,
    corridor_detail,
    corridors_to_geojson,
    districts_to_geojson,
    place_detail,
    places_to_geojson,
    stats,
    world_to_geojson_summary,
)

SEEDS_DIR = PIPELINE_ROOT / "seeds"
OUTPUTS_ROOT = PIPELINE_ROOT / "outputs" / "mapseeds"

# In-memory job registry for background generation tasks.
JOBS: dict[str, dict] = {}
# Subprocess objects are deliberately kept out of persisted job files. They
# exist only while this server owns a running build and enable safe cancellation.
JOB_PROCESSES: dict[str, dict[str, subprocess.Popen]] = {}
WORLD_JOB_LOG_ROOT = PIPELINE_ROOT / "outputs" / "logs" / "vis" / "world_jobs"
CITY_SEARCH_LOG_ROOT = PIPELINE_ROOT / "outputs" / "logs" / "vis" / "city_search"
MAX_AUTOMATIC_CHARACTER_GAP_REPAIRS = 2
# A candidate which narrowly misses its cumulative target is normally an
# incomplete retrieval/generation batch, not a user decision.  Give it two
# bounded, checkpoint-based top-up rounds before asking the user to decide.
MAX_AUTOMATIC_CANDIDATE_REPAIR_ROUNDS = 2

PROGRESS_STAGE_LABELS = {
    "map_discovery": "城市发现",
    "geo_enrichment": "地理检索",
    "place_quality": "地点质量校验",
    "world_assembly": "世界装配",
    "map_quality": "地图质量门禁",
    "character_generation": "人物与关系",
    "cultural_knowledge": "地域资料",
    "world_registration": "世界登记",
}
PROGRESS_STAGE_ORDER = tuple(PROGRESS_STAGE_LABELS)

# Progress event labels originate in independent Windows subprocesses.  The
# step ID is the durable protocol field, so it is the source of UI copy.
PROGRESS_STEP_LABELS = {
    "01_city_outline": "城市骨架",
    "02_districts": "区域组",
    "03_corridor_groups": "通道分组",
    "04_corridors": "通道组",
    "05_place_plans": "地点检索计划",
    "06_dynamic_storyline_seed": "动态线索",
    "structural_top_up": "城市结构自动补齐",
    "region_allocation": "人物区域分配",
    "profiles": "生成人物档案",
    "structure": "补全人物空间与行动结构",
    "relationships": "生成人物关系网络",
    "write": "写入人物产物",
}

PROGRESS_STAGE_STATE_LABELS = {
    "map_discovery": {
        "running": "正在发现城市结构",
        "completed": "城市结构发现完成",
        "failed": "城市发现失败",
    },
    "geo_enrichment": {
        "running": "正在检索地理地点与道路",
        "completed": "地理检索完成",
        "failed": "地理检索失败",
    },
    "place_quality": {
        "running": "正在校验地点质量",
        "completed": "地点质量校验完成",
        "failed": "地点质量校验失败",
    },
    "world_assembly": {
        "running": "正在装配城市世界",
        "completed": "城市世界装配完成",
        "failed": "城市世界装配失败",
    },
    "map_quality": {
        "running": "正在执行地图质量门禁",
        "completed": "地图质量门禁完成",
        "failed": "地图质量门禁失败",
    },
    "character_generation": {
        "running": "正在生成人物与关系",
        "completed": "人物与关系生成完成",
        "failed": "人物与关系生成失败",
        "not_run": "人物与关系未执行",
    },
    "cultural_knowledge": {
        "running": "正在补齐地域资料",
        "completed": "地域资料补齐完成",
        "failed": "地域资料补齐失败",
        "not_run": "地域资料未执行",
    },
    "world_registration": {
        "running": "正在登记世界产物",
        "completed": "世界产物登记完成",
        "failed": "世界产物登记失败",
        "not_run": "世界登记未执行",
    },
}


def _persist_world_job(job: dict) -> None:
    """Persist build timing and outcome without retaining credentials or raw requests."""
    WORLD_JOB_LOG_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {key: value for key, value in job.items() if key != "output"}
    payload["output_tail"] = job.get("output", [])[-80:]
    payload["elapsed_seconds"] = round((job.get("finished_at") or time.time()) - job["started_at"], 3)
    write_json(WORLD_JOB_LOG_ROOT / f"{job['id']}.json", payload)


def _artifact_checkpoint(path: Path) -> dict:
    """Capture a reusable local artifact without retaining payload contents."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": str(path.relative_to(PIPELINE_ROOT)),
        "sha256": digest.hexdigest(),
        "bytes": path.stat().st_size,
        "recorded_at": time.time(),
    }


def _set_job_checkpoint(job: dict, name: str, path: Path, *, stage: str) -> None:
    if not path.is_file():
        return
    job.setdefault("checkpoints", {})[name] = {"stage": stage, **_artifact_checkpoint(path)}
    _persist_world_job(job)


def _mark_stages_not_run(job: dict, *stages: str) -> None:
    """Mark downstream stages without replacing the actual failed stage."""
    progress = job.setdefault("progress", _new_job_progress())
    for stage in stages:
        entry = next((item for item in progress["stages"] if item["id"] == stage), None)
        if entry and entry.get("status") == "pending":
            entry["status"] = "not_run"
            entry["finished_at"] = time.time()
    _persist_world_job(job)


def _seed_wikivoyage_titles(seed: dict) -> list[str]:
    """Return optional source-page aliases without making them storage IDs."""
    values = seed.get("wikivoyage_titles", [])
    return [str(item).strip() for item in values if str(item).strip()] if isinstance(values, list) else []


def _cultural_assets_are_registerable(assets: dict | None) -> bool:
    """A completed regional stage requires usable evidence and candidate output."""
    return bool(
        assets
        and assets.get("status") == "available"
        and int(assets.get("evidence_cards", 0) or 0) > 0
        and assets.get("evidence_path")
        and assets.get("constraint_path")
    )


def _fail_for_cultural_assets(job: dict, assets: dict | None) -> None:
    """Apply the regional-asset registration gate consistently to every path."""
    details = assets or {}
    reason = str(details.get("reason", "regional_assets_invalid"))
    job["status"] = "failed"
    job["error"] = f"地域资料未完成：{reason}"
    job["failure_detail"] = job["error"]
    _update_job_progress(job, "cultural_knowledge", "failed", label="地域资料未完成", error=job["error"])
    _mark_stages_not_run(job, "world_registration")


def _new_job_progress() -> dict:
    return {
        "schema_version": "geoword.world_build_progress.v1",
        "stages": [{"id": stage, "label": label, "status": "pending"}
                   for stage, label in PROGRESS_STAGE_LABELS.items()],
        "current": {"stage": "map_discovery", "label": "等待城市发现开始"},
        "history": [],
    }


def _progress_label(stage: str, state: str, step: str, event_label: str) -> str:
    """Return deterministic UI copy without trusting child-process encoding."""
    if event_label.startswith("请求重试") or event_label.startswith("执行请求"):
        return event_label
    if step in PROGRESS_STEP_LABELS:
        return PROGRESS_STEP_LABELS[step]
    state_label = PROGRESS_STAGE_STATE_LABELS.get(stage, {}).get(state)
    if state_label:
        return state_label
    if event_label and "\ufffd" not in event_label:
        return event_label
    return PROGRESS_STAGE_LABELS.get(stage, "构建步骤")


def _normalize_progress(progress: dict) -> dict:
    """Normalize legacy persisted jobs for display without rewriting evidence."""
    normalized = deepcopy(progress)
    current = normalized.get("current")
    if isinstance(current, dict):
        stage = str(current.get("stage", ""))
        state = str(current.get("state", "running"))
        step = str(current.get("step", ""))
        current["stage_label"] = PROGRESS_STAGE_LABELS.get(stage, current.get("stage_label", "构建"))
        current["label"] = _progress_label(stage, state, step, str(current.get("label", "")))
    return normalized


def _update_job_progress(
    job: dict,
    stage: str,
    state: str,
    *,
    step: str = "",
    label: str = "",
    completed: int | None = None,
    total: int | None = None,
    error: str = "",
) -> None:
    """Record one generator-reported progress event and checkpoint the job."""
    if stage not in PROGRESS_STAGE_LABELS:
        return
    progress = job.setdefault("progress", _new_job_progress())
    stage_entry = next((item for item in progress["stages"] if item["id"] == stage), None)
    if stage_entry is None:
        return
    stage_entry["status"] = state
    if state == "running":
        stage_entry["started_at"] = stage_entry.get("started_at", time.time())
    if state in {"completed", "failed", "cancelled", "not_run"}:
        stage_entry["finished_at"] = time.time()
    resolved_label = _progress_label(stage, state, step, label)
    # A map stage before merge validates a delta batch for city expansion, not
    # a complete city.  Make that scope visible in the progress panel.
    if stage == "map_quality" and job.get("mode") == "incremental_expansion" and step != "poi_top_up":
        resolved_label = {
            "running": "正在校验扩增批次",
            "completed": "扩增批次校验完成",
            "failed": "扩增批次校验失败",
        }.get(state, resolved_label)
    current = {
        "stage": stage,
        "stage_label": PROGRESS_STAGE_LABELS[stage],
        "state": state,
        "step": step,
        "label": resolved_label,
        "updated_at": time.time(),
    }
    if completed is not None:
        current["completed"] = max(0, int(completed))
    if total is not None:
        current["total"] = max(0, int(total))
    if error:
        current["error"] = error
    progress["current"] = current
    event = {key: value for key, value in current.items() if key != "updated_at"}
    event["at"] = current["updated_at"]
    progress.setdefault("history", []).append(event)
    progress["history"] = progress["history"][-120:]
    _persist_world_job(job)


def _consume_progress_event(job: dict, line: str) -> bool:
    """Consume a machine-readable generator event; ordinary output remains log text."""
    marker = "[geoword-progress]"
    position = line.find(marker)
    if position < 0:
        return False
    try:
        event = json.loads(line[position + len(marker):].strip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    _update_job_progress(
        job, str(event.get("stage", "")), str(event.get("state", "running")),
        step=str(event.get("step", "")), label=str(event.get("label", "")),
        completed=event.get("completed"), total=event.get("total"), error=str(event.get("error", "")),
    )
    return True


def _summarize_job_failure(output: list[str]) -> str:
    """Expose a stable short failure reason while retaining the traceback in the log."""
    joined = "\n".join(output[-80:])
    if "TimeoutError" in joined or "read operation timed out" in joined.lower():
        if "[人物]" in joined and "relationship" in joined.lower():
            return "人物关系网络：LLM 响应读取超时"
        return "LLM 响应读取超时"
    if "ConnectionRefusedError" in joined:
        return "模型服务拒绝连接"
    if "authentication" in joined.lower() or "unauthorized" in joined.lower():
        return "模型服务鉴权失败"
    return "生成进程异常退出，请展开运行日志查看详情"


def _structural_shortfall_from_output(output: list[str]) -> dict:
    """Read the machine-readable structural shortfall from the map subprocess."""
    marker = "[geoword-structural-shortfall]"
    for line in reversed(output):
        position = line.find(marker)
        if position < 0:
            continue
        try:
            payload = json.loads(line[position + len(marker):].strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("report"), dict):
            return payload
    return {}


def _reusable_legacy_expansion_batch(job: dict) -> Path | None:
    """Return a batch rejected only by the obsolete complete-city floors.

    Earlier expansion jobs wrote their map before the generator raised for
    ``at least 3`` / ``at least 12``.  Reusing one is safe only when its actual
    delta exactly matches the recorded request and the report has no other
    errors.  This avoids paying for the same Google/LLM map stage twice.
    """
    if job.get("mode") != "incremental_expansion":
        return None
    plan = job.get("expansion_plan") or {}
    expected = plan.get("delta") or {}
    if not expected:
        return None
    map_root_value = str(job.get("map_output_root", "")).strip()
    if not map_root_value:
        return None
    raw_path = _find_generated_city_world(
        str(job.get("city_id", "")), PIPELINE_ROOT / map_root_value,
    )
    if raw_path is None:
        return None
    report_path = raw_path.parent / "quality_report.json"
    if not report_path.is_file():
        return None
    try:
        report = read_json(report_path)
        errors = list(report.get("errors") or [])
        counts = dict(report.get("counts") or {})
    except Exception:
        return None
    legacy_floor = re.compile(r"^Only \d+ (districts|corridors|places); expected at least \d+$")
    if not errors or not all(legacy_floor.match(str(error)) for error in errors):
        return None
    for key in ("districts", "corridors", "places"):
        if int(counts.get(key, -1) or -1) != int(expected.get(key, -2) or -2):
            return None
    return raw_path


def _job_response(job: dict) -> dict:
    """Return the same contract for in-memory and persisted build jobs."""
    output = job.get("output", [])
    output_tail = output[-60:] if output else job.get("output_tail", [])[-60:]
    progress = job.get("progress")
    failure_detail = job.get("failure_detail", "")
    if not progress:
        # Pre-progress jobs remain inspectable after deployment/restart.  This
        # is a display migration only; it never rewrites historical evidence.
        progress = _new_job_progress()
        if job.get("status") in {"failed", "partial"}:
            detail = failure_detail or _summarize_job_failure(output_tail)
            current = progress["current"]
            current.update({"state": "failed", "label": "城市骨架" if "01_city_outline" in "\n".join(output_tail) else "构建步骤", "error": detail})
            stage = next(item for item in progress["stages"] if item["id"] == "map_discovery")
            stage["status"] = "failed"
            failure_detail = detail
    progress = _normalize_progress(progress)
    response_status = job["status"]
    gate_report = job.get("gate_report", {})
    # Historical shortfall jobs predate the decision state.  Derive the same
    # report read-only so their candidate can be recovered or explicitly
    # registered without rewriting the original failure evidence.
    if response_status == "target_not_reached" and not gate_report:
        try:
            map_path, characters_path = _candidate_paths_from_job(job)
            if map_path and characters_path:
                gate_report = _candidate_gate_report(
                    read_json(map_path), read_json(characters_path), job.get("requested_targets") or {},
                )
                if not gate_report["hard_errors"]:
                    response_status = "awaiting_registration_decision"
        except Exception:
            gate_report = {}
    resume_plan = _resume_plan(job) if job.get("status") in {
        "failed", "cancelled", "target_not_reached", "awaiting_registration_decision",
    } else None
    if resume_plan:
        resume_plan = {
            key: str(value) if isinstance(value, Path) else value
            for key, value in resume_plan.items()
        }
    return {
        "id": job["id"], "status": response_status, "seed": job.get("seed", ""),
        "geo_provider": job.get("geo_provider", ""), "force": job.get("force", False),
        "discovery_mode": job.get("discovery_mode", "first_run"), "retry_of": job.get("retry_of", ""),
        "phase": job.get("phase", "map_generation"), "world_id": job.get("world_id", ""),
        "city_id": job.get("city_id", ""), "registration": job.get("registration", {}),
        "returncode": job.get("returncode"), "error": job.get("error"),
        "failure_detail": failure_detail, "progress": progress,
        "resume_plan": resume_plan,
        "gate_report": gate_report,
        "structural_repair_report": job.get("structural_repair_report", {}),
        "automatic_repairs": job.get("automatic_repairs", []),
        "automatic_repair_state": job.get("automatic_repair_state", {}),
        "batch_quality": job.get("batch_quality", {}),
        "merged_version_validation": job.get("merged_version_validation", {}),
        "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
        "elapsed_s": (job.get("finished_at") or time.time()) - job.get("started_at", time.time()),
        "output_tail": output_tail, "output_line_count": len(output) if output else len(output_tail),
    }


def _load_persisted_job(job_id: str) -> dict | None:
    path = WORLD_JOB_LOG_ROOT / f"{job_id}.json"
    return read_json(path) if path.is_file() else None


def _latest_world_job(world_id: str) -> dict | None:
    jobs = [job for job in JOBS.values() if job.get("world_id") == world_id]
    if jobs:
        return max(jobs, key=lambda item: item.get("started_at", 0))
    candidates: list[dict] = []
    if WORLD_JOB_LOG_ROOT.is_dir():
        for path in WORLD_JOB_LOG_ROOT.glob("job_*.json"):
            try:
                job = read_json(path)
                if job.get("world_id") == world_id:
                    candidates.append(job)
            except Exception:
                continue
    return max(candidates, key=lambda item: item.get("started_at", 0)) if candidates else None


def _cancel_world_job(job_id: str) -> dict:
    """Request cancellation of a live build without registering partial output."""
    job = JOBS.get(job_id)
    if not job:
        raise LookupError("只能停止当前服务仍在管理的构建任务。")
    if job.get("status") not in {"running", "cancelling"}:
        raise ValueError("该构建任务已结束，不能再次停止。")

    job["status"] = "cancelling"
    job["cancel_requested_at"] = time.time()
    job["cancellation"] = {
        "requested_at": job["cancel_requested_at"],
        "reason": "user_requested_parameter_change",
        "registration": "will_not_attach_partial_artifacts",
    }
    current = (job.get("progress") or {}).get("current", {})
    _update_job_progress(
        job, current.get("stage", "map_discovery"), "running",
        step=current.get("step", ""), label="正在停止本次构建",
        completed=current.get("completed"), total=current.get("total"),
    )
    for process in JOB_PROCESSES.get(job_id, {}).values():
        if process.poll() is None:
            process.terminate()
    return job


def _finalize_cancelled_world_job(job: dict) -> None:
    """Checkpoint a requested cancellation and retain only non-active artifacts."""
    current = (job.get("progress") or {}).get("current", {})
    job["status"] = "cancelled"
    job["phase"] = "cancelled"
    job["error"] = None
    job["failure_detail"] = "已按用户请求停止；未登记或启用本次候选版本。"
    job["registration"] = {
        "status": "not_attached",
        "world_id": job.get("world_id", ""),
        "reason": "cancelled_by_user",
    }
    job.setdefault("cancellation", {})["completed_at"] = time.time()
    _update_job_progress(
        job, current.get("stage", "map_discovery"), "cancelled",
        step=current.get("step", ""), label="本次构建已停止",
        completed=current.get("completed"), total=current.get("total"),
    )
    if job.get("expansion_id"):
        try:
            record_construction_attempt(
                job["world_id"], job["city_id"], artifact_type="city_expansion", status="cancelled",
                details={
                    "job_id": job["id"], "phase": current.get("stage", "map_discovery"),
                    "requested_targets": job.get("requested_targets", {}),
                    "reason": "user_requested_parameter_change",
                    "subprocess_elapsed_seconds": round(time.time() - job["started_at"], 3),
                },
            )
        except Exception as exc:
            job["cancellation"]["audit_record_error"] = str(exc)


def _record_city_search(event: dict) -> None:
    """Persist search provenance without retaining an API key or raw response."""
    CITY_SEARCH_LOG_ROOT.mkdir(parents=True, exist_ok=True)
    event_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    write_json(CITY_SEARCH_LOG_ROOT / f"{event_id}.json", {
        "schema_version": "geoword.city_search.v1",
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cost_status": "provider_billed_price_not_inferred",
        **event,
    })


def _list_worlds() -> list[dict]:
    """List registered world cities first, then unregistered legacy map worlds."""
    worlds: list[dict] = []
    registered_paths: set[str] = set()
    for registry_path in sorted(WORLD_ROOT.glob("*/world_manifest.json")):
        try:
            registry = read_json(registry_path)
            world_id = registry.get("world_id", registry_path.parent.name)
            migrate_city_versions(world_id)
            registry = read_json(registry_path)
            for city_entry in registry.get("cities", []):
                city_path = PIPELINE_ROOT / city_entry.get("registry_path", "")
                city_record = read_json(city_path)
                world_ref = city_record.get("artifacts", {}).get("map_world", {})
                source_path = world_ref.get("path", "")
                if world_ref.get("status") != "available" or not source_path:
                    continue
                manifest_ref = city_record.get("artifacts", {}).get("map_manifest", {})
                active_manifest_path = PIPELINE_ROOT / manifest_ref.get("path", "")
                active_manifest = {}
                if manifest_ref.get("status") == "available" and active_manifest_path.is_file():
                    active_manifest = read_json(active_manifest_path)
                source_file = PIPELINE_ROOT / source_path
                counts = dict(world_ref.get("metadata", {}).get("counts", {}) or {})
                # Older city artifacts legitimately have no persisted service
                # projection. Derive it from their frozen POI facts for this
                # read model; this must not create a city version or rewrite
                # the artifact.
                if source_file.is_file():
                    counts["services"] = len(_materialize_city_services(city_record, read_json(source_file)).get("services", []))
                registered_paths.add(source_path)
                worlds.append({
                    "world_id": world_id,
                    "city_id": city_record.get("city_id", ""),
                    "city_name": city_record.get("city_name", ""),
                    "variant": "registered",
                    "geo_provider": active_manifest.get("geo_provider", ""),
                    "world_path": source_path,
                    "world_exists": source_file.is_file(),
                    "counts": counts,
                    "quality": active_manifest.get("quality", {}),
                    "seed": {},
                    "manifest_path": "",
                    "deletable": False,
                    "world_registry_path": str(registry_path.relative_to(PIPELINE_ROOT)),
                    "mtime": registry_path.stat().st_mtime,
                    "readiness": city_record.get("readiness", ""),
                    "missing_capabilities": city_record.get("missing_capabilities", []),
                    "lifecycle_status": city_record.get("lifecycle_status", "active"),
                    "active_version": int(city_record.get("active_version", 1) or 1),
                    "latest_version": max(
                        [int(item.get("version", 1) or 1) for item in city_record.get("city_versions", [])] or [1]
                    ),
                })
        except Exception:
            continue
    if not OUTPUTS_ROOT.exists():
        return worlds
    for manifest_path in OUTPUTS_ROOT.rglob("map_manifest.json"):
        if "_backup_" in str(manifest_path):
            continue
        try:
            manifest = read_json(manifest_path)
            world_file = manifest_path.parent / "world.json"
            world_relative = world_file.relative_to(PIPELINE_ROOT).as_posix()
            if world_relative in registered_paths:
                continue
            worlds.append({
                "city_id": manifest.get("city_id", ""),
                "city_name": manifest.get("city_name", ""),
                "variant": "未登记历史城市",
                "geo_provider": manifest.get("geo_provider", ""),
                "world_path": world_relative,
                "world_exists": world_file.exists(),
                "counts": manifest.get("counts", {}),
                "quality": manifest.get("quality", {}),
                "seed": manifest.get("seed", {}),
                "manifest_path": str(manifest_path.relative_to(PIPELINE_ROOT)),
                "deletable": False,
                "registration_status": "unregistered_legacy",
                "mtime": manifest_path.stat().st_mtime,
            })
        except Exception:
            continue
    worlds.sort(key=lambda w: w.get("mtime", 0), reverse=True)
    return worlds


def _list_seeds() -> list[dict]:
    """List seed files from seeds/*.json with metadata."""
    seeds: list[dict] = []
    if not SEEDS_DIR.exists():
        return seeds
    for seed_path in sorted(SEEDS_DIR.glob("*.json")):
        try:
            data = read_json(seed_path)
            seeds.append({
                "name": seed_path.name,
                "path": seed_path.relative_to(PIPELINE_ROOT).as_posix(),
                "city_id": data.get("city_id", ""),
                "city_name": data.get("city_name", ""),
                "country": data.get("country", ""),
                "district_count": data.get("district_count", 0),
                "corridor_count": data.get("corridor_count", 0),
                "places_per_district": data.get("places_per_district", 0),
                "center": data.get("center", {}),
                "bbox": data.get("bbox"),
                "timezone": data.get("timezone", ""),
                "notes": data.get("notes", ""),
            })
        except Exception:
            continue
    return seeds


def _find_generated_city_world(city_id: str, output_root: Path = OUTPUTS_ROOT) -> Path | None:
    """Find the newest valid map output for one generated city."""
    candidates: list[Path] = []
    for candidate in (output_root / city_id).rglob("world.json"):
        if "_backup_" in candidate.as_posix() or "intermediates" in candidate.parts:
            continue
        try:
            declared_id = ((read_json(candidate).get("cities") or [{}])[0]).get("city_id")
            if declared_id == city_id:
                candidates.append(candidate)
        except Exception:
            continue
    return max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None


def _find_generated_characters(city_id: str, output_root: Path) -> Path | None:
    candidates: list[Path] = []
    for candidate in (output_root / city_id).rglob("characters.json"):
        try:
            if read_json(candidate).get("city_id") == city_id:
                candidates.append(candidate)
        except Exception:
            continue
    return max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None


def _find_character_draft(city_id: str, output_root: Path) -> Path | None:
    """Locate the durable Step 3 checkpoint for relationship-only recovery."""
    candidates: list[Path] = []
    for candidate in output_root.rglob("characters_draft.json"):
        try:
            if read_json(candidate).get("city_id") == city_id:
                candidates.append(candidate)
        except Exception:
            continue
    return max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None


def _counts(world: dict) -> dict[str, int]:
    return {
        "districts": len(world.get("districts", [])),
        "corridors": len(world.get("corridors", [])),
        "places": len(world.get("places", [])),
        "services": len(world.get("services", [])),
        "information_items": len(world.get("information_items", [])),
        "event_templates": len(world.get("event_templates", [])),
        "actors": len(world.get("actors", [])),
    }


def _candidate_target_gap(world: dict, characters: dict, targets: dict) -> tuple[dict[str, int], dict[str, int]]:
    """Return cumulative candidate counts and shortfalls against user targets."""
    counts = _counts(world)
    counts["characters"] = len(characters.get("characters", []) or [])
    required = ("districts", "corridors", "places", "characters")
    gaps = {
        field: max(0, int(targets.get(field, 0) or 0) - int(counts.get(field, 0) or 0))
        for field in required
    }
    return {field: int(counts[field]) for field in required}, {field: value for field, value in gaps.items() if value}


def _candidate_integrity_errors(world: dict, characters: dict) -> list[dict[str, str]]:
    """Reject structurally unsafe candidates; target shortfalls are handled separately."""
    errors: list[dict[str, str]] = []
    for collection, id_key in (
        ("districts", "district_id"), ("corridors", "corridor_id"), ("places", "place_id"),
        ("services", "service_id"), ("information_items", "information_id"),
        ("event_templates", "event_id"), ("actors", "actor_id"),
    ):
        seen: set[str] = set()
        for item in world.get(collection, []) or []:
            if not isinstance(item, dict):
                errors.append({"code": "invalid_map_entity", "detail": f"{collection} contains a non-object entity"})
                continue
            value = str(item.get(id_key, "")).strip()
            if value and value in seen:
                errors.append({"code": "duplicate_map_id", "detail": f"duplicate {id_key}: {value}"})
            if value:
                seen.add(value)

    character_ids: set[str] = set()
    for item in characters.get("characters", []) or []:
        if not isinstance(item, dict):
            errors.append({"code": "invalid_character", "detail": "characters contains a non-object entity"})
            continue
        value = str(item.get("character_id", "")).strip()
        if not value:
            errors.append({"code": "missing_character_id", "detail": "character_id is required"})
        elif value in character_ids:
            errors.append({"code": "duplicate_character_id", "detail": f"duplicate character_id: {value}"})
        character_ids.add(value)
    for relation in characters.get("relationships", []) or []:
        if not isinstance(relation, dict):
            errors.append({"code": "invalid_relationship", "detail": "relationships contains a non-object entity"})
            continue
        for key in ("character_id", "related_id"):
            value = str(relation.get(key, "")).strip()
            if value and value not in character_ids:
                errors.append({"code": "dangling_relationship", "detail": f"{key} references missing character: {value}"})
    # The merged version is the artifact that downstream tasks will consume.
    # Re-run the shared reference validator here instead of relying only on the
    # isolated map batch's earlier validation.
    for detail in validate_world(world).to_dict().get("errors", []):
        errors.append({"code": "merged_world_reference_error", "detail": str(detail)})
    return errors


def _candidate_gate_report(
    world: dict,
    characters: dict,
    targets: dict,
    *,
    character_deduplication: dict | None = None,
) -> dict:
    """One auditable result for structural, repairable, and advisory candidate checks."""
    counts, gaps = _candidate_target_gap(world, characters, targets)
    rejected_characters = int((character_deduplication or {}).get("rejected_characters", 0) or 0)
    repairable = []
    for metric, missing in gaps.items():
        reason = "generated_candidate_below_requested_target"
        if metric == "characters" and rejected_characters:
            reason = "duplicate_character_identity_removed"
        repairable.append({"metric": metric, "missing": missing, "reason": reason})
    hard_errors = _candidate_integrity_errors(world, characters)
    return {
        "schema_version": "geoword.candidate_gate.v1",
        "profile": "merged_city_version",
        "requested_targets": {key: int(targets.get(key, 0) or 0) for key in ("districts", "corridors", "places", "characters")},
        "actual_counts": counts,
        "target_gaps": gaps,
        "hard_errors": hard_errors,
        "repairable_gaps": repairable,
        "warnings": [],
        "can_register_actual": not hard_errors,
    }


def _set_candidate_gate_decision(job: dict, report: dict) -> None:
    """Persist an explicit user decision point; never attach a shortfall silently."""
    job["candidate_counts"] = report["actual_counts"]
    job["target_gaps"] = report["target_gaps"]
    job["gate_report"] = report
    if report["hard_errors"]:
        job["status"] = "failed"
        job["error"] = "候选产物存在完整性错误，不能登记。"
        job["failure_detail"] = "; ".join(item["detail"] for item in report["hard_errors"][:3])
        job["registration"] = {"status": "blocked", "reason": "integrity_error", "gate_report": report}
        _mark_stages_not_run(job, "cultural_knowledge", "world_registration")
        return
    repair_state = job.get("automatic_repair_state") or {}
    rounds = int(repair_state.get("completed_rounds", 0) or 0)
    maximum = int(repair_state.get("max_rounds", 0) or 0)
    job["status"] = "awaiting_registration_decision"
    if repair_state.get("status") == "exhausted":
        job["error"] = (
            f"已自动补齐 {rounds}/{maximum} 轮，候选仍未达到原始目标；"
            "可继续补齐、重新构建或确认按实际产物登记。"
        )
    else:
        job["error"] = "候选已完成但未达到原始目标；请继续补齐、重新构建或确认按实际产物登记。"
    job["failure_detail"] = job["error"]
    job["registration"] = {
        "status": "pending_user_confirmation", "reason": "target_not_reached",
        "candidate_counts": report["actual_counts"], "target_gaps": report["target_gaps"],
        "automatic_repair": repair_state,
    }
    _mark_stages_not_run(job, "cultural_knowledge", "world_registration")


def _run_character_gap_repair(
    job: dict,
    generated_world: Path,
    characters_path: Path,
    *,
    max_attempts: int = MAX_AUTOMATIC_CHARACTER_GAP_REPAIRS,
) -> dict:
    """Add only missing residents to a candidate snapshot, never restart it."""
    target_gaps = dict(job.get("target_gaps") or {})
    missing = int(target_gaps.get("characters", 0) or 0)
    if missing <= 0:
        return {"attempted": False, "characters_path": characters_path}

    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    repair_log = []
    for attempt in range(1, max(1, int(max_attempts)) + 1):
        repair_root = characters_path.parent / "_character_gap_repairs" / job["id"] / f"attempt_{attempt}"
        command = [
            sys.executable, "-u", "-m", "world_module.character.cli", "--world", str(generated_world),
            "--config", "config.yaml", "--count", str(missing), "--out", str(repair_root),
            "--exclude-characters", str(characters_path),
        ]
        _update_job_progress(
            job, "character_generation", "running", step="profiles",
            label=f"自动补齐人物 {attempt}/{max(1, int(max_attempts))}",
            completed=int((job.get("candidate_counts") or {}).get("characters", 0) or 0),
            total=int((job.get("requested_targets") or {}).get("characters", 0) or 0),
        )
        proc = subprocess.Popen(command, cwd=str(PIPELINE_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, encoding="utf-8", errors="replace", env=child_env)
        JOB_PROCESSES.setdefault(job["id"], {})[f"character_repair_{attempt}"] = proc
        for line in proc.stdout:
            line = line.rstrip("\n\r")
            if line.strip() and not _consume_progress_event(job, line):
                job["output"].append("[人物补齐] " + line)
        proc.wait()
        repair_path = _find_generated_characters(job["city_id"], repair_root)
        attempt_detail = {"attempt": attempt, "requested": missing, "returncode": proc.returncode}
        if proc.returncode == 0 and repair_path:
            merged, metadata = _merge_characters(
                read_json(characters_path), read_json(repair_path),
                f"repair_{job['id']}_{attempt}", int(job.get("target_city_version", 1)), return_metadata=True,
            )
            write_json(characters_path, merged)
            counts, gaps = _candidate_target_gap(read_json(generated_world), merged, job.get("requested_targets") or {})
            job["candidate_counts"] = counts
            job["target_gaps"] = gaps
            missing = int(gaps.get("characters", 0) or 0)
            attempt_detail.update({"accepted": metadata["accepted_characters"], "rejected": metadata["rejected_characters"], "remaining_gaps": gaps})
            if not gaps:
                # The supplemented residents must join one coherent relationship graph.
                rel_cmd = [sys.executable, "-u", "-m", "world_module.character.cli", "--config", "config.yaml",
                           "--world", str(generated_world), "--rel-only", "--input", str(characters_path)]
                rel_proc = subprocess.Popen(rel_cmd, cwd=str(PIPELINE_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, bufsize=1, encoding="utf-8", errors="replace", env=child_env)
                JOB_PROCESSES.setdefault(job["id"], {})["character_repair_relationships"] = rel_proc
                for line in rel_proc.stdout:
                    line = line.rstrip("\n\r")
                    if line.strip() and not _consume_progress_event(job, line):
                        job["output"].append("[人物补齐关系] " + line)
                rel_proc.wait()
                attempt_detail["relationship_returncode"] = rel_proc.returncode
                if rel_proc.returncode != 0:
                    attempt_detail["remaining_gaps"] = {"relationships": 1}
            repair_log.append(attempt_detail)
            if not attempt_detail.get("remaining_gaps"):
                job.setdefault("automatic_repairs", []).extend(repair_log)
                return {"attempted": True, "repaired": True, "characters_path": characters_path}
        else:
            attempt_detail["remaining_gaps"] = dict(job.get("target_gaps") or {})
        repair_log.append(attempt_detail)
    job.setdefault("automatic_repairs", []).extend(repair_log)
    return {"attempted": True, "repaired": False, "characters_path": characters_path}


def _remap_increment_ids(payload: dict, expansion_id: str, *, character_data: bool = False) -> dict:
    """Namespace generated IDs before adding them to an existing city artifact."""
    id_keys = {
        "district_id", "corridor_id", "place_id", "service_id", "information_id",
        "event_id", "event_template_id", "actor_id", "character_id", "related_id",
        "from_id", "to_id", "id",
    }
    if character_data:
        id_keys |= {"source", "target"}
    mapping: dict[str, str] = {}

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in id_keys and key != "city_id" and isinstance(item, str) and item:
                    mapping.setdefault(item, f"{expansion_id}__{item}")
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    def replace(value):
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace(item) for item in value]
        return mapping.get(value, value) if isinstance(value, str) else value

    collect(payload)
    return replace(deepcopy(payload))


def _entity_identity(item: dict, *, character: bool = False) -> tuple:
    """Use source identity first; only then use a conservative normalized fallback."""
    if character:
        stable = item.get("external_id") or item.get("source_id")
        if stable:
            return ("character", str(stable).strip().lower())
        fallback = ("character", str(item.get("name", "")).strip().lower(), str(item.get("role", "")).strip().lower())
        return fallback if any(fallback[1:]) else ()
    projection = item.get("map_projection") or {}
    stable = (
        item.get("google_place_id") or item.get("provider_place_id") or item.get("source_place_id")
        or projection.get("google_place_id") or projection.get("provider_place_id")
        or projection.get("source_place_id") or projection.get("osm_id")
    )
    if stable:
        return ("place", str(stable).strip().lower())
    location = item.get("location") or {}
    fallback = (
        "place", str(item.get("name", "")).strip().lower(), str(item.get("address", "")).strip().lower(),
        round(float(location.get("lat", 0) or 0), 5), round(float(location.get("lng", 0) or 0), 5),
    )
    return fallback if any(fallback[1:]) else ()


def _existing_place_source_ids(world: dict) -> list[str]:
    """Return stable provider ids that an expansion must not rediscover."""
    values = set()
    for place in world.get("places", []) or []:
        if not isinstance(place, dict):
            continue
        projection = place.get("map_projection") or {}
        for key in ("google_place_id", "provider_place_id", "source_place_id", "osm_id"):
            value = str(place.get(key, "") or projection.get(key, "")).strip()
            if value:
                values.add(value)
                break
    return sorted(values)


def _tag_new_entities(payload: dict, version: int, batch_id: str) -> dict:
    tagged = deepcopy(payload)
    for key in ("districts", "corridors", "places", "services", "information_items", "event_templates", "actors"):
        for item in tagged.get(key, []) or []:
            if isinstance(item, dict):
                item["introduced_version"] = version
                item["introduced_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                item["source_batch_id"] = batch_id
    return tagged


def _merge_city_world(existing: dict, addition: dict, expansion_id: str, seed: dict, version: int = 2,
                      *, return_metadata: bool = False) -> dict | tuple[dict, dict]:
    """Append one versioned batch while rejecting duplicate POI candidates before write."""
    merged = deepcopy(existing)
    annotated = _tag_new_entities(addition, version, expansion_id)
    existing_place_keys = {
        identity for item in merged.get("places", []) if isinstance(item, dict)
        for identity in [_entity_identity(item)] if identity
    }
    accepted_places: list[dict] = []
    rejected_places = 0
    for place in annotated.get("places", []) or []:
        if not isinstance(place, dict):
            continue
        identity = _entity_identity(place)
        if identity and identity in existing_place_keys:
            rejected_places += 1
            continue
        if identity:
            existing_place_keys.add(identity)
        accepted_places.append(place)
    annotated["places"] = accepted_places
    appended = _remap_increment_ids(annotated, expansion_id)
    for key in ("districts", "corridors", "places", "services", "information_items", "event_templates", "actors"):
        merged.setdefault(key, []).extend(appended.get(key, []))
    metadata = dict(merged.get("generation_metadata") or {})
    history = list(metadata.get("expansion_history") or [])
    history.append({
        "expansion_id": expansion_id,
        "seed": seed,
        "version": f"v{version}",
        "added_counts": _counts(appended),
        "deduplication": {"rejected_places": rejected_places},
        "note": "New entities were version-tagged and appended after POI deduplication; pre-existing city entities were retained.",
    })
    metadata["expansion_history"] = history
    metadata["current_counts"] = _counts(merged)
    merged["generation_metadata"] = metadata
    metadata = {"rejected_places": rejected_places, "accepted_counts": _counts(appended)}
    return (merged, metadata) if return_metadata else merged


def _merge_characters(existing: dict | None, addition: dict, expansion_id: str, version: int = 2,
                      *, return_metadata: bool = False) -> dict | tuple[dict, dict]:
    """Keep existing residents and reject duplicate identities before adding a versioned batch."""
    annotated = deepcopy(addition)
    for item in annotated.get("characters", []) or []:
        if isinstance(item, dict):
            item["introduced_version"] = version
            item["introduced_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            item["source_batch_id"] = expansion_id
    existing_keys = {
        identity for item in (existing or {}).get("characters", []) if isinstance(item, dict)
        for identity in [_entity_identity(item, character=True)] if identity
    }
    accepted = []
    rejected = 0
    rejected_ids: set[str] = set()
    for item in annotated.get("characters", []) or []:
        if not isinstance(item, dict):
            continue
        identity = _entity_identity(item, character=True)
        if identity and identity in existing_keys:
            rejected += 1
            rejected_ids.add(str(item.get("character_id", "")))
            continue
        if identity:
            existing_keys.add(identity)
        accepted.append(item)
    annotated["characters"] = accepted
    if rejected_ids:
        annotated["relationships"] = [
            item for item in annotated.get("relationships", []) or []
            if str(item.get("character_id", "")) not in rejected_ids
            and str(item.get("related_id", "")) not in rejected_ids
        ]
        graph = annotated.get("relationship_graph") or {}
        graph["nodes"] = [item for item in graph.get("nodes", []) or [] if str(item.get("id", "")) not in rejected_ids]
        graph["edges"] = [
            item for item in graph.get("edges", []) or []
            if str(item.get("from_id", item.get("source", ""))) not in rejected_ids
            and str(item.get("to_id", item.get("target", ""))) not in rejected_ids
        ]
        annotated["relationship_graph"] = graph
    appended = _remap_increment_ids(annotated, expansion_id, character_data=True)
    if not existing:
        metadata = {"rejected_characters": rejected, "accepted_characters": len(accepted)}
        return (appended, metadata) if return_metadata else appended
    merged = deepcopy(existing)
    merged.setdefault("characters", []).extend(appended.get("characters", []))
    merged.setdefault("relationships", []).extend(appended.get("relationships", []))
    graph = merged.setdefault("relationship_graph", {})
    added_graph = appended.get("relationship_graph") or {}
    for key in ("nodes", "edges"):
        graph.setdefault(key, []).extend(added_graph.get(key, []))
    config = dict(merged.get("generation_config") or {})
    history = list(config.get("expansion_history") or [])
    history.append({
        "expansion_id": expansion_id,
        "version": f"v{version}",
        "added_characters": len(accepted),
        "deduplication": {"rejected_characters": rejected},
        "added_relationships": len(addition.get("relationships", [])),
    })
    config["expansion_history"] = history
    merged["generation_config"] = config
    metadata = {"rejected_characters": rejected, "accepted_characters": len(accepted)}
    return (merged, metadata) if return_metadata else merged


def _append_poi_supplement(candidate: dict, supplement: dict, repair_id: str, version: int) -> tuple[dict, dict]:
    """Append only new POIs and their direct service records to a candidate.

    A top-up map is used solely as a source of newly retrieved places. Its
    districts and corridors are deliberately discarded: the candidate already
    owns those structural assets and must remain the versioning baseline.
    """
    merged = deepcopy(candidate)
    existing_identities = {
        identity for place in merged.get("places", []) if isinstance(place, dict)
        for identity in [_entity_identity(place)] if identity
    }
    used_place_ids = {str(place.get("place_id", "")) for place in merged.get("places", []) if isinstance(place, dict)}
    accepted: list[dict] = []
    place_id_map: dict[str, str] = {}
    rejected = 0
    for index, raw in enumerate(supplement.get("places", []) or []):
        if not isinstance(raw, dict):
            continue
        identity = _entity_identity(raw)
        if identity and identity in existing_identities:
            rejected += 1
            continue
        place = deepcopy(raw)
        old_id = str(place.get("place_id", "") or f"poi_{index}")
        new_id = old_id
        if not new_id or new_id in used_place_ids:
            new_id = f"{repair_id}__{old_id or index}"
        used_place_ids.add(new_id)
        place_id_map[old_id] = new_id
        place["place_id"] = new_id
        place["introduced_version"] = version
        place["introduced_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        place["source_batch_id"] = repair_id
        accepted.append(place)
        if identity:
            existing_identities.add(identity)

    merged.setdefault("places", []).extend(accepted)
    used_service_ids = {
        str(item.get("service_id", "")) for item in merged.get("services", []) if isinstance(item, dict)
    }
    accepted_services: list[dict] = []
    for index, raw in enumerate(supplement.get("services", []) or []):
        if not isinstance(raw, dict) or str(raw.get("place_id", "")) not in place_id_map:
            continue
        service = deepcopy(raw)
        service["place_id"] = place_id_map[str(raw.get("place_id", ""))]
        service_id = str(service.get("service_id", "") or f"service_{index}")
        if service_id in used_service_ids:
            service_id = f"{repair_id}__{service_id}"
        used_service_ids.add(service_id)
        service["service_id"] = service_id
        service["introduced_version"] = version
        service["introduced_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        service["source_batch_id"] = repair_id
        accepted_services.append(service)
    merged.setdefault("services", []).extend(accepted_services)

    metadata = dict(merged.get("generation_metadata") or {})
    repairs = list(metadata.get("poi_top_up_history") or [])
    repairs.append({
        "repair_id": repair_id,
        "version": f"v{version}",
        "accepted_places": len(accepted),
        "rejected_duplicate_places": rejected,
        "accepted_services": len(accepted_services),
    })
    metadata["poi_top_up_history"] = repairs
    metadata["current_counts"] = _counts(merged)
    merged["generation_metadata"] = metadata
    return merged, {
        "accepted_places": len(accepted),
        "rejected_duplicate_places": rejected,
        "accepted_services": len(accepted_services),
    }


def _run_poi_gap_repair(job: dict, candidate_world_path: Path, attempt: int) -> dict:
    """Retrieve only the missing POIs, reusing the existing discovery session."""
    missing = int((job.get("target_gaps") or {}).get("places", 0) or 0)
    run_seed_path = PIPELINE_ROOT / str(job.get("run_seed", ""))
    map_root = PIPELINE_ROOT / str(job.get("map_output_root", ""))
    if missing <= 0 or not run_seed_path.is_file() or not map_root.is_dir():
        return {"attempted": False, "reason": "missing_poi_repair_checkpoint"}

    repair_id = f"poi_repair_{job['id']}_{attempt}"
    repair_root = candidate_world_path.parent / "_poi_gap_repairs" / job["id"] / f"attempt_{attempt}"
    repair_root.mkdir(parents=True, exist_ok=True)
    repair_seed = read_json(run_seed_path)
    repair_seed.update({
        "output_variant": repair_id,
        # This output is a source of supplementary factual POIs only.  It
        # reuses the candidate's discovery evidence; its temporary districts
        # and corridors are discarded and are never forced to city-wide minima.
        "quality_profile": "poi_supplement",
        "target_place_count": missing,
        "excluded_place_ids": _existing_place_source_ids(read_json(candidate_world_path)),
    })
    repair_seed_path = repair_root / "poi_repair_seed.json"
    write_json(repair_seed_path, repair_seed)
    command = [
        sys.executable, "-u", "-m", "world_module.map.generator",
        "--seed", str(repair_seed_path.relative_to(PIPELINE_ROOT)),
        "--config", "config.yaml", "--geo-provider", str(job.get("geo_provider", "auto")),
        "--output-root", str(map_root.relative_to(PIPELINE_ROOT)),
        "--discovery-session-id", str(job.get("discovery_session_id", "")),
        "--discovery-repair-batch", str(job.get("discovery_repair_batch", 1)),
    ]
    _update_job_progress(
        job, "map_quality", "running", step="poi_top_up",
        label=f"自动补齐 POI {attempt}/{MAX_AUTOMATIC_CANDIDATE_REPAIR_ROUNDS}",
        completed=int((job.get("candidate_counts") or {}).get("places", 0) or 0),
        total=int((job.get("requested_targets") or {}).get("places", 0) or 0),
    )
    started_at = time.time()
    child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    proc = subprocess.Popen(command, cwd=str(PIPELINE_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, encoding="utf-8", errors="replace", env=child_env)
    JOB_PROCESSES.setdefault(job["id"], {})[repair_id] = proc
    for line in proc.stdout:
        line = line.rstrip("\n\r")
        if line.strip() and not _consume_progress_event(job, line):
            job["output"].append("[POI补齐] " + line)
    proc.wait()
    supplement_path = map_root / str(repair_seed["city_id"]) / repair_id / "world.json"
    detail = {
        "kind": "poi_top_up", "attempt": attempt, "requested": missing,
        "returncode": proc.returncode, "elapsed_seconds": round(time.time() - started_at, 3),
        "seed_path": str(repair_seed_path.relative_to(PIPELINE_ROOT)),
    }
    if proc.returncode != 0 or not supplement_path.is_file():
        detail["reason"] = "poi_top_up_generator_failed"
        job.setdefault("automatic_repairs", []).append(detail)
        return {"attempted": True, "repaired": False, "detail": detail}

    supplement_manifest = supplement_path.parent / "map_manifest.json"
    if supplement_manifest.is_file():
        detail["operation_ledger"] = read_json(supplement_manifest).get("operation_ledger", {})
    merged, metadata = _append_poi_supplement(
        read_json(candidate_world_path), read_json(supplement_path), repair_id,
        int(job.get("target_city_version", 1)),
    )
    write_json(candidate_world_path, merged)
    _set_job_checkpoint(job, "map_world", candidate_world_path, stage="map_quality")
    detail.update(metadata)
    job.setdefault("automatic_repairs", []).append(detail)
    return {"attempted": True, "repaired": bool(metadata["accepted_places"]), "detail": detail}


def _run_automatic_candidate_repairs(job: dict, candidate_world_path: Path, characters_path: Path) -> tuple[dict, dict]:
    """Run bounded POI/person top-ups on the candidate snapshot before gating."""
    counts, gaps = _candidate_target_gap(
        read_json(candidate_world_path), read_json(characters_path), job.get("requested_targets") or {},
    )
    job["candidate_counts"] = counts
    job["target_gaps"] = gaps
    if not gaps:
        return counts, gaps

    state = {"status": "running", "round": 0, "completed_rounds": 0,
             "max_rounds": MAX_AUTOMATIC_CANDIDATE_REPAIR_ROUNDS, "target_gaps": dict(gaps)}
    job["automatic_repair_state"] = state
    for attempt in range(1, MAX_AUTOMATIC_CANDIDATE_REPAIR_ROUNDS + 1):
        before = dict(counts)
        state.update({"round": attempt, "target_gaps": dict(gaps)})
        if int(gaps.get("places", 0) or 0) > 0:
            _run_poi_gap_repair(job, candidate_world_path, attempt)
            counts, gaps = _candidate_target_gap(
                read_json(candidate_world_path), read_json(characters_path), job.get("requested_targets") or {},
            )
            job["candidate_counts"] = counts
            job["target_gaps"] = gaps
        if int(gaps.get("characters", 0) or 0) > 0:
            _run_character_gap_repair(job, candidate_world_path, characters_path, max_attempts=1)
            counts, gaps = _candidate_target_gap(
                read_json(candidate_world_path), read_json(characters_path), job.get("requested_targets") or {},
            )
            job["candidate_counts"] = counts
            job["target_gaps"] = gaps
            _set_job_checkpoint(job, "characters", characters_path, stage="character_generation")
        state["completed_rounds"] = attempt
        state["target_gaps"] = dict(gaps)
        if not gaps:
            state["status"] = "completed"
            _update_job_progress(job, "map_quality", "completed", label="自动补齐完成")
            return counts, gaps
        if counts == before:
            break
    state["status"] = "exhausted"
    return counts, gaps


def _registered_city_detail(world_id: str, city_id: str) -> dict:
    record = _registered_city_record(world_id, city_id)
    if not record:
        raise FileNotFoundError(f"Registered city does not exist: {world_id}/{city_id}")
    migrate_city_versions(world_id, city_id)
    record = _registered_city_record(world_id, city_id) or record
    artifacts = record.get("artifacts") or {}
    seed: dict = {}
    manifest_ref = artifacts.get("map_manifest") or {}
    manifest_path = PIPELINE_ROOT / manifest_ref.get("path", "")
    if manifest_ref.get("status") == "available" and manifest_path.is_file():
        seed = read_json(manifest_path).get("seed", {})
    if not seed:
        world_ref = artifacts.get("map_world") or {}
        world_path = PIPELINE_ROOT / world_ref.get("path", "")
        if world_ref.get("status") == "available" and world_path.is_file():
            seed = (read_json(world_path).get("generation_metadata") or {}).get("seed", {})
    return {
        "world_id": world_id,
        "city_id": city_id,
        "city_name": record.get("city_name", ""),
        "seed": seed,
        "counts": ((artifacts.get("map_world") or {}).get("metadata") or {}).get("counts", {}),
        "characters": ((artifacts.get("characters") or {}).get("metadata") or {}).get("characters", 0),
        "expansion_history": record.get("expansion_history", []),
        "active_version": int(record.get("active_version", 1) or 1),
        "versions": [{
            "version": int(entry.get("version", 1) or 1),
            "version_id": entry.get("version_id", f"v{entry.get('version', 1)}"),
            "counts": entry.get("counts") or {},
        } for entry in record.get("city_versions", [])],
    }


def _registered_worlds() -> list[dict]:
    worlds: list[dict] = []
    for manifest_path in sorted(WORLD_ROOT.glob("*/world_manifest.json")):
        try:
            manifest = read_json(manifest_path)
            migrate_city_versions(manifest.get("world_id", manifest_path.parent.name))
            manifest = read_json(manifest_path)
            cities = []
            for entry in manifest.get("cities", []):
                record_path = PIPELINE_ROOT / entry.get("registry_path", "")
                try:
                    record = read_json(record_path) if record_path.is_file() else {}
                    cities.append({
                        "city_id": entry.get("city_id", ""),
                        "city_name": record.get("city_name", entry.get("city_id", "")),
                        "lifecycle_status": record.get("lifecycle_status", "active"),
                        "active_version": int(record.get("active_version", 1) or 1),
                    })
                except Exception:
                    continue
            worlds.append({
                "world_id": manifest.get("world_id", manifest_path.parent.name),
                "display_name": manifest.get("display_name", manifest_path.parent.name),
                "city_count": manifest.get("city_count", 0),
                "city_ids": manifest.get("city_ids", []),
                "cities": cities,
                "lifecycle_status": manifest.get("lifecycle_status", ""),
                "import_mode": manifest.get("import_mode", ""),
            })
        except Exception:
            continue
    return worlds


def _plan_expansion_targets(prior_record: dict, targets: dict | None) -> dict:
    """Validate cumulative expansion targets and derive this batch's honest plan."""
    if not isinstance(targets, dict):
        raise ValueError("扩增必须提供区域、通道、POI 和人物的总目标。")
    artifacts = prior_record.get("artifacts") or {}
    current = dict(((artifacts.get("map_world") or {}).get("metadata") or {}).get("counts") or {})
    current["characters"] = int(((artifacts.get("characters") or {}).get("metadata") or {}).get("characters", 0) or 0)
    fields = ("districts", "corridors", "places", "characters")
    requested: dict[str, int] = {}
    for field in fields:
        try:
            requested[field] = int(targets.get(field))
        except (TypeError, ValueError):
            raise ValueError(f"扩增目标 {field} 必须是非负整数。") from None
        if requested[field] < int(current.get(field, 0) or 0):
            raise ValueError(
                f"扩增目标 {field}={requested[field]} 小于当前累计 {current.get(field, 0)}；"
                "扩增不会删除既有产物。"
            )
    delta = {field: requested[field] - int(current.get(field, 0) or 0) for field in fields}
    if not any(delta.values()):
        raise ValueError("所有扩增目标都等于当前总量，没有需要生成的新批次。")
    # The existing map generator emits both districts and corridors and requires
    # at least two of each.  Reject unsupported partial plans rather than
    # silently generating entities beyond the requested totals.
    if delta["districts"] < 2 or delta["corridors"] < 2:
        raise ValueError(
            "当前生成器只支持新区域+新通道的扩增批次；"
            "请同时将区域和通道总目标至少各提高 2。POI-only 或人物-only 扩增尚未实现。"
        )
    if delta["places"] < 1:
        raise ValueError(
            "结构扩增必须同时增加至少 1 个 POI；请提高 POI 累计目标。"
            "系统不会在未声明目标时静默抓取额外地点。"
        )
    if delta["characters"] < 1:
        raise ValueError("扩增批次需要至少新增 1 个人物；请提高人物总目标。")
    return {"current": {field: int(current.get(field, 0) or 0) for field in fields}, "targets": requested, "delta": delta}


def _plan_new_city_targets(seed: dict, targets: dict | None) -> dict:
    """Validate the shared World Manager target controls for a new city."""
    defaults = {
        "districts": int(seed.get("district_count", 0) or 0),
        "corridors": int(seed.get("corridor_count", 0) or 0),
        "places": int(seed.get("district_count", 0) or 0) * int(seed.get("places_per_district", 0) or 0),
        "characters": 15,
    }
    if not isinstance(targets, dict):
        return defaults
    requested: dict[str, int] = {}
    for field, default in defaults.items():
        try:
            requested[field] = int(targets.get(field, default))
        except (TypeError, ValueError):
            raise ValueError(f"新建城市的 {field} 目标必须是整数。") from None
    if requested["districts"] < 2 or requested["corridors"] < 2:
        raise ValueError("新建城市至少需要 2 个区域和 2 条通道。")
    if requested["places"] < 1 or requested["characters"] < 1:
        raise ValueError("POI 和人物目标必须至少为 1。")
    return requested


def _apply_new_city_targets(seed: dict, targets: dict[str, int]) -> dict:
    """Bind the validated World Studio scale controls to the map seed.

    The quality gate must validate the exact values that the generator was
    instructed to build.  Keeping this in one helper also prevents a future
    UI target from being checked but silently ignored by discovery.
    """
    updated = dict(seed)
    updated["quality_profile"] = "initial_city"
    updated["district_count"] = int(targets["districts"])
    updated["corridor_count"] = int(targets["corridors"])
    updated["target_place_count"] = int(targets["places"])
    return updated


def _start_generation(
    seed_path: str,
    geo_provider: str,
    force: bool,
    world_id: str = "",
    expansion_city_id: str = "",
    expansion_targets: dict | None = None,
    generation_targets: dict | None = None,
    retry_context: dict | None = None,
) -> str:
    """Start map generation as a subprocess; return job_id."""
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    prior_session_id = str((retry_context or {}).get("discovery_session_id", "")).strip()
    discovery_session_id = prior_session_id or job_id
    prior_repair_batch = int((retry_context or {}).get("discovery_repair_batch", 0) or 0)
    discovery_repair_batch = prior_repair_batch + 1 if retry_context and prior_session_id else 1
    if not world_id:
        raise ValueError("城市构建必须指定目标世界。请先新建世界或选择沿用的世界。")
    seed = read_json(PIPELINE_ROOT / seed_path)
    city_id = str(seed.get("city_id", ""))
    if not city_id:
        raise ValueError("种子文件缺少 city_id。")
    if city_id == "selected_city":
        raise ValueError("城市搜索临时标识 selected_city 不能启动正式构建。")
    if expansion_city_id:
        if expansion_city_id != city_id:
            raise ValueError("扩增城市必须使用已登记城市的 city_id。")
        prior_record = _registered_city_record(world_id, city_id)
        if not prior_record:
            raise ValueError(f"不能扩增未在 {world_id} 登记的城市 {city_id}。")
        active_version = int(prior_record.get("active_version", 1) or 1)
        latest_version = next_city_version(world_id, city_id) - 1
        if active_version != latest_version:
            raise ValueError(
                f"当前使用 v{active_version}，只能基于最新版本 v{latest_version} 扩增。"
                "请先在城市卡片中选择最新版本，或新建独立世界。"
            )
        prior_map = ((prior_record.get("artifacts") or {}).get("map_world") or {})
        prior_map_path = PIPELINE_ROOT / prior_map.get("path", "")
        if prior_map.get("status") != "available" or not prior_map_path.is_file():
            raise ValueError("已登记城市缺少可用地图产物，不能扩增。")
        expansion_plan = _plan_expansion_targets(prior_record, expansion_targets)
        requested_targets = expansion_plan["targets"]
        target_city_version = next_city_version(world_id, city_id)
    else:
        prior_record = None
        prior_map_path = None
        expansion_plan = None
        requested_targets = _plan_new_city_targets(seed, generation_targets)
        seed = _apply_new_city_targets(seed, requested_targets)
        write_json(PIPELINE_ROOT / seed_path, seed)
        target_city_version = 1
    expansion_id = f"exp_{int(time.time())}_{uuid.uuid4().hex[:6]}" if expansion_city_id else ""
    artifact_root = WORLD_ROOT / world_id / "artifacts"
    if expansion_id:
        expansion_root = artifact_root / "cities" / city_id / "expansions" / expansion_id
        map_root = expansion_root / "generated_map"
        character_root = expansion_root / "generated_characters"
        expansion_root.mkdir(parents=True, exist_ok=True)
        batch_seed = dict(seed)
        batch_seed["quality_profile"] = "expansion_batch"
        batch_seed["district_count"] = expansion_plan["delta"]["districts"]
        batch_seed["corridor_count"] = expansion_plan["delta"]["corridors"]
        batch_seed["target_place_count"] = expansion_plan["delta"]["places"]
        # The map generator must count only genuinely new provider POIs toward
        # an expansion target.  Merge-time deduplication remains a guardrail,
        # not the mechanism that discovers a shortfall after paid retrieval.
        batch_seed["excluded_place_ids"] = _existing_place_source_ids(read_json(prior_map_path))
        # POI discovery is not an exact-count generator.  This target only
        # guides the batch and its actual output is recorded after generation.
        corridor_place_estimate = (
            expansion_plan["delta"]["corridors"]
            * int(batch_seed.get("corridor_band_limit", 2) or 2) * 5
        )
        batch_seed["places_per_district"] = max(
            4, (max(1, expansion_plan["delta"]["places"] - corridor_place_estimate)
                + expansion_plan["delta"]["districts"] - 1)
            // expansion_plan["delta"]["districts"],
        )
        run_seed_path = expansion_root / "expansion_seed.json"
        write_json(run_seed_path, batch_seed)
    else:
        expansion_root = None
        map_root = artifact_root / "mapseeds"
        character_root = artifact_root / "characters"
        run_seed_path = PIPELINE_ROOT / seed_path
    if retry_context:
        if retry_context.get("mode") != ("incremental_expansion" if expansion_city_id else "new_city"):
            raise ValueError("重试上下文与原构建模式不一致。")
        expansion_id = str(retry_context.get("expansion_id", ""))
        map_root = PIPELINE_ROOT / str(retry_context["map_output_root"])
        character_root = PIPELINE_ROOT / str(retry_context["character_output_root"])
        run_seed_path = PIPELINE_ROOT / str(retry_context["run_seed"])
        if not run_seed_path.is_file():
            raise FileNotFoundError("原构建种子不存在，无法安全重试。")
        expansion_root = map_root.parent if expansion_id else None
        target_city_version = int(retry_context.get("target_city_version", target_city_version))
        requested_targets = retry_context.get("requested_targets") or requested_targets
        expansion_plan = retry_context.get("expansion_plan") or expansion_plan
        if expansion_root and prior_map_path and prior_map_path.is_file():
            retry_seed = read_json(run_seed_path)
            retry_seed["excluded_place_ids"] = _existing_place_source_ids(read_json(prior_map_path))
            write_json(run_seed_path, retry_seed)
    cmd = [
        sys.executable, "-u", "-m", "world_module.map.generator",
        "--seed", str(run_seed_path.relative_to(PIPELINE_ROOT)),
        "--config", "config.yaml",
        "--geo-provider", geo_provider,
        "--output-root", str(map_root),
        "--discovery-session-id", discovery_session_id,
        "--discovery-repair-batch", str(discovery_repair_batch),
    ]
    if force:
        cmd.append("--force")

    def run_job():
        job = JOBS[job_id]
        child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        try:
            if job.get("cancel_requested_at"):
                _finalize_cancelled_world_job(job)
                return
            reused_map_value = str(job.get("reuse_generated_map_path", "")).strip()
            reused_map_path = Path(reused_map_value) if reused_map_value else None
            map_returncode = 0
            if reused_map_path and reused_map_path.is_file():
                job["output"].append(
                    f"[resume] reuse legacy expansion map={reused_map_path} reason=obsolete_complete_city_floor"
                )
                job["map_scope_recovery"] = {
                    "source_map": str(reused_map_path),
                    "reason": "obsolete_complete_city_floor",
                    "reused_external_calls": True,
                }
                _update_job_progress(job, "map_quality", "completed", label="扩增批次校验通过，复用已有地图")
            else:
                proc = subprocess.Popen(
                    cmd,
                    cwd=str(PIPELINE_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    errors="replace",
                    env=child_env,
                )
                job["pid"] = proc.pid
                JOB_PROCESSES.setdefault(job_id, {})["map"] = proc
                for line in proc.stdout:
                    line = line.rstrip("\n\r")
                    if line.strip():
                        if _consume_progress_event(job, line):
                            continue
                        job["output"].append(line)
                        if len(job["output"]) > 300:
                            job["output"] = job["output"][-300:]
                proc.wait()
                map_returncode = proc.returncode
            job["returncode"] = map_returncode
            if job.get("cancel_requested_at"):
                _finalize_cancelled_world_job(job)
                return
            # A successful map subprocess is only an intermediate checkpoint.
            # Do not expose a transient completed job to the browser before
            # characters, cultural assets, and registry attachment finish.
            job["status"] = "running" if map_returncode == 0 else "failed"
            if map_returncode != 0:
                structural_shortfall = _structural_shortfall_from_output(job["output"])
                if structural_shortfall:
                    report = structural_shortfall["report"]
                    job["status"] = "target_not_reached"
                    job["structural_shortfall"] = structural_shortfall
                    job["structural_repair_report"] = report
                    remaining = report.get("remaining_gaps") or {}
                    rendered = ", ".join(
                        f"{kind} {values[0]}/{values[1]}"
                        for kind, values in remaining.items()
                        if isinstance(values, list) and len(values) == 2
                    )
                    job["error"] = "城市结构自动补齐后仍未达到目标"
                    job["failure_detail"] = (
                        f"{rendered or '城市结构数量不足'}；已自动补齐 "
                        f"{report.get('max_automatic_rounds', 0)} 轮。"
                    )
                else:
                    job["error"] = f"Map generator exited with code {map_returncode}"
                    job["failure_detail"] = _summarize_job_failure(job["output"])
                current = (job.get("progress") or {}).get("current", {})
                _update_job_progress(
                    job, current.get("stage", "map_discovery"), "failed",
                    step=current.get("step", ""), label=current.get("label", ""),
                    completed=current.get("completed"), total=current.get("total"),
                    error=job["failure_detail"],
                )
            if map_returncode == 0:
                try:
                    generated_world = reused_map_path or _find_generated_city_world(city_id, map_root)
                    if generated_world is None:
                        raise RuntimeError("Generated city world.json was not found for registry attachment.")
                    batch_quality_path = generated_world.parent / "quality_report.json"
                    job["batch_quality"] = read_json(batch_quality_path) if batch_quality_path.is_file() else {}
                    if expansion_root:
                        addition_world = read_json(generated_world)
                        merged_world, map_deduplication = _merge_city_world(
                            read_json(prior_map_path), addition_world, expansion_id, seed, target_city_version,
                            return_metadata=True,
                        )
                        expansion_root.mkdir(parents=True, exist_ok=True)
                        generated_map_path = generated_world
                        generated_world = expansion_root / "world.json"
                        write_json(generated_world, merged_world)
                        generated_manifest = generated_map_path.parent / "map_manifest.json"
                        merged_manifest = read_json(generated_manifest) if generated_manifest.is_file() else {}
                        merged_manifest.update({
                            "city_id": city_id,
                            "city_name": seed.get("city_name", ""),
                            "paths": {"world": str(generated_world)},
                            "counts": _counts(merged_world),
                            "expansion": {
                                "expansion_id": expansion_id,
                                "city_version": f"v{target_city_version}",
                                "prior_world_path": str(prior_map_path.relative_to(PIPELINE_ROOT)),
                                "generated_batch_path": str(generated_map_path.relative_to(PIPELINE_ROOT)),
                                "generated_counts": _counts(addition_world),
                                "deduplication": map_deduplication,
                            },
                        })
                        write_json(expansion_root / "map_manifest.json", merged_manifest)
                    manifest_path = generated_world.parent / "map_manifest.json"
                    manifest = read_json(manifest_path) if manifest_path.is_file() else {}
                    ledger = manifest.get("operation_ledger", {})
                    _set_job_checkpoint(job, "map_world", generated_world, stage="map_quality")
                    job["phase"] = "character_generation"
                    if job.get("cancel_requested_at"):
                        _finalize_cancelled_world_job(job)
                        return
                    # Keep the job observable while the mandatory second stage
                    # runs; map success alone is not a completed city build.
                    job["status"] = "running"
                    character_cmd = [
                        sys.executable, "-u", "-m", "world_module.character.cli",
                        "--world", str(generated_world), "--config", "config.yaml",
                        "--out", str(character_root),
                    ]
                    character_count = (
                        expansion_plan["delta"]["characters"] if expansion_plan
                        else requested_targets["characters"]
                    )
                    character_cmd.extend(["--count", str(character_count)])
                    if expansion_root and prior_record:
                        prior_char_ref = ((prior_record.get("artifacts") or {}).get("characters") or {})
                        prior_char_path = PIPELINE_ROOT / str(prior_char_ref.get("path", ""))
                        if prior_char_ref.get("status") == "available" and prior_char_path.is_file():
                            character_cmd.extend(["--exclude-characters", str(prior_char_path)])
                    char_proc = subprocess.Popen(
                        character_cmd, cwd=str(PIPELINE_ROOT), stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, bufsize=1, encoding="utf-8", errors="replace",
                        env=child_env,
                    )
                    job["character_pid"] = char_proc.pid
                    JOB_PROCESSES.setdefault(job_id, {})["characters"] = char_proc
                    for line in char_proc.stdout:
                        line = line.rstrip("\n\r")
                        if line.strip():
                            if _consume_progress_event(job, line):
                                continue
                            job["output"].append("[人物] " + line)
                    char_proc.wait()
                    if job.get("cancel_requested_at"):
                        _finalize_cancelled_world_job(job)
                        return
                    characters_path = _find_generated_characters(city_id, character_root)
                    if char_proc.returncode != 0 or characters_path is None:
                        draft_path = _find_character_draft(city_id, character_root)
                        if draft_path:
                            _set_job_checkpoint(job, "characters_draft", draft_path, stage="character_generation")
                        if expansion_id:
                            record_construction_attempt(
                            world_id, city_id, artifact_type="characters", status="failed",
                            details={"job_id": job_id, "returncode": char_proc.returncode,
                                     "subprocess_elapsed_seconds": round(time.time() - job["started_at"], 3),
                                     "cost_status": "unknown_due_to_failure"},
                            )
                        job["status"] = "failed"
                        job["error"] = "人物构建失败；候选城市未登记或启用。"
                        job["failure_detail"] = _summarize_job_failure(job["output"])
                        current = (job.get("progress") or {}).get("current", {})
                        _update_job_progress(
                            job, current.get("stage", "character_generation"), "failed",
                            step=current.get("step", ""), label=current.get("label", ""),
                            completed=current.get("completed"), total=current.get("total"),
                            error=job["failure_detail"],
                        )
                        _mark_stages_not_run(job, "cultural_knowledge", "world_registration")
                        return
                    else:
                        character_ledger_path = characters_path
                        if expansion_root:
                            generated_characters_path = characters_path
                            character_ledger_path = generated_characters_path
                            prior_char_ref = ((prior_record.get("artifacts") or {}).get("characters") or {})
                            prior_char_path = PIPELINE_ROOT / prior_char_ref.get("path", "")
                            prior_characters = (
                                read_json(prior_char_path)
                                if prior_char_ref.get("status") == "available" and prior_char_path.is_file() else None
                            )
                            characters_path = expansion_root / "characters.json"
                            merged_characters, character_deduplication = _merge_characters(
                                prior_characters, read_json(generated_characters_path), expansion_id, target_city_version,
                                return_metadata=True,
                            )
                            write_json(characters_path, merged_characters)
                        else:
                            character_deduplication = {"rejected_characters": 0, "accepted_characters": 0}
                        candidate_characters = read_json(characters_path)
                        _set_job_checkpoint(job, "characters", characters_path, stage="character_generation")
                        candidate_counts, target_gaps = _candidate_target_gap(
                            read_json(generated_world), candidate_characters, requested_targets,
                        )
                        job["candidate_counts"] = candidate_counts
                        job["target_gaps"] = target_gaps
                        if job.get("cancel_requested_at"):
                            _finalize_cancelled_world_job(job)
                            return
                        if target_gaps:
                            candidate_counts, target_gaps = _run_automatic_candidate_repairs(
                                job, generated_world, characters_path,
                            )
                            candidate_characters = read_json(characters_path)
                        gate_report = _candidate_gate_report(
                            read_json(generated_world), candidate_characters, requested_targets,
                            character_deduplication=character_deduplication,
                        )
                        job["gate_report"] = gate_report
                        job["merged_version_validation"] = gate_report
                        if gate_report["hard_errors"] or target_gaps:
                            if expansion_id:
                                record_construction_attempt(
                                    world_id, city_id, artifact_type="city_expansion", status="failed" if gate_report["hard_errors"] else "skipped",
                                    details={
                                        "job_id": job_id,
                                        "reason": "integrity_error" if gate_report["hard_errors"] else "target_not_reached",
                                        "requested_targets": requested_targets,
                                        "candidate_counts": candidate_counts, "target_gaps": target_gaps,
                                        "gate_report": gate_report,
                                        "automatic_repairs": job.get("automatic_repairs", []),
                                        "map_operation_ledger": ledger,
                                        "candidate_map_artifact": str(generated_world.relative_to(PIPELINE_ROOT)),
                                        "candidate_character_artifact": str(characters_path.relative_to(PIPELINE_ROOT)),
                                    },
                                )
                            _set_candidate_gate_decision(job, gate_report)
                            return
                        from world_module.opening_hours_completion import complete_candidate_hours
                        website_caches = list(map_root.rglob("service_detail_cache.json"))
                        if expansion_root and prior_map_path:
                            website_caches.append(prior_map_path.parent / "service_detail_cache.json")
                        complete_candidate_hours(
                            generated_world, config_path=str(PIPELINE_ROOT / "config.yaml"),
                            website_cache_paths=website_caches,
                            progress_callback=lambda row: _update_job_progress(
                                job, "world_assembly", "running", label="补全地点营业时间",
                                completed=row["completed"], total=row["total"]),
                        )
                        _set_job_checkpoint(job, "map_world", generated_world, stage="map_quality")
                        _update_job_progress(job, "cultural_knowledge", "running", label="正在补齐地域资料")
                        try:
                            cultural_assets = ensure_cultural_assets(
                                city_id, str(seed.get("city_name", city_id)),
                                source_titles=_seed_wikivoyage_titles(seed),
                            )
                        except Exception as exc:
                            job["status"] = "failed"
                            job["error"] = f"地域资料补齐失败：{exc}"
                            job["failure_detail"] = job["error"]
                            _update_job_progress(
                                job, "cultural_knowledge", "failed", label="地域资料补齐失败", error=job["error"],
                            )
                            _mark_stages_not_run(job, "world_registration")
                            return
                        job["cultural_assets"] = cultural_assets
                        if not _cultural_assets_are_registerable(cultural_assets):
                            _fail_for_cultural_assets(job, cultural_assets)
                            return
                        _update_job_progress(
                            job, "cultural_knowledge", "completed",
                            label=f"地域资料已补齐：{cultural_assets.get('evidence_cards', 0)} 条证据",
                        )
                        _set_job_checkpoint(job, "cultural_assets", generated_world, stage="cultural_knowledge")
                        _update_job_progress(job, "world_registration", "running", label="正在登记世界产物")
                        record = attach_city_artifact(
                            world_id, city_id, generated_world, character_path=characters_path,
                            import_mode="city_expansion" if expansion_id else "world_build",
                            city_version=target_city_version,
                            registration_metadata={
                                "mode": "target_met", "requested_targets": requested_targets,
                                "actual_counts": candidate_counts, "target_gaps": {},
                                "gate_schema_version": gate_report["schema_version"],
                            },
                        )
                        char_payload = read_json(character_ledger_path)
                        char_ledger = (char_payload.get("generation_config") or {}).get("operation_ledger", {})
                        record_construction_attempt(
                            world_id, city_id, artifact_type="map_world", status="completed",
                            details={"job_id": job_id, "operation_ledger": ledger,
                                     "requested_targets": requested_targets,
                                     "candidate_counts": candidate_counts,
                                     "subprocess_elapsed_seconds": round(time.time() - job["started_at"], 3)},
                        )
                        record_construction_attempt(
                            world_id, city_id, artifact_type="characters", status="completed",
                            details={"job_id": job_id, "output": str(characters_path.relative_to(PIPELINE_ROOT)),
                                     "operation_ledger": char_ledger,
                                      "requested_targets": requested_targets,
                                       "subprocess_elapsed_seconds": round(time.time() - job["started_at"], 3)},
                        )
                        record_construction_attempt(
                            world_id, city_id, artifact_type="cultural_knowledge",
                            status="completed" if cultural_assets.get("status") == "available" else "skipped",
                            details={"job_id": job_id, "cultural_assets": cultural_assets,
                                     "subprocess_elapsed_seconds": round(time.time() - job["started_at"], 3)},
                        )
                        if expansion_id:
                            record_city_expansion(
                                world_id, city_id,
                                details={
                                    "expansion_id": expansion_id,
                                    "city_version": f"v{target_city_version}",
                                    "seed_path": seed_path,
                                    "prior_map_artifact": ((prior_record.get("artifacts") or {}).get("map_world") or {}),
                                    "prior_character_artifact": ((prior_record.get("artifacts") or {}).get("characters") or {}),
                                    "map_artifact": str(generated_world.relative_to(PIPELINE_ROOT)),
                                    "character_artifact": str(characters_path.relative_to(PIPELINE_ROOT)),
                                    "generated_counts": _counts(addition_world),
                                    "deduplication": {**map_deduplication, **character_deduplication},
                                    "current_counts": _counts(read_json(generated_world)),
                                    "expansion_plan": expansion_plan,
                                     "map_operation_ledger": ledger,
                                     "character_operation_ledger": char_ledger,
                                     "cultural_assets": cultural_assets,
                                 },
                            )
                        job["status"] = "completed"
                        _update_job_progress(job, "world_registration", "completed", label="世界登记完成")
                        job["registration"] = {
                            "status": "attached",
                            "world_id": world_id,
                            "city_id": city_id,
                            "mode": "incremental_expansion" if expansion_id else "new_city",
                            "expansion_id": expansion_id,
                            "readiness": record.get("readiness", ""),
                            "map_path": str(generated_world.relative_to(PIPELINE_ROOT)),
                            "characters_path": str(characters_path.relative_to(PIPELINE_ROOT)) if characters_path else "",
                        }
                except Exception as exc:
                    # The city build remains valid; the registry problem is explicit.
                    job["registration"] = {"status": "failed", "world_id": world_id, "error": str(exc)}
                    _update_job_progress(job, "world_registration", "failed", label="世界登记失败", error=str(exc))
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = str(exc)
            job["failure_detail"] = str(exc)
            current = (job.get("progress") or {}).get("current", {})
            _update_job_progress(
                job, current.get("stage", "map_discovery"), "failed",
                step=current.get("step", ""), label=current.get("label", ""), error=str(exc),
            )
        finally:
            JOB_PROCESSES.pop(job_id, None)
            job["finished_at"] = time.time()
            _persist_world_job(job)

    JOBS[job_id] = {
        "id": job_id,
        "status": "running",
        "output": [],
        "returncode": None,
        "seed": seed_path,
        "run_seed": str(run_seed_path.relative_to(PIPELINE_ROOT)),
        "geo_provider": geo_provider,
        "force": force,
        "discovery_mode": "refresh" if force else ("reuse" if retry_context else "first_run"),
        "discovery_session_id": discovery_session_id,
        "discovery_repair_batch": discovery_repair_batch,
        "reuse_generated_map_path": str((retry_context or {}).get("reuse_generated_map_path", "")),
        "retry_of": retry_context.get("id", "") if retry_context else "",
        "world_id": world_id,
        "city_id": city_id,
        "expansion_city_id": expansion_city_id,
        "expansion_id": expansion_id,
        "target_city_version": target_city_version,
        "expansion_plan": expansion_plan,
        "requested_targets": requested_targets,
        "mode": "incremental_expansion" if expansion_id else "new_city",
        "map_output_root": str(map_root.relative_to(PIPELINE_ROOT)),
        "character_output_root": str(character_root.relative_to(PIPELINE_ROOT)),
        "phase": "map_generation",
        "registration": {"status": "pending", "world_id": world_id} if world_id else {"status": "not_requested"},
        "started_at": time.time(),
        "finished_at": None,
        "pid": None,
        "error": None,
        "failure_detail": "",
        "resume": ({
            "source_job_id": retry_context.get("id", ""),
            "stage": "map_scope_recovery" if retry_context.get("reuse_generated_map_path") else "map_generation",
            "label": "复用已完成的扩增地图批次" if retry_context.get("reuse_generated_map_path") else "从城市结构补齐继续",
            "reused": ["候选地图"] if retry_context.get("reuse_generated_map_path") else ["城市骨架", "区域", "通道"],
        } if retry_context else {}),
        "progress": _new_job_progress(),
    }
    thread = threading.Thread(target=run_job, daemon=True)
    thread.start()
    return job_id


def _checkpoint_path(job: dict, name: str) -> Path | None:
    checkpoint = (job.get("checkpoints") or {}).get(name) or {}
    path_value = checkpoint.get("path", "")
    path = PIPELINE_ROOT / path_value
    if not path.is_file():
        return None
    expected_hash = checkpoint.get("sha256", "")
    if expected_hash and _artifact_checkpoint(path).get("sha256") != expected_hash:
        return None
    return path


def _candidate_paths_from_job(job: dict) -> tuple[Path | None, Path | None]:
    """Resolve candidate artifacts from modern checkpoints or older output roots."""
    map_path = _checkpoint_path(job, "map_world")
    characters_path = _checkpoint_path(job, "characters")
    if map_path is None:
        plan = _resume_plan(job)
        candidate = plan.get("map_path")
        map_path = candidate if isinstance(candidate, Path) and candidate.is_file() else None
    return map_path, characters_path


def _map_checkpoint_batch_quality_state(job: dict, map_path: Path) -> bool | None:
    """Whether a map checkpoint may safely enter the character stage.

    A persisted ``world.json`` is only an intermediate map output.  It is not
    a recoverable candidate when its own batch quality report failed; otherwise
    a resumed job can spend character-model calls on a map that the pipeline
    had already rejected.
    """
    quality = job.get("batch_quality")
    if isinstance(quality, dict) and "ok" in quality:
        return bool(quality.get("ok"))
    quality_path = map_path.parent / "quality_report.json"
    if not quality_path.is_file():
        return None
    try:
        payload = read_json(quality_path)
    except Exception:
        return None
    return bool(payload["ok"]) if "ok" in payload else None


def _resume_plan(previous: dict) -> dict:
    """Select the smallest safe continuation based on persisted checkpoints."""
    map_path = _checkpoint_path(previous, "map_world")
    if map_path is None:
        legacy_batch = _reusable_legacy_expansion_batch(previous)
        if legacy_batch is not None:
            return {
                "stage": "map_scope_recovery",
                "label": "复用已完成的扩增地图批次",
                "reused": ["候选地图"],
                "map_path": legacy_batch,
            }
        map_root = PIPELINE_ROOT / str(previous.get("map_output_root", ""))
        candidate = map_root.parent / "world.json"
        map_path = candidate if candidate.is_file() else _find_generated_city_world(previous.get("city_id", ""), map_root)
    if map_path is None:
        if previous.get("structural_shortfall") or previous.get("structural_repair_report"):
            return {
                "stage": "map_generation",
                "label": "从城市结构补齐继续",
                "reused": ["城市骨架", "区域", "通道"],
            }
        return {"stage": "map_generation", "label": "从城市发现继续", "reused": []}

    if _map_checkpoint_batch_quality_state(previous, map_path) is False:
        return {
            "stage": "map_generation",
            "label": "地图质量门禁未通过，重新从地图阶段继续",
            "reused": ["城市骨架", "区域", "通道"],
        }

    # A shortfall candidate is its own recovery baseline.  Do not return to the
    # active registered version: automatic/manual top-up must retain its POIs,
    # residents and relationship graph, then append only what remains missing.
    target_gaps = dict(previous.get("target_gaps") or {})
    repairable_gaps = {key: value for key, value in target_gaps.items() if int(value or 0) > 0}
    characters_path = _checkpoint_path(previous, "characters")
    if repairable_gaps and characters_path:
        return {
            "stage": "candidate_repair",
            "label": "基于候选版本补齐缺少资产",
            "reused": ["候选地图", "候选人物"],
            "target_gaps": repairable_gaps,
            "map_path": map_path,
            "characters_path": characters_path,
        }

    failed_stage = str(((previous.get("progress") or {}).get("current") or {}).get("stage", ""))
    draft_path = _checkpoint_path(previous, "characters_draft")
    characters_path = _checkpoint_path(previous, "characters")
    if failed_stage == "world_registration" and characters_path:
        return {"stage": "world_registration", "label": "从世界登记继续", "reused": ["地图", "人物", "地域资料"],
                "map_path": map_path, "characters_path": characters_path}
    if failed_stage == "cultural_knowledge" and characters_path:
        return {"stage": "cultural_knowledge", "label": "从地域资料继续", "reused": ["地图", "人物"],
                "map_path": map_path, "characters_path": characters_path}
    if draft_path:
        return {"stage": "relationships", "label": "从人物关系网络继续", "reused": ["地图", "人物档案"],
                "map_path": map_path, "draft_path": draft_path}
    return {"stage": "character_generation", "label": "从人物与关系继续", "reused": ["地图"], "map_path": map_path}


def _resume_progress(previous: dict, stage: str) -> dict:
    progress = deepcopy(previous.get("progress") or _new_job_progress())
    start = PROGRESS_STAGE_ORDER.index("map_quality") if stage == "candidate_repair" else \
        PROGRESS_STAGE_ORDER.index("character_generation") if stage in {"character_generation", "relationships"} \
        else PROGRESS_STAGE_ORDER.index(stage)
    for index, entry in enumerate(progress.get("stages", [])):
        if index >= start:
            entry.pop("started_at", None)
            entry.pop("finished_at", None)
            entry["status"] = "pending"
    progress["current"] = {"stage": PROGRESS_STAGE_ORDER[start], "label": "等待继续构建"}
    return progress


def _finish_resumed_candidate(job: dict, previous: dict, generated_world: Path, source_characters: Path,
                              *, resume_stage: str) -> None:
    """Finish a resumed build after characters are available, without rerunning the map."""
    world_id = job["world_id"]
    city_id = job["city_id"]
    expansion_id = job.get("expansion_id", "")
    expansion_root = (PIPELINE_ROOT / job["map_output_root"]).parent if expansion_id else None
    characters_path = source_characters
    character_deduplication = {"rejected_characters": 0, "accepted_characters": 0}
    if expansion_root and resume_stage != "candidate_repair":
        prior_record = _registered_city_record(world_id, city_id) or {}
        prior_ref = ((prior_record.get("artifacts") or {}).get("characters") or {})
        prior_path = PIPELINE_ROOT / prior_ref.get("path", "")
        prior_characters = read_json(prior_path) if prior_ref.get("status") == "available" and prior_path.is_file() else None
        characters_path = expansion_root / "characters.json"
        merged, character_deduplication = _merge_characters(
            prior_characters, read_json(source_characters), expansion_id, int(job.get("target_city_version", 1)),
            return_metadata=True,
        )
        write_json(characters_path, merged)

    candidate_counts, target_gaps = _candidate_target_gap(
        read_json(generated_world), read_json(characters_path), job.get("requested_targets") or {},
    )
    job["candidate_counts"] = candidate_counts
    job["target_gaps"] = target_gaps
    _set_job_checkpoint(job, "characters", characters_path, stage="character_generation")
    if target_gaps:
        candidate_counts, target_gaps = _run_automatic_candidate_repairs(
            job, generated_world, characters_path,
        )
        candidate_counts, target_gaps = _candidate_target_gap(
            read_json(generated_world), read_json(characters_path), job.get("requested_targets") or {},
        )
        job["candidate_counts"] = candidate_counts
        job["target_gaps"] = target_gaps
        _set_job_checkpoint(job, "characters", characters_path, stage="character_generation")
    gate_report = _candidate_gate_report(
        read_json(generated_world), read_json(characters_path), job.get("requested_targets") or {},
        character_deduplication=character_deduplication,
    )
    job["gate_report"] = gate_report
    job["merged_version_validation"] = gate_report
    if gate_report["hard_errors"] or target_gaps:
        _set_candidate_gate_decision(job, gate_report)
        return

    # Even a registration-only retry must validate the retained regional
    # artifacts.  Historical jobs may have reached this stage with an invalid
    # source result, so prior progress alone is not an admission credential.
    _update_job_progress(job, "cultural_knowledge", "running", label="正在补齐地域资料")
    try:
        resume_seed = read_json(job["run_seed"])
        cultural_assets = ensure_cultural_assets(
            city_id, str(resume_seed.get("city_name", city_id)),
            source_titles=_seed_wikivoyage_titles(resume_seed),
        )
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = f"地域资料补齐失败：{exc}"
        job["failure_detail"] = job["error"]
        _update_job_progress(job, "cultural_knowledge", "failed", label="地域资料补齐失败", error=job["error"])
        _mark_stages_not_run(job, "world_registration")
        return
    job["cultural_assets"] = cultural_assets
    if not _cultural_assets_are_registerable(cultural_assets):
        _fail_for_cultural_assets(job, cultural_assets)
        return
    _update_job_progress(
        job, "cultural_knowledge", "completed",
        label=f"地域资料已补齐：{cultural_assets.get('evidence_cards', 0)} 条证据",
    )
    _set_job_checkpoint(job, "cultural_assets", generated_world, stage="cultural_knowledge")

    _update_job_progress(job, "world_registration", "running", label="正在登记世界产物")
    try:
        record = attach_city_artifact(
            world_id, city_id, generated_world, character_path=characters_path,
            import_mode="city_expansion" if expansion_id else "world_build",
            city_version=int(job.get("target_city_version", 1)),
            registration_metadata={
                "mode": "target_met", "requested_targets": job.get("requested_targets") or {},
                "actual_counts": candidate_counts, "target_gaps": {},
                "gate_schema_version": gate_report["schema_version"],
            },
        )
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = f"世界登记失败：{exc}"
        job["failure_detail"] = job["error"]
        job["registration"] = {"status": "failed", "world_id": world_id, "error": str(exc)}
        _update_job_progress(job, "world_registration", "failed", label="世界登记失败", error=job["error"])
        return

    char_payload = read_json(source_characters)
    char_ledger = (char_payload.get("generation_config") or {}).get("operation_ledger", {})
    record_construction_attempt(
        world_id, city_id, artifact_type="resume",
        status="completed", details={
            "job_id": job["id"], "source_job_id": previous["id"], "resume_stage": resume_stage,
            "reused_checkpoints": job.get("resume", {}).get("reused", []),
            "candidate_counts": candidate_counts, "character_operation_ledger": char_ledger,
            "subprocess_elapsed_seconds": round(time.time() - job["started_at"], 3),
        },
    )
    job["status"] = "completed"
    job["registration"] = {
        "status": "attached", "world_id": world_id, "city_id": city_id,
        "mode": "incremental_expansion" if expansion_id else "new_city",
        "expansion_id": expansion_id, "readiness": record.get("readiness", ""),
        "map_path": str(generated_world.relative_to(PIPELINE_ROOT)),
        "characters_path": str(characters_path.relative_to(PIPELINE_ROOT)),
    }
    _update_job_progress(job, "world_registration", "completed", label="世界登记完成")


def _start_resumed_generation(previous: dict) -> str:
    """Create a separately audited job that continues from durable checkpoints."""
    plan = _resume_plan(previous)
    if plan["stage"] in {"map_generation", "map_scope_recovery"}:
        retry_context = dict(previous)
        if plan["stage"] == "map_scope_recovery":
            retry_context["reuse_generated_map_path"] = str(plan["map_path"])
        return _start_generation(
            previous["seed"], previous["geo_provider"], False, previous["world_id"],
            previous.get("expansion_city_id", ""), (previous.get("expansion_plan") or {}).get("targets"),
            previous.get("requested_targets"), retry_context=retry_context,
        )
    job_id = f"job_{uuid.uuid4().hex[:8]}"
    job = {
        "id": job_id, "status": "running", "output": [
            f"[resume] source_job={previous['id']} stage={plan['stage']} reused={','.join(plan['reused'])}"
        ], "returncode": None, "seed": previous["seed"], "run_seed": previous["run_seed"],
        "geo_provider": previous["geo_provider"], "force": False, "discovery_mode": "resume",
        "retry_of": previous["id"], "world_id": previous["world_id"], "city_id": previous["city_id"],
        "expansion_city_id": previous.get("expansion_city_id", ""), "expansion_id": previous.get("expansion_id", ""),
        "target_city_version": previous.get("target_city_version", 1), "expansion_plan": previous.get("expansion_plan"),
        "requested_targets": previous.get("requested_targets", {}), "mode": previous.get("mode", "new_city"),
        "map_output_root": previous["map_output_root"], "character_output_root": previous["character_output_root"],
        "phase": "character_generation" if plan["stage"] in {"character_generation", "relationships"} else plan["stage"],
        "registration": {"status": "pending", "world_id": previous["world_id"]}, "started_at": time.time(),
        "finished_at": None, "pid": None, "error": None, "failure_detail": "",
        "checkpoints": deepcopy(previous.get("checkpoints") or {}), "resume": {
            "source_job_id": previous["id"], "stage": plan["stage"], "label": plan["label"], "reused": plan["reused"],
        }, "progress": _resume_progress(previous, plan["stage"]),
    }

    def run_resume() -> None:
        child_env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        try:
            generated_world = plan["map_path"]
            source_characters = plan.get("characters_path")
            if plan["stage"] in {"character_generation", "relationships"}:
                character_root = PIPELINE_ROOT / job["character_output_root"]
                command = [sys.executable, "-u", "-m", "world_module.character.cli", "--config", "config.yaml"]
                if plan["stage"] == "relationships":
                    command.extend(["--world", str(generated_world), "--rel-only", "--input", str(plan["draft_path"])])
                else:
                    count = ((job.get("expansion_plan") or {}).get("delta") or {}).get("characters")
                    count = count if count is not None else (job.get("requested_targets") or {}).get("characters", 1)
                    command.extend(["--world", str(generated_world), "--count", str(count), "--out", str(character_root)])
                _update_job_progress(job, "character_generation", "running", label=plan["label"])
                proc = subprocess.Popen(command, cwd=str(PIPELINE_ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, bufsize=1, encoding="utf-8", errors="replace", env=child_env)
                job["character_pid"] = proc.pid
                JOB_PROCESSES.setdefault(job_id, {})["characters"] = proc
                for line in proc.stdout:
                    line = line.rstrip("\n\r")
                    if line.strip() and not _consume_progress_event(job, line):
                        job["output"].append("[人物] " + line)
                proc.wait()
                job["returncode"] = proc.returncode
                if proc.returncode != 0:
                    job["status"] = "failed"
                    job["error"] = "人物构建失败；候选城市未登记或启用。"
                    job["failure_detail"] = _summarize_job_failure(job["output"])
                    current = job["progress"]["current"]
                    _update_job_progress(job, "character_generation", "failed", step=current.get("step", ""),
                                         label=current.get("label", ""), error=job["failure_detail"])
                    _mark_stages_not_run(job, "cultural_knowledge", "world_registration")
                    return
                if plan["stage"] == "relationships":
                    source_characters = plan["draft_path"]
                    final_path = source_characters.with_name("characters.json")
                    write_json(final_path, read_json(source_characters))
                    source_characters = final_path
                else:
                    source_characters = _find_generated_characters(job["city_id"], character_root)
                if source_characters is None:
                    raise RuntimeError("人物产物未写入，无法继续构建。")
            if source_characters is None:
                raise RuntimeError("候选人物检查点缺失，不能继续补齐。")
            _finish_resumed_candidate(job, previous, generated_world, source_characters, resume_stage=plan["stage"])
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = str(exc)
            job["failure_detail"] = str(exc)
            current = (job.get("progress") or {}).get("current", {})
            _update_job_progress(job, current.get("stage", "map_discovery"), "failed", step=current.get("step", ""),
                                 label=current.get("label", ""), error=str(exc))
        finally:
            JOB_PROCESSES.pop(job_id, None)
            job["finished_at"] = time.time()
            _persist_world_job(job)

    JOBS[job_id] = job
    threading.Thread(target=run_resume, daemon=True).start()
    return job_id


def _start_actual_registration(previous: dict) -> str:
    """Register a structurally valid shortfall candidate only after user confirmation."""
    map_path, characters_path = _candidate_paths_from_job(previous)
    if map_path is None or characters_path is None:
        raise ValueError("候选地图或人物产物检查点缺失，不能按实际数据登记。")

    report = _candidate_gate_report(
        read_json(map_path), read_json(characters_path), previous.get("requested_targets") or {},
    )
    if report["hard_errors"]:
        raise ValueError("候选产物存在完整性错误，不能按实际数据登记。")
    if not report["target_gaps"]:
        raise ValueError("候选已达到原始目标，无需按实际数据登记。")

    job_id = f"job_{uuid.uuid4().hex[:8]}"
    confirmed_at = time.time()
    job = {
        "id": job_id, "status": "running", "output": [
            f"[actual-registration] source_job={previous['id']} user_confirmed_at={confirmed_at:.3f}",
        ], "returncode": 0, "seed": previous["seed"], "run_seed": previous["run_seed"],
        "geo_provider": previous["geo_provider"], "force": False, "discovery_mode": "register_actual",
        "retry_of": previous["id"], "world_id": previous["world_id"], "city_id": previous["city_id"],
        "expansion_city_id": previous.get("expansion_city_id", ""), "expansion_id": previous.get("expansion_id", ""),
        "target_city_version": previous.get("target_city_version", 1), "expansion_plan": previous.get("expansion_plan"),
        "requested_targets": previous.get("requested_targets", {}), "mode": previous.get("mode", "new_city"),
        "map_output_root": previous["map_output_root"], "character_output_root": previous["character_output_root"],
        "phase": "registration_with_actuals", "registration": {"status": "pending", "world_id": previous["world_id"]},
        "started_at": time.time(), "finished_at": None, "pid": None, "error": None, "failure_detail": "",
        "checkpoints": deepcopy(previous.get("checkpoints") or {}), "candidate_counts": report["actual_counts"],
        "target_gaps": report["target_gaps"], "gate_report": report,
        "registration_decision": {"type": "user_confirmed_actual", "source_job_id": previous["id"], "confirmed_at": confirmed_at},
        "progress": _resume_progress(previous, "cultural_knowledge"),
    }

    def run_actual_registration() -> None:
        try:
            city_id = job["city_id"]
            world_id = job["world_id"]
            seed_path = Path(job["run_seed"])
            if not seed_path.is_absolute():
                seed_path = PIPELINE_ROOT / seed_path
            _update_job_progress(job, "cultural_knowledge", "running", label="正在补齐地域资料")
            registration_seed = read_json(seed_path)
            cultural_assets = ensure_cultural_assets(
                city_id, str(registration_seed.get("city_name", city_id)),
                source_titles=_seed_wikivoyage_titles(registration_seed),
            )
            job["cultural_assets"] = cultural_assets
            if not _cultural_assets_are_registerable(cultural_assets):
                _fail_for_cultural_assets(job, cultural_assets)
                return
            _update_job_progress(
                job, "cultural_knowledge", "completed",
                label=(f"地域资料已补齐：{cultural_assets.get('evidence_cards', 0)} 条证据"
                       if cultural_assets.get("status") == "available" else "地域资料来源不可用"),
            )
            _set_job_checkpoint(job, "cultural_assets", map_path, stage="cultural_knowledge")
            _update_job_progress(job, "world_registration", "running", label="按实际产物登记世界版本")
            metadata = {
                "mode": "user_confirmed_actual",
                "source_job_id": previous["id"],
                "confirmed_at": confirmed_at,
                "requested_targets": report["requested_targets"],
                "actual_counts": report["actual_counts"],
                "target_gaps": report["target_gaps"],
                "gate_schema_version": report["schema_version"],
            }
            record = attach_city_artifact(
                world_id, city_id, map_path, character_path=characters_path,
                import_mode="city_expansion" if job.get("expansion_id") else "world_build",
                city_version=int(job.get("target_city_version", 1)), registration_metadata=metadata,
            )
            record_construction_attempt(
                world_id, city_id, artifact_type="actual_registration", status="completed",
                details={"job_id": job_id, **metadata, "cultural_assets": cultural_assets,
                         "subprocess_elapsed_seconds": round(time.time() - job["started_at"], 3)},
            )
            job["status"] = "completed"
            job["registration"] = {
                "status": "attached_with_shortfall", "world_id": world_id, "city_id": city_id,
                "readiness": record.get("readiness", ""), "target_gaps": report["target_gaps"],
                "actual_counts": report["actual_counts"], "registration_metadata": metadata,
            }
            _update_job_progress(job, "world_registration", "completed", label="已按实际产物登记")
            previous.setdefault("registration_decisions", []).append({
                "type": "user_confirmed_actual", "at": confirmed_at, "registration_job_id": job_id,
                "actual_counts": report["actual_counts"], "target_gaps": report["target_gaps"],
            })
            previous["registration"] = {"status": "superseded_by_actual_registration", "registration_job_id": job_id}
            _persist_world_job(previous)
        except Exception as exc:
            job["status"] = "failed"
            job["error"] = f"按实际产物登记失败：{exc}"
            job["failure_detail"] = job["error"]
            job["registration"] = {"status": "failed", "error": str(exc)}
            _update_job_progress(job, "world_registration", "failed", label="按实际产物登记失败", error=job["error"])
        finally:
            job["finished_at"] = time.time()
            _persist_world_job(job)

    JOBS[job_id] = job
    threading.Thread(target=run_actual_registration, daemon=True).start()
    return job_id


def _load_world_into_server(world_path: str) -> dict:
    """Reload the server's world from a new path."""
    path = Path(world_path)
    if not path.is_absolute():
        path = PIPELINE_ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"World not found: {path}")
    raw_world = read_json(path)
    city = raw_world.get("cities", [{}])[0] if raw_world.get("cities") else {}
    record = _registered_city_record(VisHandler.active_world_id, city.get("city_id", ""))
    world = _materialize_city_services(record, raw_world)
    VisHandler.world = world
    VisHandler.world_path = str(path)
    artifact = (record or {}).get("artifacts", {}).get("characters", {})
    matching_characters = PIPELINE_ROOT / artifact["path"] if artifact.get("status") == "available" else None
    VisHandler.characters_path = str(matching_characters) if matching_characters and matching_characters.is_file() else ""
    VisHandler.characters = read_json(matching_characters) if VisHandler.characters_path else {}
    counts = {
        "districts": len(world.get("districts", [])),
        "corridors": len(world.get("corridors", [])),
        "places": len(world.get("places", [])),
        "services": len(world.get("services", [])),
        "information_items": len(world.get("information_items", [])),
        "event_templates": len(world.get("event_templates", [])),
    }
    return {
        "ok": True, "world_path": str(path), "city_name": city.get("name", ""), "counts": counts,
        "characters_path": VisHandler.characters_path,
    }


def _find_matching_characters(city_id: str) -> Path | None:
    """Legacy-only discovery; managed world reads must use city registry paths."""
    character_root = PIPELINE_ROOT / "outputs" / "characters"
    for candidate in sorted(character_root.rglob("characters.json")):
        try:
            if read_json(candidate).get("city_id") == city_id:
                return candidate
        except Exception:
            continue
    return None


def _default_world_path(world_id: str) -> str:
    registry_path = WORLD_ROOT / world_id / "world_manifest.json"
    if not registry_path.is_file():
        return ""
    registry = read_json(registry_path)
    for city in registry.get("cities", []):
        city_record_path = PIPELINE_ROOT / city.get("registry_path", "")
        if not city_record_path.is_file():
            continue
        record = read_json(city_record_path)
        world_ref = record.get("artifacts", {}).get("map_world", {})
        if world_ref.get("status") == "available":
            return str(PIPELINE_ROOT / world_ref["path"])
    return ""


def _registered_city_record(world_id: str, city_id: str) -> dict | None:
    manifest_path = WORLD_ROOT / world_id / "world_manifest.json"
    if not manifest_path.is_file():
        return None
    manifest = read_json(manifest_path)
    for entry in manifest.get("cities", []):
        if entry.get("city_id") == city_id:
            path = PIPELINE_ROOT / entry.get("registry_path", "")
            return read_json(path) if path.is_file() else None
    return None


def _materialize_city_services(record: dict | None, world: dict) -> dict:
    """Prefer the registered factual index; deterministically rebuild on legacy input."""
    index_path: Path | None = None
    artifact = ((record or {}).get("artifacts") or {}).get("factual_service_index") or {}
    if artifact.get("status") == "available":
        candidate = PIPELINE_ROOT / str(artifact.get("path") or "")
        if candidate.is_file():
            index_path = candidate
    if index_path is None:
        sibling = Path(str((record or {}).get("artifacts", {}).get("map_world", {}).get("path", "")))
        if sibling.name == "world.json":
            candidate = PIPELINE_ROOT / sibling.parent / "factual_service_index.json"
            if candidate.is_file():
                index_path = candidate
    indexed_services = read_factual_service_index(index_path, world) if index_path else None
    return materialize_factual_services(world, indexed_services=indexed_services)


def _aggregate_registered_world(world_id: str, city_id: str = "") -> tuple[dict, dict]:
    """Construct an in-memory map view from active registry references only."""
    manifest_path = WORLD_ROOT / world_id / "world_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"World registry does not exist: {world_id}")
    manifest = read_json(manifest_path)
    selected = []
    for entry in manifest.get("cities", []):
        if city_id and entry.get("city_id") != city_id:
            continue
        record_path = PIPELINE_ROOT / entry.get("registry_path", "")
        if not record_path.is_file():
            continue
        record = read_json(record_path)
        if record.get("lifecycle_status", "active") != "active":
            continue
        ref = record.get("artifacts", {}).get("map_world", {})
        source = PIPELINE_ROOT / ref.get("path", "")
        if ref.get("status") == "available" and source.is_file():
            selected.append((record, _materialize_city_services(record, read_json(source))))
    if city_id and not selected:
        raise ValueError(f"No active city artifact is available for {city_id}")
    if city_id and len(selected) == 1:
        record, world = selected[0]
        return world, {"scope": "city", "world_id": world_id, "city_id": record.get("city_id", ""),
                       "city_ids": [record.get("city_id", "")]}
    aggregate = {"schema_version": "geoword.world.map_view.v1", "cities": [], "districts": [],
                 "corridors": [], "places": [], "services": [], "information_items": [],
                 "actors": []}
    city_ids: list[str] = []
    for record, world in selected:
        source_city_id = record.get("city_id", "")
        city_ids.append(source_city_id)
        for key in ("cities", "districts", "corridors", "places", "services", "information_items"):
            for item in world.get(key, []):
                enriched = dict(item)
                enriched["source_city_id"] = source_city_id
                aggregate[key].append(enriched)
    # The actor API merges character artifacts separately; map worlds do not own characters.
    aggregate["cities"] = [{"city_id": world_id, "name": manifest.get("display_name", world_id),
                            "source_city_ids": city_ids}]
    return aggregate, {"scope": "world", "world_id": world_id, "city_ids": city_ids}


def _world_overview(world_id: str) -> list[dict]:
    """Return city-level anchors for the world-scale map, never POI or actors."""
    manifest_path = WORLD_ROOT / world_id / "world_manifest.json"
    if not manifest_path.is_file():
        return []
    items: list[dict] = []
    for entry in read_json(manifest_path).get("cities", []):
        record = _registered_city_record(world_id, entry.get("city_id", ""))
        if not record or record.get("lifecycle_status", "active") != "active":
            continue
        ref = (record.get("artifacts") or {}).get("map_world") or {}
        path = PIPELINE_ROOT / ref.get("path", "")
        if ref.get("status") != "available" or not path.is_file():
            continue
        source = read_json(path)
        city = (source.get("cities") or [{}])[0]
        geometry = (city.get("geometry") or {}).get("data") or {}
        lat, lng = geometry.get("lat"), geometry.get("lng")
        if lat is None or lng is None:
            places = source.get("places") or []
            coords = [p.get("location", {}) for p in places if p.get("location", {}).get("lat") is not None]
            if coords:
                lat = sum(p["lat"] for p in coords) / len(coords)
                lng = sum(p["lng"] for p in coords) / len(coords)
        if lat is None or lng is None:
            continue
        counts = (ref.get("metadata") or {}).get("counts") or {}
        char_meta = ((record.get("artifacts") or {}).get("characters") or {}).get("metadata") or {}
        items.append({"city_id": record.get("city_id", ""), "city_name": record.get("city_name", ""),
                      "lat": lat, "lng": lng, "places": counts.get("places", 0),
                      "characters": char_meta.get("characters", 0), "readiness": record.get("readiness", "")})
    return items


def _select_map_scope(world_id: str, scope: str, city_id: str = "") -> dict:
    if scope not in {"world", "city"}:
        raise ValueError("scope must be world or city")
    world, metadata = _aggregate_registered_world(world_id, city_id if scope == "city" else "")
    world = materialize_factual_services(world)
    VisHandler.world = world
    VisHandler.active_world_id = world_id
    VisHandler.map_scope = metadata
    VisHandler.world_path = "" if scope == "world" else str(
        (_registered_city_record(world_id, city_id) or {}).get("artifacts", {}).get("map_world", {}).get("path", "")
    )
    if scope == "city":
        record = _registered_city_record(world_id, city_id) or {}
        artifact = (record.get("artifacts") or {}).get("characters") or {}
        matching = PIPELINE_ROOT / artifact["path"] if artifact.get("status") == "available" else None
        VisHandler.characters_path = str(matching) if matching and matching.is_file() else ""
        VisHandler.characters = read_json(matching) if VisHandler.characters_path else {}
    else:
        VisHandler.characters_path = ""
        VisHandler.characters = _world_characters(world_id)
    return {"ok": True, **metadata}


def _open_registered_world(world_id: str) -> dict:
    """Set the sole browsing-world state used by map and relationship views."""
    migrate_city_versions(world_id)
    manifest_path = WORLD_ROOT / world_id / "world_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"World registry does not exist: {world_id}")
    manifest = read_json(manifest_path)
    active_city_ids = []
    for entry in manifest.get("cities", []):
        record = _registered_city_record(world_id, entry.get("city_id", ""))
        if record and record.get("lifecycle_status", "active") == "active":
            active_city_ids.append(record.get("city_id", ""))
    if active_city_ids:
        result = _select_map_scope(world_id, "world")
    else:
        VisHandler.active_world_id = world_id
        VisHandler.world = {"schema_version": "geoword.world.map_view.v1", "cities": [], "districts": [],
                            "corridors": [], "places": [], "services": [], "information_items": [], "actors": []}
        VisHandler.characters = {"characters": [], "relationships": []}
        VisHandler.world_path = ""
        VisHandler.characters_path = ""
        VisHandler.map_scope = {"scope": "world", "world_id": world_id, "city_ids": []}
        result = {"ok": True, **VisHandler.map_scope}
    result["display_name"] = manifest.get("display_name", world_id)
    return result


def _delete_world(manifest_path: str) -> dict:
    """Delete a generated world output directory by its manifest path."""
    import shutil
    manifest = PIPELINE_ROOT / manifest_path
    try:
        manifest.resolve().relative_to(OUTPUTS_ROOT.resolve())
    except ValueError:
        raise ValueError(f"Path outside outputs directory: {manifest_path}")
    if not manifest.exists():
        raise FileNotFoundError(f"Manifest not found: {manifest_path}")
    world_dir = manifest.parent
    deleted_dir = str(world_dir.relative_to(PIPELINE_ROOT))
    shutil.rmtree(world_dir)
    parent = world_dir.parent
    try:
        next(parent.iterdir())
    except StopIteration:
        parent.rmdir()
    return {"ok": True, "deleted_dir": deleted_dir}


def _save_seed(payload: dict) -> dict:
    """Save a seed JSON to the seeds/ directory, return the relative path."""
    city_id = payload.get("city_id", "").strip()
    city_name = payload.get("city_name", "").strip()
    if not city_id or not city_name:
        raise ValueError("city_id and city_name are required")
    if city_id == "selected_city":
        raise ValueError("城市搜索临时标识 selected_city 不能保存为正式 Seed。请重新选择具体城市。")
    SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{city_id}_seed.json"
    seed_path = SEEDS_DIR / filename
    from shared.io import write_json
    write_json(seed_path, payload)
    rel_path = str(seed_path.relative_to(PIPELINE_ROOT))
    return {"ok": True, "seed_path": rel_path, "filename": filename}


_COUNTRY_LOCALE: dict[str, tuple[str, str]] = {
    "china": ("Asia/Shanghai", "zh-CN"),
    "cn": ("Asia/Shanghai", "zh-CN"),
    "中国": ("Asia/Shanghai", "zh-CN"),
    "japan": ("Asia/Tokyo", "ja-JP"),
    "jp": ("Asia/Tokyo", "ja-JP"),
    "日本": ("Asia/Tokyo", "ja-JP"),
    "united kingdom": ("Europe/London", "en-US"),
    "gb": ("Europe/London", "en-US"),
    "uk": ("Europe/London", "en-US"),
    "英国": ("Europe/London", "en-US"),
    "france": ("Europe/Paris", "en-US"),
    "fr": ("Europe/Paris", "en-US"),
    "法国": ("Europe/Paris", "en-US"),
    "germany": ("Europe/Berlin", "en-US"),
    "de": ("Europe/Berlin", "en-US"),
    "德国": ("Europe/Berlin", "en-US"),
    "italy": ("Europe/Paris", "en-US"),
    "it": ("Europe/Paris", "en-US"),
    "意大利": ("Europe/Paris", "en-US"),
    "spain": ("Europe/Paris", "en-US"),
    "es": ("Europe/Paris", "en-US"),
    "西班牙": ("Europe/Paris", "en-US"),
    "singapore": ("Asia/Singapore", "en-US"),
    "sg": ("Asia/Singapore", "en-US"),
    "新加坡": ("Asia/Singapore", "en-US"),
    "south korea": ("Asia/Seoul", "en-US"),
    "korea": ("Asia/Seoul", "en-US"),
    "kr": ("Asia/Seoul", "en-US"),
    "韩国": ("Asia/Seoul", "en-US"),
    "taiwan": ("Asia/Taipei", "zh-CN"),
    "tw": ("Asia/Taipei", "zh-CN"),
    "台湾": ("Asia/Taipei", "zh-CN"),
    "hong kong": ("Asia/Hong_Kong", "zh-CN"),
    "hk": ("Asia/Hong_Kong", "zh-CN"),
    "香港": ("Asia/Hong_Kong", "zh-CN"),
    "macau": ("Asia/Macau", "zh-CN"),
    "mo": ("Asia/Macau", "zh-CN"),
    "澳门": ("Asia/Macau", "zh-CN"),
    "australia": ("Australia/Sydney", "en-US"),
    "au": ("Australia/Sydney", "en-US"),
    "澳大利亚": ("Australia/Sydney", "en-US"),
    "canada": ("America/Toronto", "en-US"),
    "ca": ("America/Toronto", "en-US"),
    "加拿大": ("America/Toronto", "en-US"),
    "mexico": ("America/Mexico_City", "en-US"),
    "mx": ("America/Mexico_City", "en-US"),
    "墨西哥": ("America/Mexico_City", "en-US"),
    "brazil": ("America/Sao_Paulo", "en-US"),
    "br": ("America/Sao_Paulo", "en-US"),
    "巴西": ("America/Sao_Paulo", "en-US"),
    "india": ("Asia/Kolkata", "en-US"),
    "in": ("Asia/Kolkata", "en-US"),
    "印度": ("Asia/Kolkata", "en-US"),
    "thailand": ("Asia/Bangkok", "en-US"),
    "th": ("Asia/Bangkok", "en-US"),
    "泰国": ("Asia/Bangkok", "en-US"),
    "vietnam": ("Asia/Ho_Chi_Minh", "en-US"),
    "vn": ("Asia/Ho_Chi_Minh", "en-US"),
    "越南": ("Asia/Ho_Chi_Minh", "en-US"),
    "malaysia": ("Asia/Kuala_Lumpur", "en-US"),
    "my": ("Asia/Kuala_Lumpur", "en-US"),
    "马来西亚": ("Asia/Kuala_Lumpur", "en-US"),
    "indonesia": ("Asia/Jakarta", "en-US"),
    "id": ("Asia/Jakarta", "en-US"),
    "印度尼西亚": ("Asia/Jakarta", "en-US"),
    "united arab emirates": ("Asia/Dubai", "en-US"),
    "ae": ("Asia/Dubai", "en-US"),
    "阿联酋": ("Asia/Dubai", "en-US"),
    "russia": ("Europe/Moscow", "en-US"),
    "ru": ("Europe/Moscow", "en-US"),
    "俄罗斯": ("Europe/Moscow", "en-US"),
    "netherlands": ("Europe/Amsterdam", "en-US"),
    "nl": ("Europe/Amsterdam", "en-US"),
    "荷兰": ("Europe/Amsterdam", "en-US"),
    "switzerland": ("Europe/Zurich", "en-US"),
    "ch": ("Europe/Zurich", "en-US"),
    "瑞士": ("Europe/Zurich", "en-US"),
    "united states": ("America/New_York", "en-US"),
    "usa": ("America/New_York", "en-US"),
    "us": ("America/New_York", "en-US"),
    "美国": ("America/New_York", "en-US"),
}


def _match_country_locale(country: str, country_code: str) -> tuple[str, str] | None:
    for token in (country_code, country):
        key = (token or "").strip().lower()
        if not key:
            continue
        if key in _COUNTRY_LOCALE:
            return _COUNTRY_LOCALE[key]
        for name, locale in _COUNTRY_LOCALE.items():
            if name in key or key in name:
                return locale
    return None


def _infer_locale(country: str, lat: float, lng: float, country_code: str = "") -> dict[str, str]:
    """Infer timezone + UI language from country name and coordinates."""
    matched = _match_country_locale(country, country_code)
    if matched:
        tz, lang = matched
        display_country = country or country_code
        if country_code and country and country_code.upper() not in country:
            # Prefer English country name for seed when available alongside code.
            pass
        return {"timezone": tz, "language": lang, "country": display_country or country_code}
    if "united states" in (country or "").lower() or (country_code or "").upper() == "US":
        tz = "America/Los_Angeles" if lng < -100 else "America/New_York"
        return {"timezone": tz, "language": "en-US", "country": country or "United States"}
    if lng >= 100:
        return {"timezone": "Asia/Shanghai", "language": "zh-CN", "country": country}
    if lng >= 30:
        return {"timezone": "Europe/London", "language": "en-US", "country": country}
    if lng >= -30:
        return {"timezone": "Europe/London", "language": "en-US", "country": country}
    return {"timezone": "America/New_York", "language": "en-US", "country": country}


def _parse_address_components(components: list[dict]) -> tuple[str, str, str]:
    country = ""
    country_code = ""
    city_name = ""
    for comp in components:
        types = comp.get("types", [])
        if "country" in types:
            country = comp.get("long_name", "")
            country_code = comp.get("short_name", "")
        if "locality" in types:
            city_name = comp.get("long_name", "")
        elif "administrative_area_level_1" in types and not city_name:
            city_name = comp.get("long_name", "")
    return country, country_code, city_name


def _candidate_city_id(city_name: str, country: str) -> str:
    """Stable local seed id derived from an explicitly selected city candidate."""
    slug = re.sub(r"[^a-z0-9]+", "_", f"{city_name}_{country}".lower()).strip("_")
    if not slug:
        raise ValueError("城市解析未返回英文规范名称，不能创建正式城市记录。")
    return slug


def _normalized_geocode_candidate(
    *, lat: float, lng: float, formatted_address: str, city_name: str,
    country: str, timezone: str, language: str, bbox: dict, source: str,
) -> dict:
    return {
        "lat": lat,
        "lng": lng,
        "formatted_address": formatted_address,
        "city_name": city_name,
        "country": country,
        "timezone": timezone,
        "language": language,
        "bbox": bbox,
        "source": source,
        "city_id": _candidate_city_id(city_name, country),
    }


def _city_search_json(url: str, *, headers: dict[str, str] | None = None) -> dict | list:
    """Read an external city-search response without inheriting shell proxies.

    The desktop terminal may contain a development-only proxy.  City lookup must
    not silently route provider traffic through that unrelated process.
    """
    request = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=8) as response:
            return json.loads(response.read())
    except Exception as exc:
        raise RuntimeError("城市搜索服务暂时不可用") from exc


def _google_geocode_candidates(query: str, api_key: str, limit: int = 5) -> list[dict]:
    data = _city_search_json(
        "https://maps.googleapis.com/maps/api/geocode/json?" + urlencode({
            "address": query, "key": api_key,
        })
    )
    if not isinstance(data, dict) or data.get("status") not in {"OK", "ZERO_RESULTS"}:
        raise RuntimeError("Google 城市解析暂时不可用")
    candidates = []
    for result in data.get("results", [])[:limit]:
        loc = result["geometry"]["location"]
        viewport = result.get("geometry", {}).get("viewport", {})
        country, country_code, city_name = _parse_address_components(result.get("address_components", []))
        locale = _infer_locale(country, loc["lat"], loc["lng"], country_code)
        candidates.append(_normalized_geocode_candidate(
            lat=loc["lat"], lng=loc["lng"], formatted_address=result.get("formatted_address", query),
            city_name=city_name or query.split(",")[0].strip(), country=locale["country"] or country,
            timezone=locale["timezone"], language=locale["language"], source="google_geocoding",
            bbox={
                "north": viewport.get("northeast", {}).get("lat", loc["lat"] + 0.1),
                "south": viewport.get("southwest", {}).get("lat", loc["lat"] - 0.1),
                "east": viewport.get("northeast", {}).get("lng", loc["lng"] + 0.1),
                "west": viewport.get("southwest", {}).get("lng", loc["lng"] - 0.1),
            },
        ))
    return candidates


def _nominatim_candidates(query: str, limit: int = 5) -> list[dict]:
    data = _city_search_json(
        "https://nominatim.openstreetmap.org/search?" + urlencode({
            "q": query, "format": "json", "limit": limit, "addressdetails": 1,
        }),
        headers={"User-Agent": "GeoAgentBench-Vis/1.0"},
    )
    if not isinstance(data, list):
        raise RuntimeError("备用城市搜索服务暂时不可用")
    candidates = []
    for result in data[:limit]:
        lat, lng = float(result["lat"]), float(result["lon"])
        bounding_box = result.get("boundingbox", [str(lat - 0.05), str(lat + 0.05), str(lng - 0.05), str(lng + 0.05)])
        address = result.get("address", {})
        country = address.get("country", "")
        country_code = address.get("country_code", "")
        city_name = address.get("city") or address.get("town") or address.get("village") or address.get("state") or query.split(",")[0].strip()
        locale = _infer_locale(country, lat, lng, country_code)
        candidates.append(_normalized_geocode_candidate(
            lat=lat, lng=lng, formatted_address=result.get("display_name", query), city_name=city_name,
            country=locale["country"] or country, timezone=locale["timezone"], language=locale["language"],
            source="nominatim", bbox={
                "south": float(bounding_box[0]), "north": float(bounding_box[1]),
                "west": float(bounding_box[2]), "east": float(bounding_box[3]),
            },
        ))
    return candidates


def _geocode_candidates(query: str, limit: int = 5) -> list[dict]:
    """Compatibility resolution with Google first and a visible OSM fallback."""
    query = query.strip()
    if not query:
        raise ValueError("Query is empty")
    api_key = VisHandler.google_maps_api_key
    if api_key:
        try:
            candidates = _google_geocode_candidates(query, api_key, limit)
            if candidates:
                return candidates
        except RuntimeError:
            pass
    candidates = _nominatim_candidates(query, limit)
    if not candidates:
        raise ValueError("没有找到可选城市")
    return candidates


def _city_search_suggestions(query: str, session_id: str, limit: int = 5) -> dict:
    """Return Google city predictions; resolve a chosen prediction in a second call."""
    query = query.strip()
    if not query:
        raise ValueError("请输入城市名称")
    started_at = time.time()
    api_key = VisHandler.google_maps_api_key
    calls: list[str] = []
    if api_key:
        try:
            calls.append("google_places_autocomplete")
            data = _city_search_json(
                "https://maps.googleapis.com/maps/api/place/autocomplete/json?" + urlencode({
                    "input": query, "types": "(cities)", "language": "en",
                    "sessiontoken": session_id, "key": api_key,
                })
            )
            if not isinstance(data, dict) or data.get("status") not in {"OK", "ZERO_RESULTS"}:
                raise RuntimeError("Google 城市联想暂时不可用")
            suggestions = [{
                "place_id": item.get("place_id", ""),
                "label": item.get("description", ""),
                "primary_text": (item.get("structured_formatting") or {}).get("main_text", item.get("description", "")),
                "secondary_text": (item.get("structured_formatting") or {}).get("secondary_text", ""),
                "source": "google_places_autocomplete",
                "requires_resolve": True,
            } for item in data.get("predictions", [])[:limit] if item.get("place_id")]
            if suggestions:
                _record_city_search({"operation": "suggest", "query": query, "session_id": session_id,
                                     "provider": "google_places_autocomplete", "api_call_count": len(calls),
                                     "result_count": len(suggestions), "fallback_used": False,
                                     "elapsed_seconds": round(time.time() - started_at, 3)})
                return {"suggestions": suggestions, "provider": "google_places_autocomplete", "fallback_used": False}
        except RuntimeError:
            pass
    calls.append("fallback_geocoding")
    candidates = _geocode_candidates(query, limit)
    suggestions = [{
        "place_id": "", "label": item["formatted_address"], "primary_text": item["city_name"],
        "secondary_text": item.get("country", ""), "source": item["source"],
        "requires_resolve": False, "resolved": item,
    } for item in candidates]
    _record_city_search({"operation": "suggest", "query": query, "session_id": session_id,
                         "provider": suggestions[0]["source"] if suggestions else "none", "api_call_count": len(calls),
                         "result_count": len(suggestions), "fallback_used": True,
                         "elapsed_seconds": round(time.time() - started_at, 3)})
    return {"suggestions": suggestions, "provider": suggestions[0]["source"] if suggestions else "none", "fallback_used": True}


def _resolve_city_suggestion(place_id: str, session_id: str) -> dict:
    """Resolve a selected Google prediction to the normalized Seed city fields."""
    if not place_id:
        raise ValueError("请选择一个城市候选项")
    api_key = VisHandler.google_maps_api_key
    if not api_key:
        raise RuntimeError("Google 城市解析当前不可用；请重新搜索并选择备用结果")
    started_at = time.time()
    try:
        data = _city_search_json(
            "https://maps.googleapis.com/maps/api/place/details/json?" + urlencode({
                "place_id": place_id, "fields": "address_component,geometry,formatted_address,name",
                "language": "en", "sessiontoken": session_id, "key": api_key,
            })
        )
        if not isinstance(data, dict) or data.get("status") != "OK" or not data.get("result"):
            raise RuntimeError("Google 城市解析暂时不可用")
        result = data["result"]
        location = (result.get("geometry") or {}).get("location") or {}
        lat, lng = float(location["lat"]), float(location["lng"])
        viewport = (result.get("geometry") or {}).get("viewport", {})
        country, country_code, city_name = _parse_address_components(result.get("address_components", []))
        locale = _infer_locale(country, lat, lng, country_code)
        resolved = _normalized_geocode_candidate(
            lat=lat, lng=lng, formatted_address=result.get("formatted_address") or result.get("name", ""),
            city_name=city_name or result.get("name", ""), country=locale["country"] or country,
            timezone=locale["timezone"], language=locale["language"], source="google_places_details",
            bbox={
                "north": viewport.get("northeast", {}).get("lat", lat + 0.1),
                "south": viewport.get("southwest", {}).get("lat", lat - 0.1),
                "east": viewport.get("northeast", {}).get("lng", lng + 0.1),
                "west": viewport.get("southwest", {}).get("lng", lng - 0.1),
            },
        )
    except RuntimeError:
        _record_city_search({"operation": "resolve", "session_id": session_id, "provider": "google_places_details",
                             "api_call_count": 1, "result_count": 0, "fallback_used": False,
                             "outcome": "failed", "elapsed_seconds": round(time.time() - started_at, 3)})
        raise
    _record_city_search({"operation": "resolve", "session_id": session_id, "provider": "google_places_details",
                         "api_call_count": 1, "result_count": 1, "fallback_used": False,
                         "outcome": "resolved", "elapsed_seconds": round(time.time() - started_at, 3)})
    return resolved


def _geocode(query: str) -> dict:
    """Compatibility endpoint: return the first explicit city candidate."""
    return _geocode_candidates(query, limit=1)[0]


class VisHandler(SimpleHTTPRequestHandler):
    """HTTP handler that serves /api/* endpoints and static files."""

    world: dict = {}
    characters: dict = {}
    world_path: str = ""
    characters_path: str = ""
    google_maps_api_key: str = ""
    active_world_id: str = ""
    map_scope: dict = {}

    def do_GET(self):
        from vis.tasks.server import serve_tasks
        if serve_tasks(self, "GET"):
            return
        from shared.web_i18n import serve_ui_asset
        if serve_ui_asset(self):
            return
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        try:
            if path == "/api/config":
                self._json({
                    "google_maps_api_key": self.google_maps_api_key,
                    "preferred_basemap": "google" if self.google_maps_api_key else "osm",
                })
            elif path == "/api/place-type-catalog":
                self._json(place_type_catalog_payload())
            elif path == "/api/summary":
                chars = self.characters.get("characters", []) if self.characters else None
                self._json(world_to_geojson_summary(self.world, chars))
            elif path == "/api/districts":
                self._json(districts_to_geojson(
                    self.world.get("districts", []),
                    places=self.world.get("places", []),
                ))
            elif path == "/api/corridors":
                self._json(corridors_to_geojson(
                    self.world.get("corridors", []),
                    districts=self.world.get("districts", []),
                    places=self.world.get("places", []),
                ))
            elif path.startswith("/api/corridors/") and path.endswith("/detail"):
                corridor_id = path.split("/")[3]
                corridor = self._find_by_id(self.world.get("corridors", []), "corridor_id", corridor_id)
                if corridor:
                    self._json(corridor_detail(
                        corridor,
                        self.world.get("districts", []),
                        self.world.get("places", []),
                        self.world.get("information_items", []),
                    ))
                else:
                    self._error(f"Corridor {corridor_id} not found", 404)
            elif path == "/api/places":
                self._json(places_to_geojson(
                    self.world.get("places", []),
                    q=params.get("q", [None])[0],
                    category=params.get("category", [None])[0],
                    min_rating=float(params["min_rating"][0]) if params.get("min_rating") else None,
                    district_id=params.get("district_id", [None])[0],
                ))
            elif path.startswith("/api/places/") and path.endswith("/detail"):
                place_id = path.split("/")[3]
                place = self._find_by_id(self.world.get("places", []), "place_id", place_id)
                if place:
                    self._json(place_detail(
                        place,
                        self.world.get("services", []),
                        self.world.get("information_items", []),
                        districts=self.world.get("districts", []),
                        corridors=self.world.get("corridors", []),
                    ))
                else:
                    self._error(f"Place {place_id} not found", 404)
            elif path == "/api/actors":
                district_map = {d.get("district_id"): d for d in self.world.get("districts", [])}
                chars = self.characters.get("characters", []) if self.characters else None
                rels = self.characters.get("relationships", []) if self.characters else None
                self._json(actors_to_geojson(
                    self.world.get("actors", []),
                    characters=chars,
                    district_map=district_map,
                    relationships=rels,
                ))
            elif path.startswith("/api/actors/") and path.endswith("/detail"):
                actor_id = path.split("/")[3]
                actor = self._find_by_id(self.world.get("actors", []), "actor_id", actor_id)
                chars = self.characters.get("characters", []) if self.characters else None
                if not actor and chars:
                    character = self._find_by_id(chars, "character_id", actor_id)
                    if character:
                        actor = {
                            "actor_id": character.get("character_id"),
                            "name": character.get("name"),
                            "relationship_to_user": character.get("role", ""),
                            "preferences": character.get("preferences", {}),
                            "mobility": character.get("mobility", {}),
                            "availability": character.get("availability", {}),
                            "communication": character.get("communication", {}),
                            "constraints": character.get("constraints", []),
                            "is_character": True,
                        }
                if actor:
                    self._json(character_detail(actor, chars))
                else:
                    self._error(f"Actor {actor_id} not found", 404)
            elif path == "/api/characters_data":
                scope = params.get("scope", ["current_city"])[0]
                city_id = params.get("city_id", [""])[0]
                world_id = params.get("world_id", [self.active_world_id])[0] or self.active_world_id
                self._json(_world_characters(world_id, city_id=city_id) if scope == "world" else (self.characters or {}))
            elif path == "/api/stats":
                self._json(stats(self.world))
            elif path == "/api/events":
                self._json(self.world.get("event_templates", []))
            elif path == "/api/info":
                place_id = params.get("place_id", [None])[0]
                info = [
                    i for i in self.world.get("information_items", [])
                    if i.get("about", {}).get("place_id") == place_id
                ]
                self._json(info)
            elif path == "/api/worlds":
                self._json({"worlds": _list_worlds(), "current_world_path": self.world_path})
            elif path == "/api/world-registry":
                self._json({"worlds": _registered_worlds(), "active_world_id": self.active_world_id})
            elif path == "/api/world-registry/version-context":
                world_id = params.get("world_id", [self.active_world_id])[0] or self.active_world_id
                self._json(city_version_context(world_id))
            elif path == "/api/world-registry/city-detail":
                world_id = params.get("world_id", [self.active_world_id])[0] or self.active_world_id
                city_id = params.get("city_id", [""])[0]
                if not world_id or not city_id:
                    self._error("world_id and city_id are required", 400)
                else:
                    self._json(_registered_city_detail(world_id, city_id))
            elif path == "/api/map-scope":
                self._json(self.map_scope or {"scope": "city", "world_id": self.active_world_id})
            elif path == "/api/world-overview":
                self._json({"world_id": self.active_world_id, "cities": _world_overview(self.active_world_id)})
            elif path == "/api/seeds":
                self._json({"seeds": _list_seeds()})
            elif path == "/api/geocode":
                q = params.get("q", [""])[0]
                if not q:
                    self._error("Missing 'q' parameter", 400)
                else:
                    self._json(_geocode(q))
            elif path == "/api/geocode/candidates":
                q = params.get("q", [""])[0]
                if not q:
                    self._error("Missing 'q' parameter", 400)
                else:
                    self._json({"candidates": _geocode_candidates(q)})
            elif path == "/api/city-search/suggest":
                q = params.get("q", [""])[0]
                session_id = params.get("session_id", [""])[0] or uuid.uuid4().hex
                self._json(_city_search_suggestions(q, session_id))
            elif path == "/api/jobs/latest":
                world_id = params.get("world_id", [self.active_world_id])[0] or self.active_world_id
                job = _latest_world_job(world_id)
                self._json({"job": _job_response(job) if job else None})
            elif path.startswith("/api/seeds/") and path.endswith("/detail"):
                seed_name = path.split("/")[3]
                seed_path = SEEDS_DIR / seed_name
                if not seed_path.exists():
                    self._error(f"Seed {seed_name} not found", 404)
                else:
                    self._json(read_json(seed_path))
            elif path.startswith("/api/jobs/"):
                job_id = path.split("/")[3]
                job = JOBS.get(job_id) or _load_persisted_job(job_id)
                if not job:
                    self._error(f"Job {job_id} not found", 404)
                else:
                    self._json(_job_response(job))
            elif path == "/" or path == "/index.html":
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path.replace("/static/", ""))
            elif path.endswith(".js"):
                self._serve_static(path.lstrip("/"))
            elif path.endswith(".css"):
                self._serve_static(path.lstrip("/"))
            elif path.startswith("/api/"):
                self._error(f"Unknown API endpoint: {path}", 404)
            else:
                self._serve_static("index.html")
        except Exception as exc:
            self._error(str(exc), 500)

    def do_DELETE(self):
        from vis.tasks.server import serve_tasks
        if not serve_tasks(self, "DELETE"):
            self._error("Unknown API endpoint", 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        from vis.tasks.server import serve_tasks
        if serve_tasks(self, "POST"):
            return
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length else b""
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}
        try:
            if path == "/api/generate":
                seed_path = payload.get("seed", "")
                geo_provider = payload.get("geo_provider", "auto")
                force = bool(payload.get("force", False))
                if not seed_path:
                    self._error("Missing 'seed' field", 400)
                    return
                world_id = str(payload.get("world_id", self.active_world_id)).strip()
                expansion_city_id = str(payload.get("expansion_city_id", "")).strip()
                job_id = _start_generation(
                    seed_path, geo_provider, force, world_id, expansion_city_id,
                    payload.get("expansion_targets"),
                    payload.get("generation_targets"),
                )
                self._json({"job_id": job_id, "status": "running"})
            elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = path.split("/")[3]
                try:
                    job = _cancel_world_job(job_id)
                except LookupError as exc:
                    self._error(str(exc), 404)
                    return
                except ValueError as exc:
                    self._error(str(exc), 409)
                    return
                self._json({"job": _job_response(job)})
            elif path.startswith("/api/jobs/") and path.endswith("/retry"):
                job_id = path.split("/")[3]
                previous = JOBS.get(job_id) or _load_persisted_job(job_id)
                if not previous:
                    self._error(f"Job {job_id} not found", 404)
                    return
                if previous.get("status") not in {"failed", "cancelled", "target_not_reached", "awaiting_registration_decision"}:
                    self._error("仅可继续或重新构建已失败、已取消或目标未达标的任务。", 400)
                    return
                retry_mode = str(payload.get("retry_mode", "continue"))
                if retry_mode not in {"continue", "rebuild"}:
                    self._error("retry_mode 必须为 continue 或 rebuild", 400)
                    return
                if retry_mode == "continue":
                    retry_job_id = _start_resumed_generation(previous)
                    self._json({"job_id": retry_job_id, "status": "running", "retry_of": job_id,
                                "retry_mode": retry_mode, "resume_plan": JOBS[retry_job_id].get("resume", {})})
                else:
                    retry_job_id = _start_generation(
                        previous["seed"], previous["geo_provider"], True,
                        previous["world_id"], previous.get("expansion_city_id", ""),
                        previous.get("expansion_plan", {}).get("targets") if previous.get("expansion_plan") else None,
                        previous.get("requested_targets"),
                    )
                    retry_job = JOBS[retry_job_id]
                    retry_job["retry_of"] = job_id
                    retry_job["rebuild_of"] = job_id
                    retry_job["rebuild_reason"] = "user_requested_full_rebuild"
                    _persist_world_job(retry_job)
                    self._json({"job_id": retry_job_id, "status": "running", "retry_of": job_id,
                                "retry_mode": retry_mode})
            elif path.startswith("/api/jobs/") and path.endswith("/register-actual"):
                job_id = path.split("/")[3]
                previous = JOBS.get(job_id) or _load_persisted_job(job_id)
                if not previous:
                    self._error(f"Job {job_id} not found", 404)
                    return
                if previous.get("status") not in {"awaiting_registration_decision", "target_not_reached"}:
                    self._error("仅可对待确认的候选版本按实际产物登记", 409)
                    return
                if payload.get("confirm") is not True:
                    self._error("必须明确传入 confirm=true 才能按实际产物登记", 400)
                    return
                try:
                    registration_job_id = _start_actual_registration(previous)
                except ValueError as exc:
                    self._error(str(exc), 409)
                    return
                self._json({"job_id": registration_job_id, "status": "running", "registration_of": job_id,
                            "mode": "user_confirmed_actual"})
            elif path == "/api/city-search/resolve":
                self._json(_resolve_city_suggestion(
                    str(payload.get("place_id", "")).strip(),
                    str(payload.get("session_id", "")).strip() or uuid.uuid4().hex,
                ))
            elif path == "/api/world-registry/create":
                world_id = str(payload.get("world_id", "")).strip()
                display_name = str(payload.get("display_name", "")).strip() or None
                result = create_empty_world(world_id, display_name=display_name)
                self._json({"world_id": result["world_id"], "city_count": result["city_count"]})
            elif path == "/api/seeds/save":
                result = _save_seed(payload)
                self._json(result)
            elif path == "/api/worlds/load":
                world_path = payload.get("world_path", "")
                if not world_path:
                    self._error("Missing 'world_path' field", 400)
                    return
                result = _load_world_into_server(world_path)
                self._json(result)
            elif path == "/api/worlds/open":
                world_id = str(payload.get("world_id") or "").strip()
                if not world_id:
                    self._error("world_id is required", 400)
                    return
                self._json(_open_registered_world(world_id))
            elif path == "/api/worlds/select-scope":
                world_id = str(payload.get("world_id") or self.active_world_id).strip()
                scope = str(payload.get("scope") or "city").strip()
                city_id = str(payload.get("city_id") or "").strip()
                if not world_id or (scope == "city" and not city_id):
                    self._error("world_id and city_id (for city scope) are required", 400)
                    return
                self._json(_select_map_scope(world_id, scope, city_id))
            elif path == "/api/world-registry/city-lifecycle":
                world_id = str(payload.get("world_id") or "").strip()
                city_id = str(payload.get("city_id") or "").strip()
                status = str(payload.get("lifecycle_status") or "").strip()
                self._json(set_city_lifecycle_status(world_id, city_id, status))
            elif path == "/api/world-registry/detach-city":
                world_id = str(payload.get("world_id") or "").strip()
                city_id = str(payload.get("city_id") or "").strip()
                self._json(detach_city_from_world(world_id, city_id))
            elif path == "/api/world-registry/select-city-version":
                world_id = str(payload.get("world_id") or self.active_world_id).strip()
                city_id = str(payload.get("city_id") or "").strip()
                version = payload.get("version")
                if not world_id or not city_id:
                    self._error("world_id and city_id are required", 400)
                    return
                result = select_city_version(world_id, city_id, version)
                # Rebuild the active in-memory view immediately so the map and
                # relationship graph observe the selected data boundary.
                if self.active_world_id == world_id:
                    scope = (self.map_scope or {}).get("scope", "world")
                    scoped_city = (self.map_scope or {}).get("city_id", "")
                    _select_map_scope(world_id, scope, scoped_city if scope == "city" else "")
                self._json(result)
            elif path == "/api/worlds/delete":
                manifest_path = payload.get("manifest_path", "")
                if not manifest_path:
                    self._error("Missing 'manifest_path' field", 400)
                    return
                result = _delete_world(manifest_path)
                self._json(result)
            else:
                self._error(f"Unknown POST endpoint: {path}", 404)
        except Exception as exc:
            self._error(str(exc), 500)

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, filename: str):
        static_dir = Path(__file__).resolve().parent / "static"
        file_path = static_dir / filename
        if not file_path.exists() or not file_path.is_file():
            self._error(f"Not found: {filename}", 404)
            return
        content = file_path.read_bytes()
        content_type = "text/html"
        if filename.endswith(".js"):
            content_type = "application/javascript"
        elif filename.endswith(".css"):
            content_type = "text/css"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.end_headers()
        self.wfile.write(content)

    def _error(self, msg: str, code: int = 500):
        body = json.dumps({"error": msg}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _find_by_id(self, items: list[dict], key: str, value: str) -> dict | None:
        for item in items:
            if item.get(key) == value:
                return item
        return None

    def log_message(self, format, *args):
        """Suppress default log spam; only log API calls."""
        if "/api/" in (args[0] if args else ""):
            super().log_message(format, *args)


def _resolve_google_maps_api_key(explicit: str = "") -> str:
    if explicit:
        return explicit
    env_key = os.environ.get("GOOGLE_MAPS_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if env_key:
        return env_key
    try:
        config = PipelineConfig()
        # Keep the browser basemap on the same configured credential profile
        # that the Google data client starts with.  Previously the viewer
        # always used ``google_maps`` while data generation respected
        # ``google_maps_start_key`` (currently ``google_maps_3``).
        selected_profile = str(config._raw.get("google_maps_browser_key")
                               or config._raw.get("google_maps_start_key") or "google_maps")
        selected = config._raw.get(selected_profile, {})
        if isinstance(selected, dict) and selected.get("api_key"):
            return str(selected["api_key"])
        return config.google_maps_api_key
    except Exception:
        return ""


def _world_characters(world_id: str, *, city_id: str = "") -> dict:
    """Aggregate exact-city character artifacts; never infer cross-city edges."""
    manifest_path = WORLD_ROOT / world_id / "world_manifest.json"
    if not manifest_path.is_file():
        return {"scope": "world", "world_id": world_id, "characters": [], "relationships": []}
    manifest = read_json(manifest_path)
    characters: list[dict] = []
    relationships: list[dict] = []
    included_cities: list[str] = []
    requested_city_id = city_id
    for entry in manifest.get("cities", []):
        if requested_city_id and entry.get("city_id") != requested_city_id:
            continue
        city_record_path = PIPELINE_ROOT / entry.get("registry_path", "")
        if not city_record_path.is_file():
            continue
        record = read_json(city_record_path)
        if record.get("lifecycle_status", "active") != "active":
            continue
        artifact = record.get("artifacts", {}).get("characters", {})
        if artifact.get("status") != "available":
            continue
        payload = read_json(PIPELINE_ROOT / artifact["path"])
        source_city_id = record.get("city_id", "")
        included_cities.append(source_city_id)
        id_map: dict[str, str] = {}
        for character in payload.get("characters", []):
            enriched = dict(character)
            enriched.setdefault("city_id", source_city_id)
            raw_id = str(enriched.get("character_id", ""))
            # Keep graph node identifiers globally unique while preserving the
            # original id for traceability and relationship reconstruction.
            scoped_id = f"{source_city_id}::{raw_id}" if source_city_id else raw_id
            id_map[raw_id] = scoped_id
            enriched["source_character_id"] = raw_id
            enriched["character_id"] = scoped_id
            characters.append(enriched)
        for relationship in payload.get("relationships", []):
            enriched = dict(relationship)
            enriched.setdefault("scope", "city")
            enriched["character_id"] = id_map.get(str(enriched.get("character_id", "")), enriched.get("character_id", ""))
            enriched["related_id"] = id_map.get(str(enriched.get("related_id", "")), enriched.get("related_id", ""))
            relationships.append(enriched)
    return {
        "scope": "city" if requested_city_id else "world",
        "world_id": world_id,
        "city_id": requested_city_id,
        "city_ids": included_cities,
        "characters": characters,
        "relationships": relationships,
        "note": "Only exact-city artifacts are aggregated; cross-city relationships are not inferred.",
    }


def make_server(
    world_path: str,
    characters_path: str = "",
    port: int = 8765,
    google_maps_api_key: str = "",
    world_id: str = "",
    host: str = "127.0.0.1",
) -> HTTPServer:
    """Create and configure the visualization server."""
    raw_world = read_json(world_path) if world_path else {}
    sidecar = Path(world_path).parent / "factual_service_index.json" if world_path else None
    indexed_services = read_factual_service_index(sidecar, raw_world) if sidecar and sidecar.is_file() else None
    world = materialize_factual_services(raw_world, indexed_services=indexed_services)
    characters = read_json(characters_path) if characters_path else {}

    VisHandler.world = world
    VisHandler.characters = characters
    VisHandler.world_path = world_path
    VisHandler.characters_path = characters_path
    VisHandler.google_maps_api_key = _resolve_google_maps_api_key(google_maps_api_key)
    VisHandler.active_world_id = world_id
    city = (world.get("cities") or [{}])[0]
    VisHandler.map_scope = {"scope": "city", "world_id": world_id, "city_id": city.get("city_id", ""),
                            "city_ids": [city.get("city_id", "")] if city.get("city_id") else []}

    server = HTTPServer((host, port), VisHandler)
    return server


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="GeoWorld world visualization server")
    parser.add_argument("--world", default="", help="Optional path to world.json")
    parser.add_argument("--world-id", default="world_001", help="Registered world to load when --world is omitted")
    parser.add_argument("--characters", default="", help="Path to characters.json (optional)")
    parser.add_argument("--port", type=int, default=8769, help="Server port (default 8769)")
    parser.add_argument(
        "--google-maps-api-key",
        default="",
        help="Google Maps JavaScript API key (or set GOOGLE_MAPS_API_KEY / GOOGLE_API_KEY, or config.yaml → google_maps.api_key)",
    )
    args = parser.parse_args()

    world_path = args.world or _default_world_path(args.world_id)
    if not world_path:
        parser.error("Provide --world or create a registered world with at least one city.")
    initial_city_id = read_json(world_path).get("cities", [{}])[0].get("city_id", "")
    initial_record = _registered_city_record(args.world_id, initial_city_id) or {}
    initial_char = (initial_record.get("artifacts") or {}).get("characters") or {}
    characters_path = args.characters or (
        str(PIPELINE_ROOT / initial_char["path"])
        if initial_char.get("status") == "available" else ""
    )
    server = make_server(
        world_path=world_path,
        characters_path=characters_path,
        port=args.port,
        google_maps_api_key=args.google_maps_api_key,
        world_id=args.world_id,
    )
    # A registered world is the initial browsing boundary. The first city
    # artifact only bootstraps the server and must not decide the map scope.
    if not args.world:
        _open_registered_world(args.world_id)

    print(f"==============================================")
    print(f"  GeoWorld World Studio")
    print(f"  World:      {world_path}")
    if characters_path:
        print(f"  Characters: {characters_path}")
    print(f"  Google Map: {'enabled' if VisHandler.google_maps_api_key else 'disabled'}")
    print(f"  Open:       http://localhost:{args.port}")
    print(f"==============================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()
