"""Recoverable multi-step discovery session runner."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.io import read_json, write_json
from shared.config_loader import PipelineConfig
from shared.llm_client import OpenAICompatibleChatClient, YunwuResponsesClient
from world_module.map._internal.discovery.assembler import assemble_discovery
from world_module.map._internal.discovery import prompts
from world_module.map._internal.discovery.quality import (
    corridor_validation_reason,
    dedupe_named,
    district_validation_reason,
    valid_corridor,
    valid_district,
)
from world_module.map.seed import CitySeed
from world_module.map._internal.yunwu_city_search import extract_json_object, extract_response_text


_PROGRESS_LOCK = threading.Lock()
MAX_AUTOMATIC_STRUCTURAL_REPAIR_ROUNDS = 2


@dataclass
class DiscoverySessionRunner:
    model: str = "gpt-5.5"
    endpoint_name: str = "cliproxy_gpt55_pro"
    config_path: str | None = None
    max_workers: int = 4
    web_search_max_workers: int = 1
    timeout_seconds: int = 300
    web_search_timeout_seconds: int = 600
    max_attempts: int = 2
    force: bool = False
    structural_repair_batch: int = 1

    def run(self, seed: CitySeed, session_dir: str | Path) -> dict[str, Any]:
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        endpoint = PipelineConfig(self.config_path).model_endpoint(self.endpoint_name)
        self._chat_provider = endpoint.get("provider") in {"openai_compatible", "openai_chat"}
        self.client = (
            OpenAICompatibleChatClient(
                base_url=endpoint.get("base_url", ""), api_key=endpoint.get("api_key", ""),
                model=endpoint.get("model", self.model), timeout_seconds=self.timeout_seconds,
                endpoint_name=self.endpoint_name,
                pricing_reference=endpoint.get("pricing_reference"),
            )
            if self._chat_provider
            else YunwuResponsesClient(
                model=self.model, config_path=self.config_path,
                endpoint_name=self.endpoint_name, timeout_seconds=self.timeout_seconds,
            )
        )
        self._state_lock = threading.Lock()
        state = self._load_state(seed)
        max_districts = _hard_cap(seed.district_count)
        max_corridors = _hard_cap(seed.corridor_count)

        city_outline = self._step_json(
            "01_city_outline",
            lambda: self._call_json(prompts.city_outline_prompt(seed), use_web_search=True,
                                    max_output_tokens=9000, logical_step="city_outline"),
            state,
        )

        district_groups = city_outline.get("district_group_specs", []) or self._fallback_district_groups(seed)
        district_docs = self._run_group_steps(
            prefix="02_districts",
            groups=district_groups,
            prompt_fn=lambda group: prompts.districts_by_group_prompt(seed, city_outline, group),
            state=state,
            use_web_search=True,
            max_output_tokens=6500,
        )

        districts, district_audit = _collect_structural_candidates(
            district_docs, "district_candidates", district_validation_reason, max_districts,
        )

        corridor_group_doc = self._step_json(
            "03_corridor_groups",
            lambda: self._call_json(
                prompts.corridor_groups_refine_prompt(seed, city_outline, districts),
                use_web_search=False,
                max_output_tokens=5000, logical_step="corridor_refinement",
            ),
            state,
        )
        corridor_groups = corridor_group_doc.get("corridor_group_specs") or city_outline.get("corridor_group_specs", [])
        corridor_groups = corridor_groups or self._fallback_corridor_groups(seed)
        corridor_docs = self._run_group_steps(
            prefix="04_corridors",
            groups=corridor_groups,
            prompt_fn=lambda group: prompts.corridors_by_group_prompt(seed, city_outline, districts, group),
            state=state,
            use_web_search=True,
            max_output_tokens=8000,
        )

        corridors, corridor_audit = _collect_structural_candidates(
            corridor_docs, "corridor_candidates", corridor_validation_reason, max_corridors,
        )
        districts, corridors, structural_report = self._repair_structural_shortfalls(
            seed, state, city_outline, district_docs, corridor_docs,
            districts, corridors, district_audit, corridor_audit,
            max_districts, max_corridors,
        )
        structural_report["operation_ledger"] = self.client.telemetry_snapshot()
        write_json(self._structural_repair_dir() / "report.json", structural_report)
        if structural_report["remaining_gaps"]:
            report_path = self._structural_repair_dir() / "shortfall.json"
            write_json(report_path, structural_report)
            state["status"] = "target_not_reached"
            state["structural_shortfall"] = str(report_path)
            state["operation_ledger"] = structural_report["operation_ledger"]
            state["updated_at"] = _now()
            self._write_state(state)
            _emit_progress("structural_top_up", "failed", label="\u57ce\u5e02\u7ed3\u6784\u81ea\u52a8\u8865\u9f50\u540e\u4ecd\u672a\u8fbe\u6807",
                           completed=_structural_completed(structural_report), total=_structural_total(structural_report))
            print("[geoword-structural-shortfall] " + json.dumps({
                "report_path": str(report_path), "report": structural_report,
            }, ensure_ascii=False), flush=True)
            gaps = ", ".join(
                f"{kind}={actual}/{target}"
                for kind, (actual, target) in structural_report["remaining_gaps"].items()
            )
            raise DiscoveryIncompleteError(
                "discovery structural targets remain unmet after automatic repair: " + gaps,
                report=structural_report,
            )

        district_batches = _chunks(districts, 5)
        place_docs = self._run_batch_steps(
            prefix="05_place_plans",
            batches=district_batches,
            prompt_fn=lambda batch: prompts.place_plan_prompt(seed, batch, corridors),
            state=state,
            use_web_search=False,
            max_output_tokens=6000,
        )

        dynamic_storyline_doc = self._step_json(
            "06_dynamic_storyline_seed",
            lambda: self._call_json(
                prompts.dynamic_storyline_prompt(seed, districts, corridors),
                use_web_search=False,
                max_output_tokens=8000, logical_step="dynamic_storyline",
            ),
            state,
        )

        discovery = assemble_discovery(
            city_outline=city_outline,
            district_docs=district_docs,
            corridor_group_doc=corridor_group_doc,
            corridor_docs=corridor_docs,
            place_docs=place_docs,
            dynamic_storyline_doc=dynamic_storyline_doc,
            policy={
                "mode": "recoverable_discovery_session",
                "model": self.model,
                "endpoint_name": self.endpoint_name,
                "session_dir": str(self.session_dir),
                "max_workers": self.max_workers,
                "web_search_max_workers": self.web_search_max_workers,
                "timeout_seconds": self.timeout_seconds,
                "web_search_timeout_seconds": self.web_search_timeout_seconds,
                "max_attempts": self.max_attempts,
                "caps": {
                    "district_count": seed.district_count,
                    "corridor_count": seed.corridor_count,
                    "max_districts": max_districts,
                    "max_corridors": max_corridors,
                    "policy": "exact_seed_count",
                },
                "steps": state.get("steps", {}),
                "structural_repair": structural_report,
            },
        )
        discovery["operation_ledger"] = self.client.telemetry_snapshot()
        write_json(self.session_dir / "discovery.json", discovery)
        state["status"] = "complete"
        state["operation_ledger"] = self.client.telemetry_snapshot()
        state["updated_at"] = _now()
        self._write_state(state)
        print(
            "[session] assembled "
            f"districts={len(discovery['district_candidates'])}, "
            f"corridors={len(discovery['corridor_candidates'])}, "
            f"place_plans={len(discovery['place_search_plan'])}",
            flush=True,
        )
        return discovery

    def _structural_repair_dir(self) -> Path:
        batch = max(1, int(self.structural_repair_batch))
        return self.session_dir / "structural_repairs" / f"batch_{batch:02d}"

    def _repair_structural_shortfalls(
        self,
        seed: CitySeed,
        state: dict[str, Any],
        city_outline: dict[str, Any],
        district_docs: list[dict[str, Any]],
        corridor_docs: list[dict[str, Any]],
        districts: list[dict[str, Any]],
        corridors: list[dict[str, Any]],
        district_audit: dict[str, Any],
        corridor_audit: dict[str, Any],
        district_target: int,
        corridor_target: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        """Repair structural shortages before any paid geo or character stage starts."""
        repair_dir = self._structural_repair_dir()
        repair_dir.mkdir(parents=True, exist_ok=True)
        for previous_doc in self._prior_structural_repair_documents("districts"):
            district_docs.append(previous_doc)
        for previous_doc in self._prior_structural_repair_documents("corridors"):
            corridor_docs.append(previous_doc)
        districts, district_audit = _collect_structural_candidates(
            district_docs, "district_candidates", district_validation_reason, district_target,
        )
        corridors, corridor_audit = _collect_structural_candidates(
            corridor_docs, "corridor_candidates", corridor_validation_reason, corridor_target,
        )
        rounds: list[dict[str, Any]] = []
        for round_number in range(1, MAX_AUTOMATIC_STRUCTURAL_REPAIR_ROUNDS + 1):
            gaps = _structural_gaps(districts, corridors, district_target, corridor_target)
            if not gaps:
                break
            round_record: dict[str, Any] = {"round": round_number, "requested": {}, "results": {}}
            for kind, (actual, target) in gaps.items():
                # Ask for a small surplus so local validation can still retain the exact missing count.
                missing = target - actual
                requested = min(8, max(missing + 1, missing * 2))
                round_record["requested"][kind] = requested
                result_path = repair_dir / f"round_{round_number:02d}_{kind}.json"
                try:
                    if result_path.exists() and not self.force:
                        document = read_json(result_path)
                    else:
                        _emit_progress(
                            "structural_top_up", "running",
                            label=f"\u81ea\u52a8\u8865\u9f50 {kind} {round_number}/{MAX_AUTOMATIC_STRUCTURAL_REPAIR_ROUNDS}",
                            completed=len(districts) + len(corridors), total=district_target + corridor_target,
                        )
                        document = self._call_json(
                            prompts.structural_top_up_prompt(
                                seed, city_outline, districts, corridors,
                                kind=kind, requested_count=requested,
                            ),
                            use_web_search=True,
                            max_output_tokens=6000,
                            logical_step="structural_top_up",
                            request_name=f"{kind}_round_{round_number}",
                        )
                        self._write_step_result(result_path, document)
                    if kind == "districts":
                        district_docs.append(document)
                        districts, district_audit = _collect_structural_candidates(
                            district_docs, "district_candidates", district_validation_reason, district_target,
                        )
                        audit = district_audit
                        accepted = len(districts)
                    else:
                        corridor_docs.append(document)
                        corridors, corridor_audit = _collect_structural_candidates(
                            corridor_docs, "corridor_candidates", corridor_validation_reason, corridor_target,
                        )
                        audit = corridor_audit
                        accepted = len(corridors)
                    round_record["results"][kind] = {"accepted": accepted, "audit": audit}
                except Exception as exc:
                    round_record["results"][kind] = {"error": str(exc)}
            rounds.append(round_record)
            write_json(repair_dir / f"round_{round_number:02d}_summary.json", round_record)
        report = {
            "schema_version": "geoword.discovery.structural_repair.v1",
            "batch": max(1, int(self.structural_repair_batch)),
            "max_automatic_rounds": MAX_AUTOMATIC_STRUCTURAL_REPAIR_ROUNDS,
            "districts": {"target": district_target, "accepted": len(districts), "audit": district_audit},
            "corridors": {"target": corridor_target, "accepted": len(corridors), "audit": corridor_audit},
            "rounds": rounds,
            "remaining_gaps": _structural_gaps(districts, corridors, district_target, corridor_target),
            "session_dir": str(self.session_dir),
            "repair_dir": str(repair_dir),
        }
        write_json(repair_dir / "report.json", report)
        return districts, corridors, report

    def _prior_structural_repair_documents(self, kind: str) -> list[dict[str, Any]]:
        """Load accepted-candidate sources from earlier continuation batches."""
        current_batch = max(1, int(self.structural_repair_batch))
        documents: list[dict[str, Any]] = []
        root = self.session_dir / "structural_repairs"
        if not root.is_dir():
            return documents
        for path in sorted(root.glob(f"batch_*/round_*_{kind}.json")):
            try:
                batch = int(path.parent.name.rsplit("_", 1)[-1])
            except ValueError:
                continue
            if batch >= current_batch:
                continue
            try:
                documents.append(read_json(path))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return documents

    def _step_json(self, name: str, fn: Any, state: dict[str, Any]) -> dict[str, Any]:
        path = self.session_dir / f"{name}.json"
        if path.exists() and not self.force:
            print(f"[session] reuse {name}", flush=True)
            _emit_progress(name, "completed", label=f"沿用发现步骤 {name}", completed=1, total=1)
            return read_json(path)
        print(f"[session] run {name}", flush=True)
        _emit_progress(name, "running", label=f"执行发现步骤 {name}", completed=0, total=1)
        try:
            data = fn()
            write_json(path, data)
            self._mark_step(state, name, "complete", path)
            _emit_progress(name, "completed", label=f"完成发现步骤 {name}", completed=1, total=1)
            return data
        except Exception as exc:
            self._mark_step(state, name, "failed", path, error=str(exc))
            _emit_progress(name, "failed", label=f"发现步骤失败 {name}")
            raise

    def _run_group_steps(
        self,
        *,
        prefix: str,
        groups: list[dict[str, Any]],
        prompt_fn: Any,
        state: dict[str, Any],
        use_web_search: bool,
        max_output_tokens: int,
    ) -> list[dict[str, Any]]:
        return self._run_parallel_steps(
            prefix=prefix,
            items=groups,
            name_fn=lambda group, idx: f"{prefix}_{_slug(group.get('group_name') or idx)}",
            prompt_fn=prompt_fn,
            state=state,
            use_web_search=use_web_search,
            max_output_tokens=max_output_tokens,
        )

    def _run_batch_steps(
        self,
        *,
        prefix: str,
        batches: list[list[dict[str, Any]]],
        prompt_fn: Any,
        state: dict[str, Any],
        use_web_search: bool,
        max_output_tokens: int,
    ) -> list[dict[str, Any]]:
        return self._run_parallel_steps(
            prefix=prefix,
            items=batches,
            name_fn=lambda _batch, idx: f"{prefix}_{idx + 1:03d}",
            prompt_fn=prompt_fn,
            state=state,
            use_web_search=use_web_search,
            max_output_tokens=max_output_tokens,
        )

    def _run_parallel_steps(
        self,
        *,
        prefix: str,
        items: list[Any],
        name_fn: Any,
        prompt_fn: Any,
        state: dict[str, Any],
        use_web_search: bool,
        max_output_tokens: int,
    ) -> list[dict[str, Any]]:
        if not items:
            return []
        results: list[dict[str, Any]] = []
        _emit_progress(prefix, "running", label=f"执行 {prefix}", completed=0, total=len(items))
        pending = []
        for idx, item in enumerate(items):
            name = name_fn(item, idx)
            path = self._parallel_step_path(prefix, idx)
            if path.exists() and not self.force:
                print(f"[session] reuse {name}", flush=True)
                results.append(read_json(path))
            else:
                pending.append((name, path, item))
        if pending:
            (self.session_dir / prefix).mkdir(parents=True, exist_ok=True)
            worker_limit = self.web_search_max_workers if use_web_search else self.max_workers
            workers = max(1, min(worker_limit, len(pending)))
            print(f"[session] run {prefix} pending={len(pending)} workers={workers}", flush=True)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        self._call_json,
                        prompt_fn(item),
                        use_web_search=use_web_search,
                        max_output_tokens=max_output_tokens,
                        logical_step=prefix,
                        request_name=name,
                    ): (name, path)
                    for name, path, item in pending
                }
                for future in as_completed(futures):
                    name, path = futures[future]
                    try:
                        data = future.result()
                        self._write_step_result(path, data)
                        self._mark_step(state, name, "complete", path)
                        results.append(data)
                        _emit_progress(prefix, "running", label=f"完成 {name}",
                                       completed=len(results), total=len(items))
                    except Exception as exc:
                        self._mark_step(state, name, "failed", path, error=str(exc))
                        _emit_progress(prefix, "failed", label=f"发现步骤失败 {name}")
        _emit_progress(prefix, "completed", label=f"完成 {prefix}", completed=len(results), total=len(items))
        failed = [
            name for name, _path, _item in pending
            if state.get("steps", {}).get(name, {}).get("status") == "failed"
        ]
        if failed:
            message = f"{prefix} incomplete: {len(results)}/{len(items)} completed; failed={', '.join(failed)}"
            state["status"] = "failed"
            state["updated_at"] = _now()
            self._write_state(state)
            _emit_progress(prefix, "failed", label=f"discovery incomplete {prefix}",
                           completed=len(results), total=len(items))
            raise DiscoveryIncompleteError(message)
        return results

    def _call_json(self, prompt: str, *, use_web_search: bool, max_output_tokens: int,
                   logical_step: str, request_name: str = "") -> dict[str, Any]:
        """Call one logical discovery step with bounded retries and backoff."""
        policy = _request_policy(
            logical_step,
            timeout_seconds=self.timeout_seconds,
            web_search_timeout_seconds=self.web_search_timeout_seconds,
            max_attempts=self.max_attempts,
            use_web_search=use_web_search,
        )
        last_error: Exception | None = None
        for attempt, spec in enumerate(policy, start=1):
            request_label = request_name or logical_step
            _emit_progress(
                logical_step, "running",
                label=(
                    f"{request_label}：请求重试 {attempt}/{len(policy)}"
                    if attempt > 1 else f"{request_label}：正在请求 1/{len(policy)}"
                ),
                request_name=request_label,
                attempt=attempt,
                total_attempts=len(policy),
                timeout_seconds=spec["timeout_seconds"],
            )
            extra: dict[str, Any] = {"max_output_tokens": max_output_tokens}
            if use_web_search:
                extra["tools"] = [{"type": "web_search_preview"}]
            try:
                if self._chat_provider:
                    # Chat-completions endpoints do not share the Responses
                    # web-search contract. Google remains the factual source
                    # for the city map, so a valid JSON structure response is
                    # sufficient for the fallback route.
                    response = self.client.call_chat(
                        [{"role": "user", "content": prompt}],
                        extra={"max_tokens": extra["max_output_tokens"]},
                    )
                else:
                    response = self.client.call(prompt, extra=extra, timeout_seconds=spec["timeout_seconds"])
            except RuntimeError as exc:
                if use_web_search and "Unsupported tool type" in str(exc):
                    response = self.client.call(
                        prompt, extra={"max_output_tokens": extra["max_output_tokens"]},
                        timeout_seconds=spec["timeout_seconds"],
                    )
                else:
                    last_error = exc
                    response = None
            except Exception as exc:
                last_error = exc
                response = None
            if response is not None:
                try:
                    text = _response_text(response)
                    data = extract_json_object(text)
                    data.setdefault("source_response_excerpt", text[:600])
                    return data
                except Exception as exc:
                    last_error = exc
            if attempt < len(policy):
                print(f"[session] retry {logical_step} attempt={attempt + 1}/{len(policy)} after {type(last_error).__name__}: {last_error}", flush=True)
                time.sleep(spec["backoff_seconds"])
        raise RuntimeError(
            f"{logical_step} failed after {len(policy)} attempts: {type(last_error).__name__}: {last_error}"
        ) from last_error

    def _write_step_result(self, path: Path, data: dict[str, Any]) -> None:
        """Persist a completed worker result despite transient directory races."""
        for attempt in range(3):
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                write_json(path, data)
                return
            except FileNotFoundError:
                if attempt == 2:
                    raise
                time.sleep(0.2 * (attempt + 1))

    def _parallel_step_path(self, prefix: str, index: int) -> Path:
        """Use short, deterministic artifact names without losing logical names."""
        directory = {
            "02_districts": "d",
            "04_corridors": "c",
            "05_place_plans": "p",
        }.get(prefix, prefix)
        return self.session_dir / directory / f"{index + 1:03d}.json"

    def _load_state(self, seed: CitySeed) -> dict[str, Any]:
        path = self.session_dir / "session_state.json"
        if path.exists() and not self.force:
            return read_json(path)
        state = {
            "session_id": f"{seed.city_id}_discovery_session",
            "seed": seed.__dict__,
            "status": "running",
            "created_at": _now(),
            "updated_at": _now(),
            "steps": {},
        }
        self._write_state(state)
        return state

    def _mark_step(self, state: dict[str, Any], name: str, status: str, path: Path, error: str | None = None) -> None:
        with self._state_lock:
            state.setdefault("steps", {})[name] = {
                "status": status,
                "path": str(path),
                "updated_at": _now(),
            }
            if error:
                state["steps"][name]["error"] = error
            state["updated_at"] = _now()
            self._write_state(state)

    def _write_state(self, state: dict[str, Any]) -> None:
        write_json(self.session_dir / "session_state.json", state)

    def _fallback_district_groups(self, seed: CitySeed) -> list[dict[str, Any]]:
        return [
            {"group_name": "central_station_business", "focus": "station business cores", "target_count": 5},
            {"group_name": "nightlife_commercial", "focus": "nightlife and retail service areas", "target_count": 5},
            {"group_name": "tourism_history", "focus": "tourism and historic areas", "target_count": 4},
            {"group_name": "waterfront_event", "focus": "waterfront and event service areas", "target_count": 4},
            {"group_name": "medical_education_local", "focus": "healthcare, education, local daily services", "target_count": 4},
        ][: max(3, min(6, seed.district_count // 4 + 2))]

    def _fallback_corridor_groups(self, seed: CitySeed) -> list[dict[str, Any]]:
        return [
            {"group_name": "rail_intermodal", "corridor_types": ["rail_corridor", "intermodal_corridor"], "target_count": 6},
            {"group_name": "road_bus", "corridor_types": ["road_corridor", "bus_corridor"], "target_count": 5},
            {"group_name": "commercial_pedestrian", "corridor_types": ["commercial_axis", "pedestrian_corridor"], "target_count": 6},
            {"group_name": "waterfront_airport_tourism", "corridor_types": ["waterfront_axis", "airport_access", "commercial_axis"], "target_count": 5},
        ]


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _hard_cap(target_count: int) -> int:
    """Treat World Studio targets as exact structural counts, not minimums."""
    return max(1, int(target_count))


def _response_text(response: dict[str, Any]) -> str:
    """Read both Responses API and OpenAI-compatible chat payloads."""
    choices = response.get("choices") or []
    if choices:
        content = ((choices[0].get("message") or {}).get("content"))
        if isinstance(content, str):
            return content
    return extract_response_text(response)


def _request_policy(
    logical_step: str,
    *,
    timeout_seconds: int,
    web_search_timeout_seconds: int,
    max_attempts: int,
    use_web_search: bool,
) -> list[dict[str, Any]]:
    """Return stable per-request budgets without changing discovery quality on retry."""
    del logical_step
    attempts = max(1, min(int(max_attempts), 3))
    timeout = web_search_timeout_seconds if use_web_search else timeout_seconds
    return [
        {
            "timeout_seconds": max(20, int(timeout)),
            "backoff_seconds": 5 if attempt < attempts - 1 else 0,
        }
        for attempt in range(attempts)
    ]


def _cap_discovery_items(items: list[dict[str, Any]], max_count: int) -> list[dict[str, Any]]:
    return dedupe_named(items)[:max_count]


def _collect_structural_candidates(
    documents: list[dict[str, Any]],
    field: str,
    reason_fn: Any,
    target: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate and de-duplicate candidates while retaining an audit explanation."""
    raw = [item for document in documents for item in (document.get(field, []) or [])]
    valid: list[dict[str, Any]] = []
    invalid_by_reason: dict[str, int] = {}
    for item in raw:
        if not isinstance(item, dict):
            invalid_by_reason["not_object"] = invalid_by_reason.get("not_object", 0) + 1
            continue
        reason = str(reason_fn(item) or "")
        if reason:
            invalid_by_reason[reason] = invalid_by_reason.get(reason, 0) + 1
            continue
        valid.append(item)
    deduplicated = dedupe_named(valid)
    accepted = deduplicated[:target]
    return accepted, {
        "raw_returned": len(raw),
        "valid_before_deduplication": len(valid),
        "invalid_by_reason": invalid_by_reason,
        "duplicates_removed": len(valid) - len(deduplicated),
        "over_target_removed": max(0, len(deduplicated) - len(accepted)),
        "accepted": len(accepted),
    }


def _structural_gaps(
    districts: list[dict[str, Any]],
    corridors: list[dict[str, Any]],
    district_target: int,
    corridor_target: int,
) -> dict[str, tuple[int, int]]:
    gaps: dict[str, tuple[int, int]] = {}
    if len(districts) < district_target:
        gaps["districts"] = (len(districts), district_target)
    if len(corridors) < corridor_target:
        gaps["corridors"] = (len(corridors), corridor_target)
    return gaps


def _structural_completed(report: dict[str, Any]) -> int:
    return int((report.get("districts") or {}).get("accepted", 0)) + int((report.get("corridors") or {}).get("accepted", 0))


def _structural_total(report: dict[str, Any]) -> int:
    return int((report.get("districts") or {}).get("target", 0)) + int((report.get("corridors") or {}).get("target", 0))


def _slug(value: Any) -> str:
    text = str(value).lower()
    out = "".join(ch if ch.isalnum() else "_" for ch in text).strip("_")
    return out[:80] or "item"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DiscoveryIncompleteError(RuntimeError):
    """A recoverable discovery session has incomplete required groups."""

    def __init__(self, message: str, *, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report or {}


def _emit_progress(step: str, state: str, *, label: str, completed: int | None = None,
                   total: int | None = None, request_name: str = "", attempt: int | None = None,
                   total_attempts: int | None = None, timeout_seconds: int | None = None) -> None:
    """Emit discovery detail for the World Studio build monitor."""
    event = {"stage": "map_discovery", "step": step, "state": state, "label": label}
    if completed is not None:
        event["completed"] = completed
    if total is not None:
        event["total"] = total
    if request_name:
        event["request_name"] = request_name
    if attempt is not None:
        event["attempt"] = attempt
    if total_attempts is not None:
        event["total_attempts"] = total_attempts
    if timeout_seconds is not None:
        event["timeout_seconds"] = timeout_seconds
    with _PROGRESS_LOCK:
        print("[geoword-progress] " + json.dumps(event, ensure_ascii=False), flush=True)
