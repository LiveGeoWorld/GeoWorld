"""Prompt writing for batch-generated grounding examples.

The module exposes a deterministic evidence-backed writer so the batch pipeline
is usable without external LLM credentials. The system prompt file in
``prompts/system.md`` is the contract for future OpenAI-compatible integration.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from pathlib import Path
from typing import Any

import requests

from shared.config_loader import PipelineConfig
from shared.external_usage import record_external_usage
from task_module.task_types.place_grounding.batch_generation.config import pipeline_root
from task_module.task_types.place_grounding.batch_generation.evidence import prompt_evidence_map_from_prompt_parts, final_task_image_path


def system_prompt_path() -> Path:
    return Path(__file__).with_name("prompts") / "system.md"


def load_system_prompt() -> str:
    return system_prompt_path().read_text(encoding="utf-8")


def write_grounding_prompt_with_llm(
    *,
    evidence: dict[str, Any],
    quality_profile: dict[str, Any],
    granularity: str,
    config_yaml: str = "config.yaml",
    stage: str = "task_design",
    temperature: float = 0.7,
    allow_gold_name: bool = False,
    prompt_language: str = "en",
    review_translation_language: str = "zh-CN",
    # Backward-compatible alias
    difficulty_profile: dict[str, Any] | None = None,
    target_type: str = "poi",
) -> dict[str, Any]:
    """Use the configured LLM to write a tailored grounding prompt.

    The model receives evidence/provenance but must not copy the gold name into
    ``user_prompt`` unless explicitly allowed. Output is validated for basic
    style and evidence constraints before being accepted.
    """
    prompt_language = "en"
    place = evidence.get("place_evidence", {})
    quality_profile = quality_profile or difficulty_profile or {}
    cfg = PipelineConfig(config_yaml)
    endpoint_name = cfg.stage_model(stage)
    endpoint = cfg.model_endpoint(endpoint_name)
    clue_density = _clue_density_hint(quality_profile)
    user_payload = {
        "task": "write_place_grounding_example",
        "acceptable_granularity": granularity,
        "prompt_language": prompt_language,
        "review_translation_language": review_translation_language,
        "allow_gold_name_in_user_prompt": allow_gold_name,
        "forbidden_in_user_prompt": _forbidden_prompt_terms(place, allow_gold_name),
        "evidence_ledger": evidence,
        "quality_profile": quality_profile,
        "clue_density_hint": clue_density,
        "output_schema": {
            "prompt_language": prompt_language,
            "user_prompt": "A natural 2-4 sentence request for identifying the target using the single final task image and supplementary textual clues; make the intended place clear without revealing the answer or exhaustively describing the image",
            "review_translation": {
                "language": review_translation_language,
                "user_prompt": "translation of user_prompt for human review; preserve ambiguity and intent",
                "notes": "optional short review note about localized expressions",
            },
            "visual_clues": ["Useful evidence directly visible in the final task image; record it for audit without requiring every detail to be restated in user_prompt"],
            "text_clues": ["Clues actually expressed in user_prompt, supported by the supplied evidence; reference-image information used for identification must be conveyed here through the user request, not treated as visible in the final task image"],
            "negative_clues": ["distractor / exclusion clues if supported by evidence"],
            "candidate_scope": {
                "city_id": "Bench city id",
                "allowed_place_types": ["map place types"],
                "area_hints": ["district/area/anchor hints"],
                "address_hints": ["short road or area hints, not full raw address"],
                "nearby_anchors": ["nearby anchor names"],
            },
            "grounding_constraints": ["agent process constraints"],
            "evaluation": {"must_mention": [], "must_avoid": []},
            "prompt_evidence_map": {"prompt phrase or clue": ["evidence json path"]},
            "quality_profile": quality_profile,
        },
    }
    user_content = _user_content_with_images(
        text=json.dumps(user_payload, ensure_ascii=False, indent=2),
        images=_writer_images(evidence),
    )
    raw = _chat_completion(
        endpoint=endpoint,
        messages=[
            {"role": "system", "content": load_system_prompt()},
            {"role": "user", "content": user_content},
        ],
        temperature=temperature,
    )
    payload = parse_llm_json_response(_message_content(raw))
    payload.setdefault("quality_profile", quality_profile)
    payload.setdefault("difficulty_profile", quality_profile)
    payload.setdefault("prompt_language", prompt_language)
    payload.setdefault("review_translation", {})
    if isinstance(payload["review_translation"], dict):
        payload["review_translation"].setdefault("language", review_translation_language)
        if prompt_language == review_translation_language:
            payload["review_translation"].setdefault("user_prompt", payload.get("user_prompt", ""))
    issues = validate_prompt_payload(
        payload,
        evidence=evidence,
        allow_gold_name=allow_gold_name,
        expected_language=prompt_language,
        review_translation_language=review_translation_language,
    )
    if issues:
        raise ValueError("LLM prompt payload failed validation: " + "; ".join(issues))
    payload["generation_method"] = {
        "type": "llm",
        "stage": stage,
        "endpoint": endpoint_name,
        "model": endpoint.get("model", ""),
    }
    return payload


def deterministic_write_grounding_prompt(
    *,
    evidence: dict[str, Any],
    quality_profile: dict[str, Any],
    granularity: str,
    allow_gold_name: bool = False,
    prompt_language: str = "en",
    review_translation_language: str = "zh-CN",
    difficulty_profile: dict[str, Any] | None = None,
    target_type: str = "poi",
) -> dict[str, Any]:
    """Write a conservative, context-rich prompt from evidence only."""
    prompt_language = "en"
    quality_profile = quality_profile or difficulty_profile or {}
    place = evidence.get("place_evidence", {})
    anchors = place.get("nearby_anchors", [])
    confusable_names = [name for name in place.get("confusable_names", []) if name]
    place_type = _human_type(place.get("primary_type", ""), prompt_language)
    anchor_name = anchors[0].get("name", "") if anchors else ""
    district = place.get("district_id", "")
    district_hint = _human_district_hint(district)
    name = place.get("name", "")
    address_hint = _address_hint(place.get("address", ""))

    visual_clues = _visual_clues(place_type, anchor_name, quality_profile, prompt_language)
    text_clues = _localized_text_clues(
        prompt_language, place_type, anchor_name, district_hint, address_hint,
    )
    negative_clues = _localized_negative_clues(prompt_language, confusable_names[:2])

    if allow_gold_name and name:
        target_phrase = name
    elif place_type:
        target_phrase = f"这片{place_type}或同类地点附近"
    else:
        target_phrase = "这个位置附近"

    scene = _scene_context(place_type, anchor_name, district_hint, address_hint, quality_profile)
    uncertainty = _uncertainty_sentence(quality_profile)
    negative = _negative_sentence(negative_clues)
    user_prompt_zh = _zh_user_prompt(
        scene=scene,
        target_phrase=target_phrase,
        uncertainty=uncertainty,
        negative=negative,
    )
    user_prompt = _localized_fallback_prompt(
        language=prompt_language,
        user_prompt_zh=user_prompt_zh,
        place_type=place_type,
        anchor_name=anchor_name,
        address_hint=address_hint,
    )

    prompt_parts = {
        "visual_clues": visual_clues,
        "text_clues": text_clues,
        "negative_clues": negative_clues,
    }
    return {
        "prompt_language": prompt_language,
        "user_prompt": user_prompt,
        "review_translation": _review_translation_payload(
        language=review_translation_language,
        prompt_language=prompt_language,
            user_prompt=user_prompt,
            user_prompt_zh=user_prompt_zh,
        ),
        "visual_clues": visual_clues,
        "text_clues": text_clues,
        "negative_clues": negative_clues,
        "candidate_scope": {
            "city_id": place.get("city_id", ""),
            "allowed_place_types": [place.get("primary_type", "")] if place.get("primary_type") else [],
            "area_hints": [item for item in [district, anchor_name] if item],
            "address_hints": [address_hint] if address_hint else [],
            "nearby_anchors": [anchor.get("name", "") for anchor in anchors if anchor.get("name")],
        },
        "grounding_constraints": _localized_grounding_constraints(prompt_language),
        "evaluation": {
            "must_mention": [item for item in [name, anchor_name, place_type] if item],
            "must_avoid": confusable_names[:2],
        },
        "prompt_evidence_map": prompt_evidence_map_from_prompt_parts(prompt_parts),
        "quality_profile": quality_profile,
        "difficulty_profile": quality_profile,
        "generation_method": {"type": "deterministic_fallback"},
    }


def parse_llm_json_response(text: str) -> dict[str, Any]:
    """Parse a JSON object from an LLM response."""
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object")
    return payload


def _has_untranslated_cjk(value: Any, evidence: dict[str, Any]) -> bool:
    """Allow exact frozen names; this guard is not a general language detector."""
    place = evidence.get("place_evidence") or {}
    names = [place.get("name", ""), *(place.get("confusable_names") or [])]
    names.extend(item.get("name", "") for item in place.get("nearby_anchors", []) if isinstance(item, dict))
    names.extend(item.get("name", "") for item in (evidence.get("ambiguity_landscape") or {}).get("confusable_candidates", []) if isinstance(item, dict))
    names = sorted({name for name in names if isinstance(name, str) and name}, key=len, reverse=True)

    def check(item: Any) -> bool:
        if isinstance(item, dict):
            return any(check(v) for k, v in item.items() if k not in {"city_id", "district_id", "place_id"})
        if isinstance(item, list):
            return any(check(v) for v in item)
        if not isinstance(item, str):
            return False
        for name in names:
            item = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", " ", item)
        return bool(re.search(r"[\u3400-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]", item))

    return check(value)


def validate_prompt_payload(
    payload: dict[str, Any],
    *,
    evidence: dict[str, Any],
    allow_gold_name: bool = False,
    expected_language: str | None = None,
    review_translation_language: str | None = None,
) -> list[str]:
    """Basic quality gate for LLM-written prompt payloads."""
    issues: list[str] = []
    prompt = str(payload.get("user_prompt", "")).strip()
    place = evidence.get("place_evidence", {})
    if len(prompt) < 45:
        issues.append("user_prompt is too short")
    for term in _forbidden_prompt_terms(place, allow_gold_name):
        if term and term in prompt:
            issues.append(f"user_prompt leaks forbidden term: {term}")
    if re.search(r"\b(place_|district_|candidate_id|primary_type|map_projection|lat|lng)\b", prompt):
        issues.append("user_prompt contains raw schema/map field names")
    if _looks_like_template(prompt):
        issues.append("user_prompt looks templated or field-concatenated")
    language = str(payload.get("prompt_language", "")).strip()
    if expected_language and language and language != expected_language:
        issues.append(f"prompt_language mismatch: expected {expected_language}, got {language}")
    if expected_language and not language:
        issues.append("prompt_language is missing")
    if language.lower().startswith("en") and _has_untranslated_cjk(prompt, evidence):
        issues.append("English user_prompt contains untranslated CJK text")
    if language.lower().startswith("zh") and not re.search(r"[\u4e00-\u9fff]", prompt):
        issues.append("Chinese user_prompt does not contain Chinese natural language")
    raw_type_terms = {"restaurant", "shopping_mall", "cafe", "hotel", "hospital", "park", "museum", "cinema"}
    if language.lower().startswith("zh") and any(re.search(rf"\b{re.escape(term)}\b", prompt, re.I) for term in raw_type_terms):
        issues.append("Chinese user_prompt contains a raw English place-type enum")
    evaluation = payload.get("evaluation") or {}
    mentions = evaluation.get("must_mention") if isinstance(evaluation, dict) else []
    if not isinstance(mentions, list):
        issues.append("evaluation.must_mention must be a list")
    else:
        mention_text = " ".join(str(item) for item in mentions)
        if language.lower().startswith("en") and _has_untranslated_cjk(mention_text, evidence):
            issues.append("English evaluation terms contain untranslated CJK text")
        if language.lower().startswith("zh") and any(re.search(rf"\b{re.escape(term)}\b", mention_text, re.I) for term in raw_type_terms):
            issues.append("Chinese evaluation terms contain raw English place-type enums")
    if language.lower().startswith("en"):
        for field in ("visual_clues", "text_clues", "negative_clues", "grounding_constraints", "evaluation", "candidate_scope"):
            if _has_untranslated_cjk(payload.get(field), evidence):
                issues.append(f"English {field} contains untranslated CJK text")
    review_translation = payload.get("review_translation")
    if not isinstance(review_translation, dict):
        issues.append("review_translation must be an object")
    elif prompt and expected_language and review_translation_language and expected_language != review_translation_language:
        translated_prompt = str(review_translation.get("user_prompt", "")).strip()
        translated_language = str(review_translation.get("language", "")).strip()
        if not translated_prompt:
            issues.append("review_translation.user_prompt is missing")
        if translated_language and translated_language != review_translation_language:
            issues.append(
                f"review_translation.language mismatch: expected {review_translation_language}, got {translated_language}"
            )
    for field in ["visual_clues", "text_clues", "negative_clues"]:
        if not isinstance(payload.get(field), list):
            issues.append(f"{field} must be a list")
    if not isinstance(payload.get("candidate_scope"), dict):
        issues.append("candidate_scope must be an object")
    evidence_map = payload.get("prompt_evidence_map")
    if not isinstance(evidence_map, dict) or not evidence_map:
        issues.append("prompt_evidence_map is missing")
    return issues


def _chat_completion(endpoint: dict[str, Any], messages: list[dict[str, Any]], temperature: float) -> dict[str, Any]:
    base_url = str(endpoint.get("base_url", "")).rstrip("/")
    if not base_url:
        raise ValueError("LLM endpoint missing base_url")
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
            # The evidence card is intentionally compact.  Bound completion
            # length so a multimodal prompt cannot stall a validation batch.
            "max_tokens": 1600,
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


def _user_content_with_images(text: str, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for number, item in enumerate(images, 1):
        if number == 1:
            label = (f"Image {number} — Final task image\nEvidence reference: image_assets[{item['index']}]\n\n"
                     "This is the single image supplied to the tested agent. Use it to establish "
                     "the visible clues and clarify which place the request concerns. Do not "
                     "automatically transcribe all its visible details into the user request.")
        else:
            label = (f"Image {number} — Authoring reference image\nEvidence reference: image_assets[{item['index']}]\n"
                     f"Viewing direction: {item['heading']}\n\n"
                     "Use this additional view to find useful contextual information about the "
                     "surroundings. If you use that information as a clue for the tested agent, "
                     "express it in the user request; do not describe it as visible in the final "
                     "task image. This reference image is not supplied to the tested agent.")
        content.append({"type": "text", "text": label})
        content.append({"type": "image_url", "image_url": {"url": _image_data_url(item['path'])}})
    return content


def _writer_images(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    root = pipeline_root()
    selected = final_task_image_path(evidence)
    if not selected:
        raise ValueError("Missing final task image for Grounding authoring")
    final_path = (root / selected).resolve()
    if not final_path.is_file():
        raise FileNotFoundError(f"Final task image does not exist: {final_path}")
    results = []
    assets = evidence.get("image_assets", [])
    seen = set()
    for index, asset in enumerate(assets):
        if asset.get("quality") == "fetch_failed" or not asset.get("path"):
            continue
        raw = Path(asset["path"])
        path = (raw if raw.is_absolute() else root / raw).resolve()
        if not path.is_file() or path in seen:
            continue
        seen.add(path)
        results.append({"path": path, "index": index, "heading": (asset.get("viewpoint") or {}).get("heading", "not recorded")})
    results.sort(key=lambda item: item['path'] != final_path)
    return results


def _image_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _message_content(raw: dict[str, Any]) -> str:
    choices = raw.get("choices") or []
    if not choices:
        raise ValueError("LLM response has no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("LLM response has empty content")
    return content


def _forbidden_prompt_terms(place: dict[str, Any], allow_gold_name: bool) -> list[str]:
    terms = []
    if not allow_gold_name and place.get("name"):
        terms.append(str(place.get("name")))
    if place.get("place_id"):
        terms.append(str(place.get("place_id")))
    if place.get("district_id"):
        terms.append(str(place.get("district_id")))
    return terms


def _looks_like_template(prompt: str) -> bool:
    markers = [
        "看起来不像纯住宅区，更像是有",
        "我手头只有这些零散印象",
        "请不要直接猜，先根据图片和这段描述去地图里找候选",
    ]
    return sum(1 for marker in markers if marker in prompt) >= 2


def _human_type(value: str, language: str = "zh-CN") -> str:
    zh_mapping = {
        "park": "公园",
        "hospital": "医院",
        "doctor": "诊所",
        "medical_clinic": "诊所",
        "skin_care_clinic": "诊所",
        "dentist": "牙科诊所",
        "subway_station": "地铁站",
        "tourist_attraction": "景点",
        "shopping_mall": "商场",
        "hotel": "酒店",
        "restaurant": "餐厅",
        "chinese_restaurant": "中餐馆",
        "fast_food_restaurant": "快餐店",
        "cafe": "咖啡店",
        "coffee_shop": "咖啡店",
        "lodging": "酒店",
        "department_store": "商场",
        "parking_lot": "停车场",
    }
    en_mapping = {
        "park": "park", "hospital": "hospital", "doctor": "clinic",
        "medical_clinic": "clinic", "skin_care_clinic": "skin care clinic",
        "dentist": "dental clinic", "subway_station": "subway station",
        "tourist_attraction": "tourist attraction", "shopping_mall": "shopping mall",
        "hotel": "hotel", "restaurant": "restaurant", "chinese_restaurant": "Chinese restaurant",
        "fast_food_restaurant": "fast-food restaurant", "cafe": "cafe", "coffee_shop": "coffee shop",
        "lodging": "hotel", "department_store": "department store", "parking_lot": "parking lot",
    }
    mapping = en_mapping if language.lower().startswith("en") else zh_mapping
    return mapping.get(str(value or ""), str(value or "").replace("_", " "))


def _human_district_hint(value: str) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    parts = raw.replace("district_", "").split("_")
    return "".join(part for part in parts if part and part not in {"smoke", "corridor"})


def _address_hint(value: str) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    chinese = re.search(r"[\u4e00-\u9fffA-Za-z0-9号弄巷\\-]{2,24}(?:路|街|大道|巷|弄)", raw)
    if chinese:
        return chinese.group(0).strip(" ,，。")
    english = re.search(
        r"(?:\b\d+\s*,?\s*)?\b[A-Z][A-Za-z.'’\- ]{1,34}\s(?:Road|Street|Avenue|Lane|Square|High Street)\b",
        raw,
    )
    if english:
        return english.group(0).strip(" ,，。")
    return ""


def _visual_clues(place_type: str, anchor_name: str, quality_profile: dict[str, Any], language: str = "zh-CN") -> list[str]:
    if language.lower().startswith("en"):
        clues = [
            "Use the visible road and building setting together with map candidates.",
            "Treat the surrounding streetscape, not one sign, as the main clue.",
        ]
        if place_type:
            clues.insert(0, f"The setting may suggest a {place_type} or a similar place type.")
        if anchor_name:
            clues.append(f"Use the nearby landmark {anchor_name} to narrow the area.")
        if int(quality_profile.get("ambiguity_score", 0)) >= 7:
            clues.append("Similar candidates may share this visual setting, so rule out distractors.")
        return clues
    clues = ["街景中可见的道路/建筑环境需要和地图候选结合判断", "周边氛围而非单一招牌构成主要线索"]
    if place_type:
        clues.insert(0, f"图片环境可能体现{place_type}或相近地点类型")
    if anchor_name:
        clues.append(f"可能需要结合附近锚点 {anchor_name} 进行区域判断")
    if int(quality_profile.get("ambiguity_score", 0)) >= 7:
        clues.append("视觉环境可能和相似候选共享，需要排除干扰项")
    return clues


def _localized_text_clues(language: str, place_type: str, anchor_name: str,
                          district_hint: str, address_hint: str) -> list[str]:
    if language.lower().startswith("en"):
        values = []
        if place_type:
            values.append(f"The request implies a {place_type}.")
        if anchor_name:
            values.append(f"The user only vaguely recalls the nearby anchor {anchor_name}.")
        if district_hint:
            values.append(f"The streetscape points toward {district_hint}.")
        if address_hint:
            values.append(f"The area may be around {address_hint}.")
        return values
    values = []
    if place_type:
        values.append(f"用户描述中隐含地点类型：{place_type}")
    if anchor_name:
        values.append(f"用户只模糊记得附近有{anchor_name}一类锚点")
    if district_hint:
        values.append(f"叙述中的街区氛围指向{district_hint}")
    if address_hint:
        values.append(f"可能涉及{address_hint}一带")
    return values


def _localized_negative_clues(language: str, names: list[str]) -> list[str]:
    if language.lower().startswith("en"):
        return [f"Do not confuse it with {name}" for name in names]
    return [f"不要和{name}混淆" for name in names]


def _localized_grounding_constraints(language: str) -> list[str]:
    if language.lower().startswith("en"):
        return [
            "Use the image and text clues for place grounding.",
            "Search MapApp candidates before submitting the final candidate.",
            "Do not answer from text alone without map verification.",
        ]
    return [
        "必须基于图片和文字线索完成地点 grounding",
        "必须通过 MapApp 检索候选并提交最终 candidate",
        "不要只凭最终文本猜测答案",
    ]


def _scene_context(
    place_type: str,
    anchor_name: str,
    district_hint: str,
    address_hint: str,
    quality_profile: dict[str, Any],
) -> str:
    ambiguity = int(quality_profile.get("ambiguity_score", 5))
    lead = "朋友刚发来这张街边照片，让我帮忙确认位置。"
    if ambiguity >= 7:
        lead = "我只截到这张不太完整的街景图，里面的信息有点零散。"
    elif ambiguity <= 3:
        lead = "我路过时随手拍了这张照片，想回头确认一下具体位置。"
    clues = []
    if place_type:
        clues.append(f"看起来不像纯住宅区，更像是有{place_type}或相关设施的街边环境")
    if address_hint:
        clues.append(f"我隐约记得可能是在{address_hint}一带")
    if anchor_name:
        clues.append(f"附近好像还有一个比较明显的地点，但我只记得名字里像是{anchor_name}")
    elif district_hint:
        clues.append(f"整体感觉像是在{district_hint}这个方向的城市街区")
    return lead + "".join(f"{clue}。" for clue in clues)


def _zh_user_prompt(
    *,
    scene: str,
    target_phrase: str,
    uncertainty: str,
    negative: str,
) -> str:
    return (
        f"{scene}我想确认图片里这个地方到底是哪里，但我手头只有这些零散印象。"
        f"{uncertainty}{negative}"
        "请不要直接猜，先根据图片和这段描述去地图里找候选，再告诉我最可能的地点和判断依据。"
    )


def _localized_fallback_prompt(
    *,
    language: str,
    user_prompt_zh: str,
    place_type: str,
    anchor_name: str,
    address_hint: str,
) -> str:
    """Conservative non-LLM prompt templates for supported local languages."""
    lang = language.lower()
    if lang.startswith("zh"):
        return user_prompt_zh

    type_phrase = place_type or "place"
    anchor_phrase = f" near something I remember as {anchor_name}" if anchor_name else ""
    address_phrase = f" around {address_hint}" if address_hint else ""
    if lang.startswith("ja"):
        anchor_ja = f"近くに「{anchor_name}」という名前の目印があった気がします。" if anchor_name else ""
        address_ja = f"{address_hint}あたりだったような記憶もあります。" if address_hint else ""
        return (
            f"この写真の場所を確認したいのですが、手元にあるのは少し曖昧な記憶だけです。"
            f"{address_ja}{anchor_ja}"
            "見た目の手がかりと地図上の候補を照らし合わせて、最も可能性の高い場所を教えてください。"
        )

    return (
        f"I am trying to identify the place in this street-view image, but my memory is fragmentary. "
        f"It felt like an area with a {type_phrase}{anchor_phrase}{address_phrase}, though I may be mixing it up with nearby candidates. "
        "Please use the image and these hints to search the map first, then tell me the most likely location and why."
    )


def _clue_density_hint(quality_profile: dict[str, Any]) -> str:
    score = int(quality_profile.get("ambiguity_score", 5))
    if score <= 3:
        return "low_ambiguity_use_fewer_clues"
    if score >= 7:
        return "high_ambiguity_use_more_clues_to_keep_solvable"
    return "medium_ambiguity_use_balanced_clues"


def _review_translation_payload(language: str, prompt_language: str,
                                user_prompt: str, user_prompt_zh: str) -> dict[str, str]:
    translated = user_prompt if language.lower() == prompt_language.lower() else user_prompt_zh
    return {
        "language": language,
        "user_prompt": translated,
        "notes": "自动生成的人工审查翻译；保留原 prompt 的模糊性与线索意图。",
    }


def _uncertainty_sentence(quality_profile: dict[str, Any]) -> str:
    score = int(quality_profile.get("ambiguity_score", 5))
    if score >= 7:
        return "不过这些线索有点容易误导，附近可能有好几个看起来相似的候选。"
    if score <= 3:
        return "这些线索应该能缩小范围，但我还不能直接确认具体名字。"
    return "这些信息不是很完整，只能靠图片和地图候选一起判断。"


def _negative_sentence(negative_clues: list[str]) -> str:
    if not negative_clues:
        return ""
    softened = []
    for clue in negative_clues[:2]:
        name = clue.replace("不要和", "").replace("混淆", "").strip()
        softened.append(f"我担心它会和{name}这种附近候选混在一起")
    return "".join(f"{item}。" for item in softened)
