from shared.costing import apply_llm_pricing_reference


GPT54_MINI_REFERENCE = {
    "actual_provider": "Yunwu",
    "reference_provider": "OpenAI",
    "reference_model": "gpt-5.4-mini",
    "pricing_mode": "standard",
    "currency": "USD",
    "source_url": "https://developers.openai.com/api/docs/pricing/",
    "retrieved_on": "2026-07-23",
    "rates_per_million_tokens": {
        "input": 0.75,
        "cached_input": 0.075,
        "output": 4.50,
    },
}


def test_yunwu_gpt54_mini_uses_openai_reference_without_claiming_actual_bill() -> None:
    result = apply_llm_pricing_reference(
        {"token_usage": {"prompt": 1_000_000, "completion": 1_000_000}},
        GPT54_MINI_REFERENCE,
    )

    assert result["estimated_reference_cost_usd"] == 5.25
    assert result["estimated_public_list_cost_currency"] == "USD"
    assert result["money_estimate"] == 5.25
    assert result["actual_billed_cost"] is None
    assert result["actual_provider"] == "Yunwu"
    assert result["reference_provider"] == "OpenAI"
    assert result["cached_input_assumption"] == "not reported by provider; estimated as zero"


def test_missing_reference_keeps_cost_unknown() -> None:
    telemetry = {"cost_status": "pricing_not_configured", "money_estimate": None, "token_usage": {}}
    assert apply_llm_pricing_reference(telemetry, None) == telemetry


def test_cached_input_replaces_the_normal_input_rate() -> None:
    result = apply_llm_pricing_reference(
        {"token_usage": {"prompt": 1_000_000, "cached_input": 500_000, "completion": 0}},
        GPT54_MINI_REFERENCE,
    )

    assert result["estimated_reference_cost_usd"] == 0.4125


def test_tiered_price_uses_each_request_length() -> None:
    qwen_reference = {
        "currency": "CNY",
        "rates_per_million_tokens": {"input": 0.8, "output": 2.0},
        "pricing_tiers": [
            {"max_input_tokens": 128_000, "input": 0.8, "output": 2.0},
            {"max_input_tokens": 256_000, "input": 2.4, "output": 20.0},
        ],
    }
    result = apply_llm_pricing_reference(
        {
            "token_usage": {"prompt": 200_000, "completion": 2_000},
            "request_usage": [
                {"prompt": 100_000, "completion": 1_000},
                {"prompt": 100_000, "completion": 1_000},
            ],
        },
        qwen_reference,
    )

    assert result["estimated_public_list_cost"] == 0.164
    assert result["pricing_tier_request_counts"] == {"128000": 2}
