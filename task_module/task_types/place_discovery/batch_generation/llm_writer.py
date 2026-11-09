"""Prompt writing for batch-generated place_discovery examples."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import requests

from shared.config_loader import PipelineConfig
from shared.external_usage import record_external_usage
from task_module.task_types.place_discovery.batch_generation.evidence import prompt_evidence_map_from_criteria


TEMPLATE_PHRASES = [
    "请先看一下候选详情",
    "候选的具体情况",
    "推荐一个最合适",
    "请结合距离、开放状态、拥挤程度",
    "不要只按评分排序",
    "I don't want a recommendation based only on rating",
    "check nearby candidates, inspect their details",
]
INTERNAL_FIELD_TERMS = [
    "service_type",
    "place_id",
    "service_id",
    "district_id",
    "price_band",
    "queue_time_min",
    "crowding_level",
    "noise_level",
    "open_status",
    "candidate_services",
    "gold_service",
]


def system_prompt_path() -> Path:
    return Path(__file__).with_name("prompts") / "system.md"


def load_system_prompt() -> str:
    return system_prompt_path().read_text(encoding="utf-8")


def write_discovery_prompt_with_llm(
    *,
    evidence: dict[str, Any],
    quality_profile: dict[str, Any],
    config_yaml: str = "config.yaml",
    stage: str = "task_design",
    temperature: float = 0.7,
    prompt_language: str = "zh-CN",
    review_translation_language: str = "zh-CN",
    scenario_hint: str = "",
    character: dict[str, Any] | None = None,
    character_constraints: dict[str, Any] | None = None,
    max_retries: int = 2,
    difficulty_profile: dict[str, Any] | None = None,
    audit_trail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality_profile = quality_profile or difficulty_profile or {}
    cfg = PipelineConfig(config_yaml)
    endpoint_name = cfg.stage_model(stage)
    endpoint = cfg.model_endpoint(endpoint_name)
    forbidden = _forbidden_terms(evidence)
    issues: list[str] = []
    payload: dict[str, Any] = {}
    for attempt in range(max_retries + 1):
        user_payload = _llm_user_payload(
            evidence=evidence,
            quality_profile=quality_profile,
            prompt_language=prompt_language,
            review_translation_language=review_translation_language,
            forbidden=forbidden,
            scenario_hint=scenario_hint,
            quality_feedback=issues,
            character=character,
            character_constraints=character_constraints,
        )
        messages = [
            {"role": "system", "content": load_system_prompt()},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, indent=2)},
        ]
        raw = _chat_completion(
            endpoint=endpoint,
            messages=messages,
            temperature=temperature,
        )
        payload = parse_llm_json_response(_message_content(raw))
        _normalize_prompt_payload(
            payload,
            prompt_language=prompt_language,
            review_translation_language=review_translation_language,
        )
        issues = validate_prompt_payload(payload, evidence=evidence, expected_language=prompt_language)
        if audit_trail is not None:
            audit_trail.setdefault("attempts", []).append({
                "attempt": attempt + 1,
                "messages": messages,
                "raw_response": raw,
                "normalized_output": dict(payload),
                "validation_issues": list(issues),
            })
        if not issues:
            break
        if attempt >= max_retries:
            raise ValueError("LLM discovery prompt failed validation: " + "; ".join(issues))
    payload["generation_method"] = {
        "type": "llm",
        "stage": stage,
        "endpoint": endpoint_name,
        "model": endpoint.get("model", ""),
        "quality_retries": max_retries,
    }
    return payload


def deterministic_write_discovery_prompt(
    *,
    evidence: dict[str, Any],
    quality_profile: dict[str, Any],
    prompt_language: str = "zh-CN",
    review_translation_language: str = "zh-CN",
    character: dict[str, Any] | None = None,
    character_constraints: dict[str, Any] | None = None,
    difficulty_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality_profile = quality_profile or difficulty_profile or {}
    service_type = evidence.get("service_type", "")
    gold = evidence.get("gold_service", {})
    start = evidence.get("start", {})
    scenario = _scenario(service_type)
    hard_constraints, soft_preferences = _constraints(service_type, gold, quality_profile)

    if character_constraints:
        hard_constraints = character_constraints.get("hard_constraints", hard_constraints)
        soft_preferences = soft_preferences + character_constraints.get("soft_preferences", [])
        soft_preferences = list(dict.fromkeys(soft_preferences))

    criteria = [
        f"以{start.get('area_name') or start.get('district_id') or '当前位置附近'}为搜索范围",
        *hard_constraints,
        *soft_preferences,
    ]

    if character:
        user_prompt_zh = _character_aware_zh_prompt(service_type, start, character, hard_constraints, soft_preferences)
    else:
        user_prompt_zh = _zh_prompt(service_type, start, hard_constraints, soft_preferences)

    user_prompt = _localized_prompt(prompt_language, user_prompt_zh, service_type, start)
    must_mention = [gold.get("name", ""), hard_constraints[0] if hard_constraints else "", soft_preferences[0] if soft_preferences else ""]
    must_mention = [item for item in must_mention if item]
    payload = {
        "prompt_language": prompt_language,
        "user_prompt": user_prompt,
        "review_translation": {
            "language": review_translation_language,
            "user_prompt": user_prompt if review_translation_language == prompt_language else user_prompt_zh,
            "notes": "deterministic fallback translation for human review",
        },
        "hard_constraints": hard_constraints,
        "soft_preferences": soft_preferences,
        "ranking_criteria": criteria,
        "evaluation": {
            "must_mention": must_mention,
            "must_avoid": ["只按评分排序", "忽略用户限制条件"],
        },
        "prompt_evidence_map": prompt_evidence_map_from_criteria(criteria),
        "quality_profile": quality_profile,
        "generation_method": {"type": "deterministic_fallback", "scenario": scenario},
    }
    return payload


def _character_profile_for_llm(
    character: dict[str, Any],
    character_constraints: dict[str, Any] | None,
) -> dict[str, Any]:
    prefs = character.get("preferences", {})
    mobility = character.get("mobility", {})
    communication = character.get("communication", {})
    return {
        "character_id": character.get("character_id", ""),
        "name": character.get("name", ""),
        "age": character.get("age"),
        "gender": character.get("gender", ""),
        "occupation": character.get("occupation", ""),
        "personality_traits": character.get("personality_traits", []),
        "preferences": {
            "prefer": prefs.get("prefer", []),
            "avoid": prefs.get("avoid", []),
            "dietary": prefs.get("dietary", ""),
            "budget": prefs.get("budget", "medium"),
        },
        "mobility": {
            "max_walk_m": mobility.get("max_walk_m", 800),
            "preferred_modes": mobility.get("preferred_modes", []),
            "home_district_id": mobility.get("home_district_id", ""),
        },
        "communication_style": communication.get("style", ""),
        "derived_constraints": character_constraints or {},
        "writing_instruction": (
            "Write the user_prompt FROM THIS CHARACTER'S PERSPECTIVE. "
            "Naturally embed the character's budget, walking tolerance, noise/queue/crowding "
            "preferences, and dietary restrictions into the request. Do NOT list preferences "
            "as fields. The gold service satisfies ALL of the character's hard constraints "
            "and most soft preferences — the prompt must make the character's needs clear "
            "enough that the gold is the uniquely correct answer. Match the character's "
            "communication_style for tone and formality."
        ),
    }


def _character_aware_zh_prompt(
    service_type: str,
    start: dict[str, Any],
    character: dict[str, Any],
    hard: list[str],
    soft: list[str],
) -> str:
    area = start.get("area_name") or "附近"
    prefs = character.get("preferences", {})
    budget = prefs.get("budget", "medium")
    prefer = prefs.get("prefer", [])
    avoid = prefs.get("avoid", [])

    budget_phrases = {"low": "价格实惠一点", "medium": "价格适中就好", "high": "品质比价格更重要"}
    budget_phrase = budget_phrases.get(budget, "价格适中")

    avoid_phrase = f"不太想去{avoid[0]}那种地方" if avoid else ""
    prefer_phrase = f"比较看重{prefer[0]}" if prefer else ""

    if service_type == "restaurant_reservation":
        prompt = f"我今晚在{area}一带，想找个{budget_phrase}的吃饭地方。{prefer_phrase}，{avoid_phrase}。希望不要太折腾，能安稳吃上就好。"
    elif service_type == "cafe_reservation":
        prompt = f"我在{area}附近有段空档，想找家能坐下来待会的咖啡店。{prefer_phrase}，{avoid_phrase}，距离别太远。"
    elif service_type == "hotel_booking":
        prompt = f"我准备去{area}附近，想找个{budget_phrase}、交通方便的住处。{prefer_phrase}，{avoid_phrase}，晚上回来也别太折腾。"
    else:
        prompt = f"我打算在{area}附近安排一下，{prefer_phrase}，{avoid_phrase}。找个{budget_phrase}、距离合理的选择。"

    return prompt


def _llm_user_payload(
    *,
    evidence: dict[str, Any],
    quality_profile: dict[str, Any],
    prompt_language: str,
    review_translation_language: str,
    forbidden: list[str],
    scenario_hint: str,
    quality_feedback: list[str],
    character: dict[str, Any] | None = None,
    character_constraints: dict[str, Any] | None = None,
    difficulty_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality_profile = quality_profile or difficulty_profile or {}
    gold = evidence.get("gold_service", {})
    start = evidence.get("start", {})
    landscape = evidence.get("search_landscape", {})
    return {
        "task": "write_place_discovery_example",
        "prompt_language": prompt_language,
        "review_translation_language": review_translation_language,
        "writing_brief": {
            "user_situation": "Write a realistic user request, not a benchmark instruction.",
            "length_contract": {
                "target_unicode_characters": "180-320",
                "hard_max_unicode_characters": 420,
                "counting_rule": "Count every Unicode character, including spaces and punctuation.",
                "compression_rule": (
                    "If quality_feedback_from_previous_attempt says user_prompt is too long, "
                    "rewrite it to at most 280 characters by removing repetition and secondary context. "
                    "Do not add any new scenario, constraint, or preference."
                ),
            },
            "start_area": start.get("area_name", ""),
            "public_start": {
                "city_name": start.get("city_name", ""),
                "place_name": start.get("name", ""),
                "address": start.get("address", ""),
            },
            "requested_service_type": evidence.get("service_type", ""),
            "target_search_ambiguity_score": quality_profile.get("search_ambiguity_score"),
            "selected_scenario_type": scenario_hint,
            "scenario_diversity_policy": [
                "Do not default to 'I am nearby and just finished shopping'.",
                "Use selected_scenario_type as the main situation frame unless it contradicts evidence.",
                "Choose one coherent scenario that fits the service evidence.",
                "The request may be current-location discovery, future travel planning, remote booking, area comparison, group outing, work/study break, budget-experience tradeoff, or time/weather-sensitive planning.",
                "Use start_area as a hard location hint only when the scenario naturally needs it; otherwise use it as an area preference or planning region.",
            ],
            "candidate_scenario_types": [
                "current_area_immediate_need",
                "tourist_itinerary_planning",
                "area_selection_discovery",
                "advance_reservation",
                "group_or_family_constraints",
                "work_or_study_break",
                "budget_experience_tradeoff",
                "time_or_weather_sensitive_plan",
            ],
            "natural_ambiguity": {
                "same_district_service_count": landscape.get("same_district_service_count"),
                "nearby_same_service_count_900m": landscape.get("nearby_same_service_count_900m"),
                "confusable_candidate_count": len(landscape.get("confusable_candidates", [])),
            },
            "gold_visible_attributes_for_constraint_design": {
                "place_type": gold.get("place_type"),
                "rating": gold.get("rating"),
                "price_band": gold.get("price_band"),
                "queue_time_min": gold.get("queue_time_min"),
                "crowding_level": gold.get("crowding_level"),
                "noise_level": gold.get("noise_level"),
                "indoor_available": gold.get("indoor_available"),
                "open_status": gold.get("open_status"),
            },
            "style_target": [
                "The user should sound like a person with plans, constraints, and uncertainty.",
                "The user_prompt must explicitly name both public_start.city_name and public_start.place_name so the user has a public, searchable current location.",
                "Keep user_prompt concise: one scenario, all hard constraints, and only the most discriminative soft preferences.",
                "Hide the benchmark mechanics; do not say candidate/detail/ranking unless it is natural.",
                "Embed constraints in context instead of listing fields.",
                "Vary the situation frame across generated examples; avoid repeating the same opening and same 'nearby after shopping' setup.",
            ],
        },
        "character_profile": _character_profile_for_llm(character, character_constraints) if character else None,
        "forbidden_in_user_prompt": forbidden,
        "forbidden_style": {
            "template_phrases": TEMPLATE_PHRASES,
            "internal_field_terms": INTERNAL_FIELD_TERMS,
        },
        "quality_feedback_from_previous_attempt": quality_feedback,
        "evidence_ledger": evidence,
        "quality_profile": quality_profile,
        "output_schema": {
            "prompt_language": prompt_language,
            "user_prompt": "2-4 natural sentences, 180-320 Unicode characters and never over 420; no gold name; no internal fields",
            "review_translation": {
                "language": review_translation_language,
                "user_prompt": "faithful translation for human review",
                "notes": "brief note about ambiguous expressions, if any",
            },
            "hard_constraints": ["must-satisfy constraints inferred from the prompt"],
            "soft_preferences": ["preferences or tradeoffs inferred from the prompt"],
            "ranking_criteria": ["criteria the judge expects the agent to use"],
            "evaluation": {
                "must_mention": ["gold name and key constraints, for judge only"],
                "must_avoid": ["bad reasoning patterns"],
            },
            "prompt_evidence_map": {"natural phrase or implied constraint": ["evidence json path"]},
        },
    }


def _normalize_prompt_payload(
    payload: dict[str, Any],
    *,
    prompt_language: str,
    review_translation_language: str,
) -> None:
    payload.setdefault("prompt_language", prompt_language)
    payload.setdefault("review_translation", {})
    if isinstance(payload["review_translation"], dict):
        payload["review_translation"].setdefault("language", review_translation_language)
        if prompt_language == review_translation_language:
            payload["review_translation"].setdefault("user_prompt", payload.get("user_prompt", ""))


def validate_prompt_payload(
    payload: dict[str, Any],
    *,
    evidence: dict[str, Any],
    expected_language: str | None = None,
) -> list[str]:
    issues = []
    prompt = str(payload.get("user_prompt", "")).strip()
    if len(prompt) < 35:
        issues.append("user_prompt is too short")
    if len(prompt) > 420:
        issues.append("user_prompt is too long")
    if _sentence_count(prompt) < 2:
        issues.append("user_prompt should contain at least two natural sentences")
    if expected_language and payload.get("prompt_language") != expected_language:
        issues.append("prompt_language mismatch")
    start = evidence.get("start", {}) or {}
    for label, value in (
        ("start city", start.get("city_name")),
        ("start place", start.get("name")),
    ):
        if value and str(value).casefold() not in prompt.casefold():
            issues.append(f"user_prompt omits public {label}: {value}")
    for term in _forbidden_terms(evidence):
        if term and term in prompt:
            issues.append(f"user_prompt leaks gold term: {term}")
    for term in INTERNAL_FIELD_TERMS:
        if term in prompt:
            issues.append(f"user_prompt leaks internal field term: {term}")
    for phrase in TEMPLATE_PHRASES:
        if phrase and phrase in prompt:
            issues.append(f"user_prompt is template-like: {phrase}")
    if _looks_like_generated_instruction(prompt):
        issues.append("user_prompt reads like a generated instruction instead of a user situation")
    if _leaks_raw_attribute_values(prompt):
        issues.append("user_prompt leaks raw attribute values instead of natural language")
    review_translation = payload.get("review_translation")
    if not isinstance(review_translation, dict) or not review_translation.get("user_prompt"):
        issues.append("review_translation.user_prompt is required")
    for field in ["hard_constraints", "soft_preferences", "ranking_criteria"]:
        if not isinstance(payload.get(field), list) or not payload.get(field):
            issues.append(f"{field} must be a non-empty list")
    if len(payload.get("ranking_criteria") or []) < 4:
        issues.append("ranking_criteria should cover multiple constraints")
    if not isinstance(payload.get("evaluation"), dict):
        issues.append("evaluation must be an object")
    if not isinstance(payload.get("prompt_evidence_map"), dict) or not payload.get("prompt_evidence_map"):
        issues.append("prompt_evidence_map is missing")
    return issues


def parse_llm_json_response(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object")
    return payload


def _sentence_count(prompt: str) -> int:
    separators = "。！？!?."
    return sum(1 for char in prompt if char in separators)


def _looks_like_generated_instruction(prompt: str) -> bool:
    stripped = prompt.strip()
    instruction_markers = [
        "请根据",
        "请结合",
        "帮我比较候选",
        "推荐最合适",
        "inspect their details",
        "compare the trade-offs",
    ]
    marker_count = sum(1 for marker in instruction_markers if marker in stripped)
    return marker_count >= 2


def _leaks_raw_attribute_values(prompt: str) -> bool:
    raw_values = [" low", " medium", " high", "very_high", "open_now", "closed", "restaurant_reservation"]
    return any(value in f" {prompt} " for value in raw_values)


def _chat_completion(endpoint: dict[str, Any], messages: list[dict[str, Any]], temperature: float) -> dict[str, Any]:
    base_url = str(endpoint.get("base_url", "")).rstrip("/")
    session = requests.Session()
    session.trust_env = False
    started_at = time.monotonic()
    try:
        response = session.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {endpoint.get('api_key', '')}",
            "Content-Type": "application/json",
        },
        json={
            "model": endpoint.get("model", ""),
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
            # Discovery prompts request a compact ranked rationale, not an
            # unbounded essay.  Keep the real LLM response resource-bounded.
            "max_tokens": 1400,
        },
        timeout=120,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        record_external_usage(provider="llm", operation="chat_completions", status="failed",
                              elapsed_s=time.monotonic() - started_at, usage={"request_units": 1},
                              model=endpoint.get("model", ""), error_class=type(exc).__name__)
        raise
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object")
    usage = payload.get("usage") or {}
    prompt = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    completion = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    record_external_usage(provider="llm", operation="chat_completions", status="success",
                          elapsed_s=time.monotonic() - started_at,
                          usage={"request_units": 1, "prompt": prompt, "completion": completion,
                                 "total": int(usage.get("total_tokens", prompt + completion) or 0)},
                          model=endpoint.get("model", ""))
    return payload


def _message_content(raw: dict[str, Any]) -> str:
    choices = raw.get("choices") or []
    if not choices:
        raise ValueError("LLM response has no choices")
    content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM response has empty content")
    return content


def _forbidden_terms(evidence: dict[str, Any]) -> list[str]:
    gold = evidence.get("gold_service", {})
    return [str(item) for item in [gold.get("name"), gold.get("service_id"), gold.get("place_id")] if item]


def _scenario(service_type: str) -> str:
    return {
        "restaurant_reservation": "quick_meal",
        "cafe_reservation": "seated_cafe",
        "hotel_booking": "tourist_hotel",
        "attraction_visit": "local_visit",
    }.get(service_type, "local_discovery")


def _constraints(
    service_type: str,
    gold: dict[str, Any],
    quality_profile: dict[str, Any],
) -> tuple[list[str], list[str]]:
    price = gold.get("price_band") or "medium"
    queue = gold.get("queue_time_min")
    hard = {
        "restaurant_reservation": ["步行范围内", f"价格不要高于 {price}", "能较快完成用餐"],
        "cafe_reservation": ["走路不远", "能坐下来停留", "不只是外带窗口"],
        "hotel_booking": ["交通方便", f"预算大致为 {price}", "周边有基础餐饮/咖啡配套"],
        "attraction_visit": ["距离当前区域合理", "开放状态可用", "适合短时间前往"],
    }.get(service_type, ["距离合理", "当前可用"])
    soft = {
        "restaurant_reservation": ["不要只按评分排序", "尽量少排队", "更适合真实用餐需求"],
        "cafe_reservation": ["咖啡品质好一些", "环境不要太吵", "适合整理材料或短暂停留"],
        "hotel_booking": ["不要住在游客最拥挤的一线位置", "夜间返回方便", "安静性和可达性需要平衡"],
        "attraction_visit": ["不要过度拥挤", "结合天气/室内外属性", "说明取舍"],
    }.get(service_type, ["说明推荐理由", "排除不合适候选"])
    search_ambiguity_score = int(quality_profile.get("search_ambiguity_score", 0))
    if search_ambiguity_score >= 7 and queue is not None:
        soft.append(f"需要考虑排队时间约 {queue} 分钟是否可接受")
    return hard, soft


def _zh_prompt(service_type: str, start: dict[str, Any], hard: list[str], soft: list[str]) -> str:
    area = start.get("area_name") or "附近"
    if service_type == "restaurant_reservation":
        return f"我今晚会在{area}一带活动，想提前找个走起来不折腾、价格也别太夸张的吃饭地点。最好别让我等太久，也希望你说明为什么它比附近其他选择更合适。"
    if service_type == "cafe_reservation":
        return f"我会在{area}附近留出一段空档，想找一家能坐下来待一会儿的咖啡店。距离不能太折腾，环境也别太吵，最好能解释它为什么适合短暂停留。"
    if service_type == "hotel_booking":
        return f"我准备去{area}附近活动，想找一个交通方便、预算适中、晚上回来也方便的酒店。希望它别只是位置热闹，还要兼顾安静程度和周边基础配套。"
    return f"我计划在{area}附近安排一个顺路停留的地点，但不想去太绕或太拥挤的地方。请帮我找一个当前安排下更稳妥的选择，并说明主要取舍。"


def _localized_prompt(language: str, user_prompt_zh: str, service_type: str, start: dict[str, Any]) -> str:
    lang = language.lower()
    area = start.get("area_name") or "this area"
    if lang.startswith("zh"):
        return user_prompt_zh
    if lang.startswith("ja"):
        target = "カフェ" if service_type == "cafe_reservation" else "ホテル" if service_type == "hotel_booking" else "お店"
        return f"{area}の近くで、条件に合う{target}を探したいです。距離や混雑、価格帯のバランスを見ながら、なぜそこが今の予定に合うのか説明してもらえますか。"
    target = "cafe" if service_type == "cafe_reservation" else "hotel" if service_type == "hotel_booking" else "place"
    return f"I'll be around {area} and need a suitable {target} that fits the situation, not just the most famous one. Please weigh distance, comfort, availability, and the likely trade-offs before suggesting where I should go."
