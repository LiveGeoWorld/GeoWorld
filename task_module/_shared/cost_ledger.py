"""Append-only, interruption-safe task-construction cost ledger.

The ledger records measured external usage. Monetary values remain unknown
unless a frozen, explicit pricing snapshot is supplied by the release.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from shared.io import read_json, write_json
from shared.external_usage import bind_external_usage_recorder


SCHEMA_VERSION = "geoword.task_construction_cost.v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskConstructionCostLedger:
    """Writes redacted events and derived summaries beneath one batch."""

    def __init__(
        self,
        batch_dir: str | Path,
        *,
        world_id: str,
        city_id: str,
        city_version: str = "",
        task_type: str,
        pricing_snapshot: dict[str, Any] | None = None,
    ) -> None:
        self.batch_dir = Path(batch_dir)
        self.world_id = world_id
        self.city_id = city_id
        self.city_version = city_version
        self.task_type = task_type
        self.cost_dir = self.batch_dir / "cost"
        self.cost_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.cost_dir / "usage_events.jsonl"
        self.snapshot_path = self.cost_dir / "pricing_snapshot.json"
        self.summary_path = self.cost_dir / "generation_cost_summary.json"
        self._active_unit = "batch_shared"
        self._active_phase = "batch_initialization"
        self._events = self._read_events()
        self._ensure_pricing_snapshot(pricing_snapshot)
        self._write_summary()

    def _ensure_pricing_snapshot(self, configured: dict[str, Any] | None) -> None:
        if self.snapshot_path.exists():
            return
        pricing_reference = (configured or {}).get("pricing_reference")
        endpoint_name = str((configured or {}).get("endpoint_name") or "")
        model = str((configured or {}).get("model") or "")
        provider = str((configured or {}).get("provider") or "unknown")
        if isinstance(pricing_reference, dict) and pricing_reference:
            currency = str(pricing_reference.get("currency") or "")
            rates = pricing_reference.get("rates_per_million_tokens")
            if currency and isinstance(rates, dict) and rates:
                write_json(self.snapshot_path, {
                    "schema_version": "geoword.task_construction_pricing.v2",
                    "captured_at": _now(),
                    "status": "pricing_configured",
                    "llm_endpoints": [{
                        "endpoint_name": endpoint_name,
                        "provider": provider,
                        "model": model,
                        "pricing_reference": pricing_reference,
                    }],
                    "note": (
                        "This is a frozen, redacted pricing reference for the selected "
                        "construction endpoint. It is an estimate, not a provider invoice."
                    ),
                })
                return
        write_json(self.snapshot_path, {
            "schema_version": "geoword.task_construction_pricing.v2",
            "captured_at": _now(),
            "status": "pricing_not_configured",
            "currency": "USD",
            "rates": {},
            "note": (
                "Measured usage is retained without guessing money. Add a dated "
                "provider/SKU price reference before treating any estimate as cost."
            ),
        })

    @contextmanager
    def unit(self, unit_id: str, phase: str) -> Iterator[None]:
        prior_unit, prior_phase = self._active_unit, self._active_phase
        self._active_unit, self._active_phase = unit_id, phase
        try:
            with bind_external_usage_recorder(self):
                yield
        finally:
            self._active_unit, self._active_phase = prior_unit, prior_phase

    @contextmanager
    def batch_scope(self, phase: str = "batch_shared") -> Iterator[None]:
        with self.unit("batch_shared", phase):
            yield

    def record_external_usage(self, **event: Any) -> None:
        """Append one redacted event and refresh its recoverable summary."""
        usage = event.pop("usage", None)
        clean_usage = {
            key: int(value or 0)
            for key, value in (usage or {}).items()
            if key in {"prompt", "completion", "total", "cached_input", "request_units", "image_units"}
        }
        record = {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"usage_{uuid.uuid4().hex}",
            "recorded_at": _now(),
            "world_id": self.world_id,
            "city_id": self.city_id,
            "city_version": self.city_version,
            "task_type": self.task_type,
            "batch_id": self.batch_dir.name,
            "generation_unit_id": self._active_unit,
            "phase": event.pop("phase", self._active_phase),
            "provider": str(event.pop("provider", "unknown")),
            "operation": str(event.pop("operation", "unknown")),
            "status": str(event.pop("status", "unknown")),
            "cache_status": str(event.pop("cache_status", "not_applicable")),
            "elapsed_s": round(float(event.pop("elapsed_s", 0.0) or 0.0), 4),
            "usage": clean_usage,
            "error_class": str(event.pop("error_class", "")),
            "metadata": {
                str(key): value for key, value in event.items()
                if key in {"model", "endpoint_name", "profile", "http_status", "attempt"}
            },
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
        self._events.append(record)
        # The append-only event log is durable per call. Avoid repeatedly
        # rereading it during dense route construction; formalization forces a
        # final checkpoint and a restarted process rebuilds from the log.
        if len(self._events) % 10 == 0:
            self._write_summary()

    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def _read_events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows

    def refresh_summary(self) -> dict[str, Any]:
        self._events = self._read_events()
        return self._write_summary()

    def _write_summary(self) -> dict[str, Any]:
        events = self._events
        units: dict[str, dict[str, Any]] = {}
        aggregate = _aggregate(events)
        for event in events:
            unit = str(event.get("generation_unit_id") or "batch_shared")
            bucket = units.setdefault(unit, {"events": []})
            bucket["events"].append(event)
        unit_summaries = {
            unit: _aggregate(value["events"])
            for unit, value in units.items()
        }
        summary = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _now(),
            "scope": {
                "world_id": self.world_id,
                "city_id": self.city_id,
                "city_version": self.city_version,
                "task_type": self.task_type,
                "batch_id": self.batch_dir.name,
            },
            "cost_status": _pricing_status(self.snapshot_path),
            "actual_external_usage": aggregate,
            "estimated_monetary_cost": _estimate_monetary_cost(events, self.snapshot_path),
            "by_generation_unit": unit_summaries,
            "note": (
                "This is measured usage, not an invoice. batch_shared is intentionally "
                "not assigned to a formal task. Historical artifacts remain unchanged."
            ),
        }
        write_json(self.summary_path, summary)
        return summary

    def write_task_cost(self, task_dir: str | Path, task_id: str, unit_id: str) -> dict[str, Any]:
        task_events = [event for event in self.events() if event.get("generation_unit_id") == unit_id]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _now(),
            "task_id": task_id,
            "generation_unit_id": unit_id,
            "direct_usage": _aggregate(task_events),
            "event_ids": [event.get("event_id") for event in task_events],
            "cost_status": _pricing_status(self.snapshot_path),
            "estimated_monetary_cost": _estimate_monetary_cost(task_events, self.snapshot_path),
            "note": "Direct task usage excludes batch_shared reusable-asset work.",
        }
        write_json(Path(task_dir) / "generation_cost.json", payload)
        self.refresh_summary()
        return payload


def _aggregate(events: list[dict[str, Any]]) -> dict[str, Any]:
    token_usage = {"prompt": 0, "completion": 0, "total": 0, "cached_input": 0}
    request_count = 0
    cache_hits = 0
    failures = 0
    elapsed_s = 0.0
    operations: dict[str, int] = {}
    for event in events:
        usage = event.get("usage") or {}
        request_count += int(usage.get("request_units", 1) or 0)
        for key in token_usage:
            token_usage[key] += int(usage.get(key, 0) or 0)
        if event.get("cache_status") == "hit":
            cache_hits += 1
        if event.get("status") not in {"success", "unavailable", "cache_hit"}:
            failures += 1
        elapsed_s += float(event.get("elapsed_s", 0.0) or 0.0)
        operation = f"{event.get('provider')}:{event.get('operation')}"
        operations[operation] = operations.get(operation, 0) + 1
    return {
        "event_count": len(events),
        "external_request_count": request_count,
        "cache_hit_count": cache_hits,
        "failed_event_count": failures,
        "elapsed_s": round(elapsed_s, 4),
        "token_usage": token_usage,
        "operations": operations,
    }


def _pricing_status(snapshot_path: Path) -> str:
    try:
        return str(read_json(snapshot_path).get("status") or "pricing_not_configured")
    except Exception:
        return "pricing_not_configured"


def _estimate_monetary_cost(events: list[dict[str, Any]], snapshot_path: Path) -> dict[str, Any]:
    """Estimate LLM token cost without inventing Google or currency conversion rates."""
    try:
        snapshot = read_json(snapshot_path)
    except Exception:
        snapshot = {}
    endpoints = snapshot.get("llm_endpoints") or []
    priced_by_model: dict[str, dict[str, Any]] = {}
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            continue
        reference = endpoint.get("pricing_reference") or {}
        rates = reference.get("rates_per_million_tokens") if isinstance(reference, dict) else {}
        model = str(endpoint.get("model") or "")
        currency = str(reference.get("currency") or "") if isinstance(reference, dict) else ""
        if model and currency and isinstance(rates, dict):
            priced_by_model[model] = {"currency": currency, "rates": rates, "reference": reference}

    by_currency: dict[str, dict[str, Any]] = {}
    unpriced: dict[str, int] = {}
    for event in events:
        provider = str(event.get("provider") or "unknown")
        usage = event.get("usage") or {}
        units = int(usage.get("request_units", 0) or 0)
        if provider != "llm":
            if units:
                unpriced[provider] = unpriced.get(provider, 0) + units
            continue
        model = str((event.get("metadata") or {}).get("model") or "")
        pricing = priced_by_model.get(model)
        prompt = int(usage.get("prompt", 0) or 0)
        completion = int(usage.get("completion", 0) or 0)
        if not pricing or (prompt == 0 and completion == 0):
            if units:
                unpriced["llm" if not model else f"llm:{model}"] = unpriced.get("llm" if not model else f"llm:{model}", 0) + units
            continue
        rates = pricing["rates"]
        try:
            estimate = (
                prompt * float(rates["input"]) + completion * float(rates["output"])
            ) / 1_000_000
        except (KeyError, TypeError, ValueError):
            unpriced[f"llm:{model}"] = unpriced.get(f"llm:{model}", 0) + units
            continue
        bucket = by_currency.setdefault(pricing["currency"], {
            "amount": 0.0,
            "priced_event_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        })
        bucket["amount"] += estimate
        bucket["priced_event_count"] += 1
        bucket["prompt_tokens"] += prompt
        bucket["completion_tokens"] += completion
    for bucket in by_currency.values():
        bucket["amount"] = round(bucket["amount"], 8)
    if by_currency and unpriced:
        status = "partial_estimate"
    elif by_currency:
        status = "estimated"
    else:
        status = "pricing_not_configured"
    return {
        "status": status,
        "by_currency": by_currency,
        "unpriced_external_requests": unpriced,
        "note": (
            "Each currency is reported separately. Google Maps and any endpoint without "
            "a frozen SKU/token reference remain measured usage, not monetary estimates."
        ),
    }
