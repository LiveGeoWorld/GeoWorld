from __future__ import annotations

from shared.external_usage import record_external_usage
from shared.io import read_json
from task_module._shared.base_generator import BaseTaskGenerator
from task_module._shared.cost_ledger import TaskConstructionCostLedger


def test_cost_ledger_keeps_failed_attempts_and_writes_task_direct_cost(tmp_path):
    ledger = TaskConstructionCostLedger(
        tmp_path / "batch_a",
        world_id="world_002", city_id="shanghai_smoke", city_version="v2",
        task_type="itinerary",
    )
    with ledger.unit("slot_0001", "task_construction"):
        record_external_usage(
            provider="llm", operation="chat_completions", status="failed",
            elapsed_s=0.5, usage={"request_units": 1}, model="qwen-plus",
        )
        record_external_usage(
            provider="google_maps", operation="routes", status="success",
            elapsed_s=1.0, usage={"request_units": 1}, profile="google_maps_3",
        )
    with ledger.batch_scope("route_coverage"):
        record_external_usage(
            provider="route_cache", operation="route_lookup", status="cache_hit",
            cache_status="hit", usage={"request_units": 0},
        )

    task_dir = tmp_path / "batch_a" / "tasks" / "task_0001"
    task_dir.mkdir(parents=True)
    task_cost = ledger.write_task_cost(task_dir, "task_1", "slot_0001")
    summary = read_json(tmp_path / "batch_a" / "cost" / "generation_cost_summary.json")

    assert task_cost["direct_usage"]["external_request_count"] == 2
    assert task_cost["direct_usage"]["failed_event_count"] == 1
    assert summary["actual_external_usage"]["event_count"] == 3
    assert summary["by_generation_unit"]["batch_shared"]["cache_hit_count"] == 1
    assert (task_dir / "generation_cost.json").is_file()


def test_cost_ledger_freezes_configured_token_rates_and_keeps_other_providers_unpriced(tmp_path):
    ledger = TaskConstructionCostLedger(
        tmp_path / "batch_priced",
        world_id="world_002", city_id="shanghai_smoke", city_version="v2",
        task_type="safety_planning",
        pricing_snapshot={
            "endpoint_name": "qwen_default",
            "provider": "openai_compatible",
            "model": "qwen-plus",
            "pricing_reference": {
                "currency": "CNY",
                "rates_per_million_tokens": {"input": 0.8, "output": 2.0},
            },
        },
    )
    with ledger.unit("slot_0001", "task_construction"):
        record_external_usage(
            provider="llm", operation="chat_completions", status="success",
            usage={"request_units": 1, "prompt": 1_000, "completion": 500},
            model="qwen-plus",
        )
        record_external_usage(
            provider="google_maps", operation="routes", status="success",
            usage={"request_units": 1}, profile="google_maps_3",
        )
    summary = ledger.refresh_summary()

    assert summary["cost_status"] == "pricing_configured"
    estimate = summary["estimated_monetary_cost"]
    assert estimate["status"] == "partial_estimate"
    assert estimate["by_currency"]["CNY"]["amount"] == 0.0018
    assert estimate["unpriced_external_requests"]["google_maps"] == 1


def test_formal_generator_freezes_the_selected_config_endpoint_pricing(tmp_path):
    import yaml
    config_path=tmp_path/'test_config.yaml'
    config_path.write_text(yaml.safe_dump({'stage_models':{'scenario_patch':'qwen_default'},'model_endpoints':{'qwen_default':{'provider':'openai_compatible','model':'unit-test-model','pricing_reference':{'currency':'CNY','rates_per_million_tokens':{'input':0.8,'output':2.0}}}}}),encoding='utf-8')
    generator = BaseTaskGenerator(
        {"cities": [{"city_id": "shanghai_smoke"}]}, {"characters": []}, [], [], tmp_path, config_yaml=str(config_path),
    )
    generator.task_type = "safety_planning"
    generator.stage_model_key = "scenario_patch"
    batch_dir = tmp_path / "formal_batch"
    generator.initialize_cost_ledger({
        "batch_dir": batch_dir,
        "world_id": "world_002",
        "city_id": "shanghai_smoke",
        "task_type": "safety_planning",
        "generation_context": {"city_version": "v2"},
    })

    snapshot = read_json(batch_dir / "cost" / "pricing_snapshot.json")
    assert snapshot["status"] == "pricing_configured"
    assert snapshot["llm_endpoints"][0]["endpoint_name"] == "qwen_default"
    assert snapshot["llm_endpoints"][0]["pricing_reference"]["currency"] == "CNY"
