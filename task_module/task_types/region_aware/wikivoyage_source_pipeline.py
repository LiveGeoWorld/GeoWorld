"""Auditable Wikivoyage source preparation for Region task construction.

This module stops before task construction. It preserves the raw city page,
prepares coherent section chunks for an author-side LLM, and validates derived
context payloads without ever rewriting source text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable


PARSER_VERSION = "wikitext_structural.v5"
SOURCE_SCHEMA = "wikivoyage_source_snapshot.v4"
CONTEXT_SCHEMA = "wikivoyage_contextualized.v3"
RULE_SCHEMA = "regional_rules.v3"
PIPELINE_CONTRACT_VERSION = "region_source_pipeline.v10"
DEFAULT_MAX_SOURCE_REQUEST_CHARS = 2400
DEFAULT_MAX_LIST_ITEMS_PER_REQUEST = 5
DEFAULT_MAX_ATOMIC_SOURCE_CHARS = 8000

_HEADING_RE = re.compile(r"^(={2,6})\s*(.*?)\s*\1\s*$")
_LIST_RE = re.compile(r"^[*#:;]+\s*(.*)$")
_MEDIA_RE = re.compile(r"\[\[(?:File|Image):([^\]|]+)(?:\|([^\]]*))?\]\]", re.IGNORECASE)
_GALLERY_FILE_RE = re.compile(r"^\s*(?:File|Image):([^|\n]+)(?:\|(.*))?$", re.IGNORECASE)
_LISTING_TEMPLATE_RE = re.compile(
    r"^\s*\{\{\s*(?:listing|see|do|eat|drink|sleep|buy|go|vicinity|routebox)\b", re.IGNORECASE,
)
_DISPLAY_CONTROL_TEMPLATE_RE = re.compile(
    r"^\s*\{\{\s*(?:mapframe|mapmask)\b", re.IGNORECASE,
)

CONTEXTUALIZATION_SYSTEM_PROMPT = """You are a Wikivoyage source-context restorer.

The user message supplies one source context and a small ordered set of original
source items. A source item is either Wikitext or an image item with its original
Wikitext/caption and an author-side visual description. Return exactly this JSON object:
{"items":[{"index":1,"contextualized_text":"...","has_unresolved_ambiguity":false}]}

Return one result for every supplied item, preserving its index and order. Make
each item understandable outside the original page only by inheriting explicit
facts from the supplied source context. Preserve every negation, qualifier,
exception, probability, and uncertainty. Do not use outside knowledge, infer
omitted legal details, create a task, add facts, or reproduce source IDs. When a
reference cannot be resolved uniquely, retain the source meaning and set
has_unresolved_ambiguity to true. For an image item, use only the supplied
visible-description and caption; do not identify unshown people, locations,
laws, products, or events. Return JSON only."""

MEDIA_DESCRIPTION_SYSTEM_PROMPT = """You are an evidence-preserving visual describer for a Wikivoyage image.

Describe only plainly visible content that can later help a reader understand
the accompanying city-page source. Preserve visible negation, warning signs,
labels, restrictions, quantities, and uncertainty. Do not identify a place,
person, product, law, policy, event, or implication unless it is visibly shown
or stated in the supplied caption. Do not use outside knowledge or make a task.

Return exactly this JSON object:
{"visible_description":"...","has_unresolved_ambiguity":false}

The description must be concise and factual. Return JSON only."""

RULE_EXTRACTION_SYSTEM_PROMPT = """You are a Wikivoyage regional-rule formatter.

The user message supplies one source context and a small ordered set of
contextualized source items. Extract zero or more source-supported regional-rule
bundles. A bundle is one coherent regional condition. Keep a base rule, its
conditions, exceptions, and qualifications together as clauses. Split only facts
that apply independently to different subjects or activities.

Return exactly this JSON object:
{"rule_candidates":[{"rule_category":"...","subject":"...","summary":"...",
"clauses":[{"role":"base_rule","statement":"...","item_indexes":[1]}],
"scope":{"activity":null,"place_context":null,"time_context":null},
"extraction_status":"supported"}]}

Each clause must cite one or more supplied local item indexes, never source IDs.
Its role must be exactly one of: base_rule, condition, exception, qualification.
Do not decide whether a bundle can generate a task. Do not create a user scenario,
place, service, candidate set, task prompt, gold answer, or executable rule. Do
not use external knowledge or strengthen source wording. Use ambiguous or
incomplete when the source does not settle the proposition. Return JSON only."""

STAGE1_TEXT_SYSTEM_PROMPT = """你负责整理一个城市 Wikivoyage 页面中的来源内容。
你的任务是处理当前切片中的原始Wikitext 来源项，进行独立准则的拆分并进行结构化、上下文化和准则化，以便后续生成任务。

输入包含当前页面名称、当前来源单元所在的章节路径，以及当前切片中的原始Wikitext来源项。
请你按照如下步骤一步一步完成：
1. 补全能够由当前输入明确确定的代词、省略对象、适用场景和上下文，使后续每条结果脱离原段落后仍然完整可理解；
2. 将来源内容整理为结构化的地域准则；
3. 将能够独立成立、独立触发或独立应用的内容拆成不同准则。

拆分规则:
1. 一条 rule_candidate 只能表达一条独立准则。
如果当前切片包含多个具有不同主体、适用对象、触发条件、行为要求、限制内容或选择影响的独立陈述，必须分别输出多个 rule_candidate。

2. 每个 statement 必须是一条经过必要上下文补全后，可以脱离原段落独立理解的完整准则。
理解这条准则所必需的主体、适用场景、触发条件、例外和限定，都必须写入同一个 statement，不能依赖其他 rule_candidate 补充。

3. 当前切片中每条可以独立成立的来源陈述都应当恰好提取一次。
不得遗漏独立陈述，也不得把同一条独立陈述重复写入多个 rule_candidate。

4. 不得使用输入之外的知识，不得强化、弱化或改变来源含义。
上下文补全可以恢复原文能够明确确定的省略信息，但不得引入原文没有使用的新概念或专门名称。

5. 人名、地名、机构名、商户名、商品名及其他专有名称，应保持原始Wikitext 中的写法。

6. 每个 rule_candidate 必须通过 source_item_indexes 引用实际支持该 statement 的输入来源项。
source_item_indexes 中的数字必须来自输入的 source_items[].index。可以引用一个或多个来源项，但不得引用不存在或与该 statement 无关的 index。


只返回以下JSON格式内容：
{
  "rule_candidates": [
    {
      "statement": "填写经过上下文补全后，可以脱离原段落独立理解的一条完整准则。",
      "source_item_indexes": "填写实际支持该准则的 source_items[].index。数组中必须是输入里真实存在的一个或多个整数。",
      "extraction_status": "根据来源的实际支持程度，从 supported、ambiguous、incomplete 中选择一个。"
    },
    ...
  ]
}

示例：Wikivoyage内容:“许多印度人和部分华人吃素；多数马来人只吃清真食品；多数印度教徒不吃牛肉；如果不确定饮食限制，应提前询问”
该来源项的 index 为 1。
输出:
{
  "rule_candidates": [
    {
      "statement": "邀请新加坡朋友用餐时，应考虑其饮食限制；许多印度人和部分华人吃素。",
      "source_item_indexes": [1],
      "extraction_status": "supported"
    },
    {
      "statement": "邀请新加坡朋友用餐时，应考虑其饮食限制；多数马来人只吃清真食品。",
      "source_item_indexes": [1],
      "extraction_status": "supported"
    },
    {
      "statement": "邀请新加坡朋友用餐时，应考虑其饮食限制；多数印度教徒不吃牛肉。",
      "source_item_indexes": [1],
      "extraction_status": "supported"
    },
    {
      "statement": "邀请新加坡朋友用餐且不确定对方的饮食限制时，应提前询问。",
      "source_item_indexes": [1],
      "extraction_status": "supported"
    }
  ]
}
"""

STAGE1_MULTIMODAL_SYSTEM_PROMPT = """你负责整理一个城市 Wikivoyage 页面中的来源内容。
你的任务是处理当前切片中的原始Wikitext来源项和原始图片，进行独立准则的拆分并进行结构化、上下文化和准则化，以便后续生成任务。

输入包含当前页面名称、当前来源单元所在的章节路径、当前切片中的原始Wikitext来源项，以及与图片来源项对应的原始图片、原始Wikitext图片引用、file_reference和caption_source_text。
每张原始图片通过image_attachment_index与source_items中source_kind为image的来源项逐一对应。

请你按照如下步骤一步一步完成：
1. 分别处理当前切片中的Wikitext来源项和图片来源项，每张图片默认为一个独立的rule_candidate。
2. 结合图片引用以及说明文字，先客观、完整地观察并描述原始图片中明确可见的内容，包括可见对象、文字、行为、位置关系和场景。
3. 补全能够由当前输入明确确定的代词、省略对象、适用场景和上下文，使后续每条结果脱离原段落后仍然完整可理解；
4. 将能够独立成立、独立触发或独立应用的内容拆成不同准则；
5. 将来源内容整理为结构化的地域准则。

拆分规则:
1. 一条 rule_candidate 只能表达一条独立准则。
如果当前切片包含多个具有不同主体、适用对象、触发条件、行为要求、限制内容或选择影响的独立陈述，必须分别输出多个 rule_candidate。

2. 每个 statement 必须是一条经过必要上下文补全后，可以脱离原段落独立理解的完整准则。
理解这条准则所必需的主体、适用场景、触发条件、例外和限定，都必须写入同一个 statement，不能依赖其他 rule_candidate 补充。

3. 当前切片中每条可以独立成立的来源陈述都应当恰好提取一次。
不得遗漏独立陈述，也不得把同一条独立陈述重复写入多个 rule_candidate。

4. 不得使用输入之外的知识，不得强化、弱化或改变来源含义。
上下文补全可以恢复原文和图片能够明确确定的省略信息，但不得引入当前输入没有使用或明确显示的新概念或专门名称。

5. 人名、地名、机构名、商户名、商品名及其他专有名称，应保持原始Wikitext中的写法语言,对于图片也要表述一致。

6. 每个 rule_candidate 必须通过 source_item_indexes 引用实际支持该 statement 的输入来源项。
source_item_indexes 中的数字必须来自输入的 source_items[].index。可以引用一个或多个来源项，但不得引用不存在或与该 statement 无关的 index。

7. 每张图片都是一个独立的 rule_candidate，必须通过 source_item_indexes 引用对应的 source_kind 为 image 的来源项。

只返回以下JSON格式内容：
{
  "rule_candidates": [
    {
      "statement": "填写经过上下文补全后，可以脱离原段落独立理解的一条完整准则。",
      "source_item_indexes": "填写实际支持该准则的 source_items[].index。数组中必须是输入里真实存在的一个或多个整数。",
      "extraction_status": "根据来源的实际支持程度，从 supported、ambiguous、incomplete 中选择一个。"
    },
    ...
  ]
}

示例：Wikivoyage内容:“许多印度人和部分华人吃素；多数马来人只吃清真食品；多数印度教徒不吃牛肉；如果不确定饮食限制，应提前询问”
该来源项的 index 为 1。
输出:
{
  "rule_candidates": [
    {
      "statement": "邀请新加坡朋友用餐时，应考虑其饮食限制；许多印度人和部分华人吃素。",
      "source_item_indexes": [1],
      "extraction_status": "supported"
    },
    {
      "statement": "邀请新加坡朋友用餐时，应考虑其饮食限制；多数马来人只吃清真食品。",
      "source_item_indexes": [1],
      "extraction_status": "supported"
    },
    {
      "statement": "邀请新加坡朋友用餐时，应考虑其饮食限制；多数印度教徒不吃牛肉。",
      "source_item_indexes": [1],
      "extraction_status": "supported"
    },
    {
      "statement": "邀请新加坡朋友用餐且不确定对方的饮食限制时，应提前询问。",
      "source_item_indexes": [1],
      "extraction_status": "supported"
    }
  ]
}

如果当前切片没有可提取的地域准则，返回 {"rule_candidates":[]}。
"""

STAGE2_ASSESSMENT_SYSTEM_PROMPT = """You are an expert responsible for reviewing one structured Wikivoyage regional rule for a city benchmark.

The rule input contains:
1. one structured Wikivoyage regional-rule object containing only one complete rule statement and the original source items for that statement;
2. one closed mapping whose keys are integer indexes and whose values are available_primary_types that actually exist in the current frozen city map.

Process the input step by step in the following order:
1. Determine whether the current complete rule statement contains multiple restrictions that can stand or apply independently. If it does, list those restrictions separately and stop all subsequent assessment. Do not consider the original source items for this statement.

2. If the rule cannot be split further, determine:
- whether the rule is a city- or region-related fact;
- whether its applicable subject or situation is clear;
- whether the rule can affect the choice of real places within the city.
Set task_source_eligible to true only when all three checks are true and at least one affected POI type exists. Otherwise, set task_source_eligible to false and affected_poi_type_indexes must be empty.

3. If the rule is suitable for task generation, select from the given mapping the POI type indexes that can genuinely be affected by the rule. Do not output an index outside the mapping. If the rule cannot affect a choice among places within the city, or can only be used to ask for direct recitation of knowledge, it is not suitable for task generation.

Output requirements:
Return only the JSON corresponding to the assessment result and no other content.

If the current rule is one atomic restriction that cannot be split further, return:
{
  "atomicity": "atomic",
  "checks": {
    "regional_fact": "Set to true or false according to whether the current rule is a city- or region-related fact",
    "has_applicability_scope": "Set to true or false according to whether the applicable subject or situation is clear",
    "can_distinguish_real_choices": "Set to true or false according to whether the current rule can affect the choice of real places within the city"
  },
  "affected_poi_type_indexes": [
    "Provide the available_primary_types indexes actually affected by the current rule. Every array element must be an integer index that genuinely exists in the input; return an empty array when no type qualifies"
  ],
  "poi_retrieval_phrases": [
    "Provide specific object names or complete proper names used to retrieve POIs. Every array element must come from the current rule itself; return an empty array when no qualifying retrieval phrase exists"
  ],
  "task_source_eligible": "Set to true only when all three checks are true and affected_poi_type_indexes is non-empty; otherwise set to false",
  "reason": "Briefly explain the basis for the assessments above"
}

If the current rule contains multiple restrictions that can stand or apply independently, return:
{
  "atomicity": "needs_split",
  "independent_restrictions": [
    "Provide the first complete independent restriction identified in the current rule",
    "Provide the second complete independent restriction identified in the current rule",
    ...
  ],
  "reason": "Briefly explain why these restrictions can stand or apply independently"
}
When atomicity is needs_split, independent_restrictions must contain at least two restrictions. Every restriction must come from the current rule itself; do not add a restriction that appears only elsewhere in the original source.

Example 1:
Input:
regional_rule:
"When inviting most Malay Singaporeans to a meal, consider that they generally eat only halal food."
available_primary_types:
{
  "12": "restaurant",
  "18": "food_court",
  "41": "shopping_mall"
}

Output:
{
  "atomicity": "atomic",
  "checks": {
    "regional_fact": true,
    "has_applicability_scope": true,
    "can_distinguish_real_choices": true
  },
  "affected_poi_type_indexes": [12, 18],
  "poi_retrieval_phrases": [
    "halal food",
    "halal"
  ],
  "task_source_eligible": true,
  "reason": "The rule has a clear applicable group and dietary requirement and can affect the choice of dining place; halal food and halal are specific expressions in the current rule that can be used to retrieve relevant POIs."
}

Example 2:
Input:
regional_rule:
"When inviting guests to a meal, consider that many Indians and some Chinese are vegetarian, most Malays eat only halal food, and most Hindus do not eat beef."

Output:
{
  "atomicity": "needs_split",
  "independent_restrictions": [
    "When inviting guests to a meal, consider that many Indians and some Chinese are vegetarian.",
    "When inviting guests to a meal, consider that most Malays eat only halal food.",
    "When inviting guests to a meal, consider that most Hindus do not eat beef."
  ],
  "reason": "Vegetarian food, halal food, and avoiding beef concern different applicable groups and dietary requirements and can independently affect place selection, so they must be split into three independent rules."
}
"""

STAGE2_ASSESSMENT_SYSTEM_PROMPT_ZH = """你是城市基准中负责审核一条结构化 Wikivoyage 地域准则的审核专家。

准则包含以下内容：
1. 一个结构化 Wikivoyage 地域准则对象，其中只包含一条完整准则描述，以及该描述的原始来源项；
2. 一个封闭映射，键为整数索引，值为当前冻结城市地图中实际存在的 available_primary_types。

请你一步一步来按照以下步骤进行处理:
1. 判断当前完整准则描述是否包含多条能够独立成立或独立应用的限制。如果包含，则分别列出这些限制并停止后续判断。该描述的原始来源项不做考虑。

2. 如果准则不可再拆，判断：
- 该准则是否属于城市或地域相关事实；
- 适用对象或适用情形是否明确；
- 该准则是否会影响城市内部真实地点的选择。
仅当三项判断全部为 true，并且至少有一个受影响的 POI 类型时，将task_source_eligible 标记为 true。否则，task_source_eligible 为 false同时affected_poi_type_indexes 必须为空。

3. 如果准则适合用于生成任务，从给定映射中选择确实可能受该准则影响的 POI 类型索引。不得输出映射之外的索引。如果准则不能影响城市内部地点选择，或者只能用于直接复述知识，则不能用于生成任务。

输出要求如下:
只返回与判断结果对应的 JSON，不要返回其他内容。

如果当前准则是一条不可再拆的单一限制，返回：
{
  "atomicity": "atomic",
  "checks": {
    "regional_fact": "根据当前准则是否属于城市或地域相关事实，填写 true 或 false",
    "has_applicability_scope": "根据适用对象或适用情形是否明确，填写 true 或 false",
    "can_distinguish_real_choices": "根据当前准则是否能够影响城市内部真实地点的选择，填写 true 或 false"
  },
  "affected_poi_type_indexes": [
    "填写实际受当前准则影响的 available_primary_types 索引。数组元素必须是输入中真实存在的整数索引；没有符合条件的类型时返回空数组"
  ],
  "poi_retrieval_phrases": [
    "填写用于检索POI的具体对象名称或完整专有名称。数组元素必须来自当前准则本身；没有符合条件的检索词时返回空数组"
  ],
  "task_source_eligible": "仅当 checks 中三项均为 true 且 affected_poi_type_indexes 非空时填写 true，否则填写 false",
  "reason": "简要说明上述判断的依据"
}

如果当前准则包含多条能够独立成立或独立应用的限制，返回：
{
  "atomicity": "needs_split",
  "independent_restrictions": [
    "填写从当前准则中识别出的第一条完整独立限制",
    "填写从当前准则中识别出的第二条完整独立限制",
    ...
  ],
  "reason": "简要说明这些限制为什么能够独立成立或独立应用"
}
当 atomicity 为 needs_split 时，independent_restrictions 必须至少包含两条限制。每条限制都必须来自当前准则本身，不得加入只存在于原始来源其他内容中的限制。

示例:
输入：
regional_rule：
“邀请新加坡的多数马来人用餐时，应考虑他们通常只吃清真（halal）食品。”
available_primary_types：
{
  "12": "restaurant",
  "18": "food_court",
  "41": "shopping_mall"
}

输出：
{
  "atomicity": "atomic",
  "checks": {
    "regional_fact": true,
    "has_applicability_scope": true,
    "can_distinguish_real_choices": true
  },
  "affected_poi_type_indexes": [12, 18],
  "poi_retrieval_phrases": [
    "清真食品",
    "halal"
  ],
  "task_source_eligible": true,
  "reason": "该准则具有明确的适用对象和饮食要求，会影响餐饮地点选择；清真食品和 halal 是当前准则中能够用于检索相关 POI 的具体表达。"
}

示例2:
输入：
regional_rule：
“邀请客人用餐时，应考虑许多印度人和部分华人吃素、多数马来人只吃清真食品，以及多数印度教徒不吃牛肉。”

输出：
{
  "atomicity": "needs_split",
  "independent_restrictions": [
    "邀请客人用餐时，应考虑许多印度人和部分华人吃素。",
    "邀请客人用餐时，应考虑多数马来人只吃清真食品。",
    "邀请客人用餐时，应考虑多数印度教徒不吃牛肉。"
  ],
  "reason": "素食、清真食品和不食牛肉分别针对不同适用对象和饮食要求，可以独立影响地点选择，因此必须拆分为三条独立准则。"
}
"""


def build_source_snapshot(
    *, city_id: str, page_title: str, language: str, dump_file: str, raw_wikitext: str,
) -> dict[str, Any]:
    """Create an immutable, structured source snapshot from one raw page."""
    return {
        "schema_version": SOURCE_SCHEMA,
        "parser_version": PARSER_VERSION,
        "city_id": city_id,
        "source": {
            "source": "wikivoyage",
            "page_title": page_title,
            "language": language,
            "dump_file": dump_file,
            "page_wikitext": raw_wikitext,
            "page_hash": _fingerprint(raw_wikitext),
        },
        "sections": _parse_target_sections(raw_wikitext, city_id),
    }


def build_source_chunks(
    snapshot: dict[str, Any], *, max_source_chars: int = DEFAULT_MAX_SOURCE_REQUEST_CHARS,
    max_list_items: int = DEFAULT_MAX_LIST_ITEMS_PER_REQUEST,
    max_atomic_source_chars: int = DEFAULT_MAX_ATOMIC_SOURCE_CHARS,
) -> list[dict[str, Any]]:
    """Plan bounded LLM requests without changing the immutable source structure.

    A source group is immutable provenance, not an LLM request boundary. Within
    one section, consecutive ordinary source items may share a bounded request.
    Lists, tables, media, and template-container markers close that request.
    """
    if min(max_source_chars, max_list_items, max_atomic_source_chars) <= 0:
        raise ValueError("source request limits must be positive")
    chunks: list[dict[str, Any]] = []
    for section in snapshot.get("sections", []):
        blocks = section.get("blocks", [])
        group_positions = _source_group_positions(blocks)
        ordinary: list[dict[str, Any]] = []

        def flush_ordinary() -> None:
            nonlocal ordinary
            for unit in _split_nonlist_request_units(
                ordinary, max_source_chars, max_list_items, max_atomic_source_chars,
            ):
                chunks.append(_make_chunk(
                    snapshot, section, unit, len(chunks) + 1, group_positions,
                ))
            ordinary = []

        index = 0
        while index < len(blocks):
            block = blocks[index]
            block_type = block.get("block_type")
            if block_type in {"paragraph", "template_record", "media"}:
                ordinary.append(block)
                index += 1
                continue

            flush_ordinary()
            if block_type == "list_item":
                list_group_id = block.get("semantic_group_id")
                list_blocks: list[dict[str, Any]] = []
                while (
                    index < len(blocks)
                    and blocks[index].get("block_type") == "list_item"
                    and blocks[index].get("semantic_group_id") == list_group_id
                ):
                    list_blocks.append(blocks[index])
                    index += 1
                for unit in _split_list_request_units(
                    list_blocks, max_source_chars, max_list_items, max_atomic_source_chars,
                ):
                    chunks.append(_make_chunk(
                        snapshot, section, unit, len(chunks) + 1, group_positions,
                    ))
                continue

            if block_type == "table":
                chunks.append(_make_chunk(
                    snapshot, section, [block], len(chunks) + 1, group_positions,
                ))
            # Non-processable container markers deliberately close the request.
            index += 1
        flush_ordinary()
    return chunks


def build_stage1_request(
    snapshot: dict[str, Any], chunk: dict[str, Any],
    *, image_data_urls: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build one text or multimodal Stage-1 request for a bounded source slice."""
    supplied_images = image_data_urls or {}
    media_blocks = [block for block in chunk["blocks"] if block.get("block_type") == "media"]
    missing = [
        str(block.get("media_id") or "") for block in media_blocks
        if str(block.get("media_id") or "") not in supplied_images
    ]
    if missing:
        raise ValueError(f"Stage 1 multimodal request is missing images: {missing}")

    source_items: list[dict[str, Any]] = []
    ordered_images: list[str] = []
    for index, block in enumerate(chunk["blocks"], start=1):
        if block.get("block_type") == "media":
            media_id = str(block.get("media_id") or "")
            ordered_images.append(supplied_images[media_id])
            source_items.append({
                "index": index,
                "source_kind": "image",
                "original_wikitext": str(block.get("source_text") or ""),
                "file_reference": str(block.get("file_reference") or ""),
                "caption_source_text": str(block.get("caption_source_text") or ""),
                "image_attachment_index": len(ordered_images),
            })
        else:
            source_items.append({
                "index": index,
                "source_kind": "wikitext",
                "original_content": str(block.get("source_text") or ""),
            })

    return {
        "system_prompt": STAGE1_MULTIMODAL_SYSTEM_PROMPT if media_blocks else STAGE1_TEXT_SYSTEM_PROMPT,
        "validation": {"kind": "stage1_extraction", "item_count": len(source_items)},
        "input": {
            "page_title": snapshot["source"]["page_title"],
            "section_path": list(chunk["section_path"]),
            "source_items": source_items,
        },
        "image_data_urls": ordered_images,
    }


def build_media_description_request(
    snapshot: dict[str, Any], media_block: dict[str, Any], *, image_data_url: str,
) -> dict[str, Any]:
    """Build a vision request without placing image bytes in persisted source JSON."""
    if media_block.get("block_type") != "media":
        raise ValueError("Media description requests require a media block")
    if not image_data_url.startswith("data:image/"):
        raise ValueError("Media description requires a supported image data URL")
    media_id = str(media_block.get("media_id") or "")
    section_path = next(
        (section.get("section_path") or [] for section in snapshot.get("sections", [])
         if any(str(block.get("media_id") or "") == media_id for block in section.get("blocks", []))),
        [],
    )
    return {
        "system_prompt": MEDIA_DESCRIPTION_SYSTEM_PROMPT,
        "validation": {"kind": "media_description"},
        "input": {
            "source_context": f"Wikivoyage page: {snapshot['source']['page_title']}. "
                              f"Section: {' > '.join(section_path) or 'Lead'}.",
            "caption_source_text": str(media_block.get("caption_source_text") or ""),
        },
        "image_data_url": image_data_url,
    }


def materialize_media_descriptions(
    snapshot: dict[str, Any], outputs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Persist visual descriptions as a derived asset, never inside raw source blocks."""
    items: list[dict[str, Any]] = []
    for section in snapshot.get("sections", []):
        for block in section.get("blocks", []):
            if block.get("block_type") != "media":
                continue
            media_id = str(block.get("media_id") or "")
            output = outputs.get(media_id)
            if not isinstance(output, dict):
                raise ValueError(f"Missing visual description for media item: {media_id}")
            _validate_media_description_output(output)
            items.append({
                "media_id": media_id,
                "source_text": str(block.get("source_text") or ""),
                "file_reference": str(block.get("file_reference") or ""),
                "caption_source_text": str(block.get("caption_source_text") or ""),
                "visible_description": output["visible_description"],
                "status": "ambiguous" if output["has_unresolved_ambiguity"] else "supported",
                "description_method": "author_vision_llm",
            })
    return {
        "schema_version": "wikivoyage_media_descriptions.v1",
        "city_id": snapshot["city_id"],
        "source_page_hash": snapshot["source"]["page_hash"],
        "media": items,
    }


def build_contextualization_request(
    snapshot: dict[str, Any], chunk: dict[str, Any],
    *, media_descriptions: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a model-facing request with no script identity or runtime fields."""
    return {
        "system_prompt": CONTEXTUALIZATION_SYSTEM_PROMPT,
        "validation": {"kind": "contextualization", "item_count": len(chunk["blocks"])},
        "input": {
            "source_context": _model_source_context(snapshot, chunk),
            "items": [
                _contextualization_input_item(block, index, media_descriptions or {})
                for index, block in enumerate(chunk["blocks"], start=1)
            ],
        },
    }


def materialize_contextualized_snapshot(
    snapshot: dict[str, Any], chunks: Iterable[dict[str, Any]], llm_outputs: Iterable[dict[str, Any]],
    *, media_asset: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind model-local indexes back to immutable evidence IDs in script-owned output."""
    text_items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk, output in zip(chunks, llm_outputs, strict=True):
        _validate_contextualization_output(output, len(chunk["blocks"]))
        for index, item in enumerate(output["items"], start=1):
            block = chunk["blocks"][index - 1]
            evidence_id = block["evidence_id"]
            if evidence_id in seen:
                raise ValueError(f"Repeated contextualized source item: {evidence_id}")
            record = {
                "evidence_id": evidence_id,
                "source_text": block["source_text"],
                "contextualized_text": item["contextualized_text"],
                "status": "ambiguous" if item["has_unresolved_ambiguity"] else "supported",
                "contextualization_method": "author_llm",
                "source_kind": block.get("block_type"),
            }
            if block.get("block_type") == "media":
                media_by_id = {
                    str(media.get("media_id") or ""): media
                    for media in (media_asset or {}).get("media", []) if isinstance(media, dict)
                }
                media = media_by_id.get(str(block.get("media_id") or ""))
                if media is None:
                    raise ValueError(f"Contextualization requires a visual description for {block.get('media_id')}")
                record["caption_source_text"] = media.get("caption_source_text", "")
                record["visible_description"] = media.get("visible_description", "")
                record["visual_description_status"] = media.get("status", "ambiguous")
            text_items.append(record)
            seen.add(evidence_id)
    return {
        "schema_version": CONTEXT_SCHEMA,
        "city_id": snapshot["city_id"],
        "source_page_hash": snapshot["source"]["page_hash"],
        "contextualized_blocks": text_items,
        "media_observations": list((media_asset or {}).get("media", [])),
    }


def build_rule_extraction_request(snapshot: dict[str, Any], context_asset: dict[str, Any], chunk: dict[str, Any]) -> dict[str, Any]:
    """Build the second author-side request after contextualization is persisted."""
    context_by_id = {item["evidence_id"]: item for item in context_asset.get("contextualized_blocks", [])}
    return {
        "system_prompt": RULE_EXTRACTION_SYSTEM_PROMPT,
        "validation": {"kind": "rule_extraction", "item_count": len(chunk["blocks"])},
        "input": {
            "source_context": _model_source_context(snapshot, chunk),
            "items": [
                {
                    "index": index,
                    "text": context_by_id[block["evidence_id"]]["contextualized_text"],
                    "has_unresolved_ambiguity": context_by_id[block["evidence_id"]]["status"] != "supported",
                }
                for index, block in enumerate(chunk["blocks"], start=1)
            ],
        },
    }


def materialize_regional_rules(
    snapshot: dict[str, Any], chunks: Iterable[dict[str, Any]], chunk_outputs: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Bind Stage-1 source indexes and exactly deduplicate complete statements."""
    valid_refs = {
        block.get("evidence_id") or block.get("media_id")
        for section in snapshot.get("sections", [])
        for block in section.get("blocks", [])
    }
    rules_by_signature: dict[str, dict[str, Any]] = {}
    for chunk, output in zip(chunks, chunk_outputs, strict=True):
        _validate_stage1_extraction_output(output, len(chunk["blocks"]))
        for candidate in output.get("rule_candidates", []):
            rule = _materialize_rule_candidate(snapshot["city_id"], candidate, chunk["blocks"], valid_refs)
            signature = _canonical_json({"statement": rule["statement"]})
            if signature in rules_by_signature:
                existing = rules_by_signature[signature]
                existing["evidence_refs"] = sorted(set(existing["evidence_refs"]) | set(rule["evidence_refs"]))
                continue
            rule["rule_id"] = "rr_" + snapshot["city_id"] + "_" + _fingerprint(signature)[:12]
            rules_by_signature[signature] = rule
    return {
        "schema_version": RULE_SCHEMA,
        "city_id": snapshot["city_id"],
        "source_page_hash": snapshot["source"]["page_hash"],
        "rules": list(rules_by_signature.values()),
    }


def replace_regional_rule_with_local_split(
    rules_asset: dict[str, Any], *, old_rule_id: str,
    replacements: Iterable[dict[str, Any]], valid_evidence_ids: set[str],
) -> tuple[dict[str, Any], list[str]]:
    """Replace one Stage-1 rule with validated independent rules in place."""
    rules = rules_asset.get("rules")
    if not isinstance(rules, list):
        raise ValueError("Stage 1 regional rules asset must contain a rules list")
    positions = [index for index, rule in enumerate(rules) if rule.get("rule_id") == old_rule_id]
    if len(positions) != 1:
        raise ValueError(f"Expected exactly one Stage 1 rule {old_rule_id!r}")
    city_id = str(rules_asset.get("city_id") or "")
    materialized: list[dict[str, Any]] = []
    for candidate in replacements:
        rule = _validate_rule_candidate(city_id, candidate, valid_evidence_ids)
        signature = _canonical_json({"statement": rule["statement"]})
        rule["rule_id"] = "rr_" + city_id + "_" + _fingerprint(signature)[:12]
        materialized.append(rule)
    new_ids = [rule["rule_id"] for rule in materialized]
    remaining_ids = {str(rule.get("rule_id") or "") for rule in rules if rule.get("rule_id") != old_rule_id}
    if len(new_ids) != len(set(new_ids)) or remaining_ids.intersection(new_ids):
        raise ValueError("Local Stage 1 split produced duplicate rule IDs")
    updated = json.loads(json.dumps(rules_asset, ensure_ascii=False))
    position = positions[0]
    updated["rules"][position:position + 1] = materialized
    return updated, new_ids


def build_stage2_assessment_request(
    rule: dict[str, Any], source_evidence: dict[str, Any], poi_type_catalog: list[str],
) -> dict[str, Any]:
    """Build the single, world-bound Stage-2 request for one Stage-1 rule."""
    statement = str(rule.get("statement") or "").strip()
    if not statement:
        raise ValueError("Stage 2 requires a non-empty Stage 1 statement")
    return {
        "system_prompt": STAGE2_ASSESSMENT_SYSTEM_PROMPT_ZH,
        "validation": {
            "kind": "stage2_assessment",
            "allowed_poi_type_indexes": list(range(1, len(poi_type_catalog) + 1)),
        },
        "input": {
            "regional_rule": {"statement": statement},
            "source_items": [
                {
                    "index": index,
                    "source_kind": block.get("source_kind", "wikitext"),
                    "original_source_text": block.get("source_text", ""),
                    **({"caption_source_text": block.get("caption_source_text", ""),
                        "visible_description": block.get("visible_description", "")}
                       if block.get("source_kind") == "media" else {}),
                }
                for index, block in enumerate(source_evidence.get("blocks", []), start=1)
            ],
            "available_primary_types": {
                str(index): primary_type
                for index, primary_type in enumerate(poi_type_catalog, start=1)
            },
        },
    }


def build_poi_type_catalog(world: dict[str, Any]) -> list[str]:
    """Return the frozen map's stable, semantic-only Stage-2 type catalog."""
    types: set[str] = set()
    for place in world.get("places") or []:
        if not isinstance(place, dict):
            continue
        primary_type = str(place.get("primary_type") or "").strip()
        if primary_type:
            types.add(primary_type)
    return sorted(types)


def stage2_source_rules(rules_asset: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only source-supported Stage-1 rules admitted to Stage 2."""
    rules = rules_asset.get("rules") or []
    if not isinstance(rules, list):
        raise ValueError("Stage 1 regional rules asset must contain a rules list")
    admitted: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("Stage 1 regional rule is not an object")
        if rule.get("extraction_status") == "supported":
            admitted.append(rule)
    return admitted


def materialize_stage2_assessments(
    rules_asset: dict[str, Any], poi_type_catalog: list[str], llm_outputs: Iterable[dict[str, Any]],
    *, world_id: str, city_version: str, frozen_world_hash: str,
) -> dict[str, Any]:
    """Persist one combined Stage-2 judgement per structured source rule."""
    outputs = list(llm_outputs)
    rules = stage2_source_rules(rules_asset)
    if len(outputs) != len(rules):
        raise ValueError("Stage 2 output count does not match source-supported Stage 1 rules")
    index_to_type = {index: value for index, value in enumerate(poi_type_catalog, start=1)}
    entries: list[dict[str, Any]] = []
    required = ("regional_fact", "has_applicability_scope", "can_distinguish_real_choices")
    for rule, output in zip(rules, outputs, strict=True):
        _validate_stage2_assessment_output(output, len(poi_type_catalog))
        rule_id = rule["rule_id"]
        atomicity = output["atomicity"]
        checks = output.get("checks")
        indexes = output.get("affected_poi_type_indexes") or []
        retrieval_phrases = output.get("poi_retrieval_phrases") or []
        if atomicity == "needs_split":
            status = "needs_stage1_split"
        elif not all(checks[key] for key in required):
            status = "source_only"
        elif output["task_source_eligible"]:
            status = "task_source_eligible"
        else:
            status = "not_bindable_in_current_map"
        entries.append({
            "rule_id": rule_id,
            "atomicity": atomicity,
            **({"independent_restrictions": list(output["independent_restrictions"])}
               if atomicity == "needs_split" else {}),
            "checks": ({key: checks[key] for key in required} if checks is not None else None),
            "status": status,
            "reason": str(output.get("reason") or ""),
            # Indexes are the model's constrained return contract.  The
            # readable type names below are a script-derived audit projection.
            "affected_poi_type_indexes": sorted(set(indexes)),
            "affected_poi_primary_types": sorted({
                index_to_type[index] for index in indexes
            }),
            "poi_retrieval_phrases": list(dict.fromkeys(retrieval_phrases)),
            "task_source_eligible": output.get("task_source_eligible"),
            "human_review": default_stage2_human_review(),
        })
    return {
        "schema_version": "regional_rule_stage2_assessments.v4",
        "world_id": world_id,
        "city_id": rules_asset.get("city_id"),
        "city_version": city_version,
        "frozen_world_hash": frozen_world_hash,
        "source_page_hash": rules_asset.get("source_page_hash"),
        "regional_rules_hash": _asset_hash(rules_asset),
        "poi_type_catalog": poi_type_catalog,
        "poi_type_catalog_hash": _asset_hash(poi_type_catalog),
        "stage1_rule_count": len(rules_asset.get("rules") or []),
        "stage2_input_rule_count": len(rules),
        "excluded_stage1_status_counts": {
            status: sum(
                1 for rule in rules_asset.get("rules") or []
                if isinstance(rule, dict) and str(rule.get("extraction_status") or "") == status
            )
            for status in ("ambiguous", "incomplete")
        },
        "assessments": entries,
    }


def merge_stage2_assessment_entries(
    rules_asset: dict[str, Any], poi_type_catalog: list[str],
    existing_asset: dict[str, Any] | None, new_entries: Iterable[dict[str, Any]],
    *, world_id: str, city_version: str, frozen_world_hash: str,
) -> dict[str, Any]:
    """Merge unchanged reviewed rows with newly evaluated rules by rule ID."""
    existing_rows = {
        str(row.get("rule_id") or ""): json.loads(json.dumps(row, ensure_ascii=False))
        for row in ((existing_asset or {}).get("assessments") or []) if isinstance(row, dict)
    }
    replacement_rows = {
        str(row.get("rule_id") or ""): json.loads(json.dumps(row, ensure_ascii=False))
        for row in new_entries if isinstance(row, dict)
    }
    ordered: list[dict[str, Any]] = []
    for rule in stage2_source_rules(rules_asset):
        rule_id = str(rule.get("rule_id") or "")
        row = replacement_rows.get(rule_id) or existing_rows.get(rule_id)
        if row is None:
            raise ValueError(f"Stage 2 merge is missing assessment for {rule_id}")
        if "human_review" not in row:
            row["human_review"] = default_stage2_human_review()
        ordered.append(row)
    return {
        "schema_version": "regional_rule_stage2_assessments.v4",
        "world_id": world_id,
        "city_id": rules_asset.get("city_id"),
        "city_version": city_version,
        "frozen_world_hash": frozen_world_hash,
        "source_page_hash": rules_asset.get("source_page_hash"),
        "regional_rules_hash": _asset_hash(rules_asset),
        "poi_type_catalog": poi_type_catalog,
        "poi_type_catalog_hash": _asset_hash(poi_type_catalog),
        "stage1_rule_count": len(rules_asset.get("rules") or []),
        "stage2_input_rule_count": len(ordered),
        "excluded_stage1_status_counts": {
            status: sum(
                1 for rule in rules_asset.get("rules") or []
                if isinstance(rule, dict) and str(rule.get("extraction_status") or "") == status
            )
            for status in ("ambiguous", "incomplete")
        },
        "assessments": ordered,
    }


def write_json_atomic(path: str | Path, payload: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Keep the temporary basename independent of the target basename.  Region
    # item identifiers can be long enough that appending ``.tmp`` crosses the
    # legacy Windows MAX_PATH boundary even when the final path itself fits.
    descriptor, temporary_name = tempfile.mkstemp(prefix=".json_", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        for attempt in range(5):
            try:
                temporary.replace(target)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)
    return target


def default_stage2_human_review() -> dict[str, Any]:
    """Return script-owned review metadata; this is never model output."""
    return {"status": "pending", "reviewer": None, "reviewed_at": None}


def add_stage2_human_review_metadata(asset: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Idempotently add review metadata without replacing existing decisions."""
    migrated = json.loads(json.dumps(asset, ensure_ascii=False))
    assessments = migrated.get("assessments")
    if not isinstance(assessments, list):
        raise ValueError("Stage 2 asset must contain an assessments list")
    changed = 0
    for assessment in assessments:
        if not isinstance(assessment, dict):
            raise ValueError("Stage 2 assessment must be an object")
        if "human_review" not in assessment:
            assessment["human_review"] = default_stage2_human_review()
            changed += 1
    if changed:
        migrated["schema_version"] = "regional_rule_stage2_assessments.v4"
    return migrated, changed


def _parse_target_sections(raw_wikitext: str, city_id: str) -> list[dict[str, Any]]:
    """Segment the complete city page while retaining all raw Wikitext upstream."""
    return _parse_all_sections(raw_wikitext, city_id)


def _parse_all_sections(raw_wikitext: str, city_id: str) -> list[dict[str, Any]]:
    """Losslessly segment every city-page section with balanced template records.

    This parser deliberately works on Wikitext structure rather than treating a
    template parameter continuation (``| name=...``) as a prose paragraph.
    """
    heading_stack: dict[int, str] = {}
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = {
        "section_id": "lead_01", "section_path": ["Lead"], "order": 1, "blocks": [],
    }
    sections.append(current)
    body: list[str] = []

    def flush_section() -> None:
        nonlocal body
        if current is not None:
            _append_structural_blocks(current, body, city_id)
        body = []

    for raw_line in raw_wikitext.splitlines():
        heading = _HEADING_RE.match(raw_line)
        if not heading:
            if current is not None:
                body.append(raw_line)
            continue
        flush_section()
        level = len(heading.group(1))
        heading_stack[level] = heading.group(2).strip()
        for stale in [key for key in heading_stack if key > level]:
            del heading_stack[stale]
        ordered_path = [heading_stack[key] for key in sorted(heading_stack)]
        section_id = _slug(".".join(ordered_path)) or "section"
        current = {
            "section_id": f"{section_id}_{len(sections) + 1:02d}",
            "section_path": ordered_path,
            "order": len(sections) + 1,
            "blocks": [],
        }
        sections.append(current)
    flush_section()
    return [section for section in sections if section["blocks"]]


def _append_structural_blocks(section: dict[str, Any], lines: list[str], city_id: str) -> None:
    """Append paragraphs, lists, tables and balanced template records for one section."""
    evidence_number = sum(1 for block in section["blocks"] if "evidence_id" in block)

    media_number = 0
    emitted_media: set[tuple[str, str]] = set()

    def add(block_type: str, group: str, text: str) -> None:
        nonlocal evidence_number
        evidence_number += 1
        _add_block(section, f"wv_{city_id}_{section['order']:02d}_{evidence_number:04d}", block_type, group, text)

    def add_media(file_reference: str, caption: str, source_text: str, group: str) -> None:
        nonlocal media_number
        key = (file_reference.casefold(), source_text)
        if not file_reference or key in emitted_media:
            return
        emitted_media.add(key)
        media_number += 1
        media_id = f"wv_{city_id}_media_{section['order']:02d}_{media_number:04d}"
        section["blocks"].append({
            "evidence_id": media_id,
            "media_id": media_id,
            "block_type": "media",
            "semantic_group_id": f"{group}.media_{media_number:04d}",
            "order": len(section["blocks"]) + 1,
            "source_text": source_text,
            "file_reference": file_reference.strip(),
            "caption_source_text": caption.strip(),
            "local_asset_path": None,
            "media_status": "pending_visual_description",
        })

    def add_inline_media(line: str, group: str) -> str:
        for match in _MEDIA_RE.finditer(line):
            options = [part.strip() for part in (match.group(2) or "").split("|") if part.strip()]
            add_media(match.group(1), options[-1] if options else "", match.group(0), group)
        return _MEDIA_RE.sub("", line).strip()

    def add_template_media(text: str, group: str) -> None:
        for match in re.finditer(r"(?im)^\s*\|\s*(?:image\d*|photo\d*|logo)\s*=\s*(?:File:|Image:)?([^|\n]+)", text):
            file_reference = match.group(1).strip()
            if file_reference:
                caption_match = re.search(r"(?im)^\s*\|\s*(?:caption|image_caption)\s*=\s*([^|\n]+)", text)
                caption = caption_match.group(1).strip() if caption_match else ""
                add_media(file_reference, caption, match.group(0), group)

    paragraph: list[str] = []
    list_lines: list[str] = []
    record: list[str] = []
    record_depth = 0
    container_depth = 0
    container_source: list[str] = []
    container_group = ""
    table_lines: list[str] = []
    in_table = False
    gallery_lines: list[str] = []
    in_gallery = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            add("paragraph", f"{section['section_id']}.paragraph_{len(section['blocks']) + 1:04d}", "\n".join(paragraph))
        paragraph = []

    def flush_list() -> None:
        nonlocal list_lines
        if list_lines:
            group = f"{section['section_id']}.list_{len(section['blocks']) + 1:04d}"
            for item in list_lines:
                add("list_item", group, item)
        list_lines = []

    def flush_table() -> None:
        nonlocal table_lines, in_table
        if table_lines:
            group = f"{section['section_id']}.table_{len(section['blocks']) + 1:04d}"
            text = "\n".join(table_lines)
            add("table", group, text)
            add_inline_media(text, group)
        table_lines = []
        in_table = False

    def flush_gallery() -> None:
        nonlocal gallery_lines, in_gallery
        group = f"{section['section_id']}.gallery_{len(section['blocks']) + 1:04d}"
        for gallery_line in gallery_lines:
            match = _GALLERY_FILE_RE.match(gallery_line)
            if match:
                add_media(match.group(1), match.group(2) or "", gallery_line, group)
        gallery_lines = []
        in_gallery = False

    def brace_delta(value: str) -> int:
        return value.count("{{") - value.count("}}")

    for line in lines:
        if in_gallery:
            if re.match(r"^\s*</gallery\s*>\s*$", line, re.IGNORECASE):
                flush_gallery()
            else:
                gallery_lines.append(line)
            continue
        if in_table:
            table_lines.append(line)
            if line.lstrip().startswith("|}"):
                flush_table()
            continue
        if record:
            record.append(line)
            record_depth += brace_delta(line)
            if record_depth <= container_depth:
                group = f"{section['section_id']}.template_{len(section['blocks']) + 1:04d}"
                text = "\n".join(record)
                add("template_record", group, text)
                add_template_media(text, group)
                record, record_depth = [], 0
            continue
        if line.lstrip().startswith("{|"):
            flush_paragraph(); flush_list()
            in_table = True
            table_lines = [line]
            continue
        if re.match(r"^\s*<gallery(?:\s|>)", line, re.IGNORECASE):
            flush_paragraph(); flush_list()
            in_gallery = True
            gallery_lines = []
            continue
        if container_depth:
            container_source.append(line)
        if container_depth and _LIST_RE.match(line) and "{{" in line:
            flush_paragraph(); flush_list()
            record, record_depth = [line], container_depth + brace_delta(line)
            continue
        if not line.strip():
            flush_paragraph(); flush_list()
            continue
        if _MEDIA_RE.search(line):
            flush_paragraph(); flush_list()
            group = f"{section['section_id']}.media_inline_{len(section['blocks']) + 1:04d}"
            residual = add_inline_media(line, group)
            if residual:
                paragraph.append(residual)
            continue
        if _DISPLAY_CONTROL_TEMPLATE_RE.match(line):
            flush_paragraph(); flush_list()
            group = f"{section['section_id']}.display_control_{len(section['blocks']) + 1:04d}"
            add("display_control", group, line)
            continue
        if _LISTING_TEMPLATE_RE.match(line):
            flush_paragraph(); flush_list()
            group = f"{section['section_id']}.template_{len(section['blocks']) + 1:04d}"
            if brace_delta(line) <= 0:
                add("template_record", group, line)
                add_template_media(line, group)
            else:
                record, record_depth = [line], brace_delta(line)
            continue
        if line.startswith("{{") and brace_delta(line) > 0:
            flush_paragraph(); flush_list()
            container_depth += brace_delta(line)
            group = f"{section['section_id']}.container_{len(section['blocks']) + 1:04d}"
            container_source = [line]
            container_group = group
            add("template_container", group, line)
            continue
        if container_depth and line.strip().endswith("}}"):
            container_depth += brace_delta(line)
            group = f"{section['section_id']}.container_{len(section['blocks']) + 1:04d}"
            add("template_container_close", group, line)
            if container_depth <= 0:
                add_template_media("\n".join(container_source), container_group or group)
                container_source = []
                container_group = ""
            continue
        if _LIST_RE.match(line):
            flush_paragraph(); list_lines.append(line)
            continue
        flush_list(); paragraph.append(line)
    flush_paragraph(); flush_list(); flush_table(); flush_gallery()
    return


def _add_block(section: dict[str, Any], evidence_id: str, block_type: str, group_id: str, source_text: str) -> None:
    section["blocks"].append({
        "evidence_id": evidence_id,
        "block_type": block_type,
        "semantic_group_id": group_id,
        "order": len(section["blocks"]) + 1,
        "source_text": source_text,
    })


def _semantic_groups(blocks: Iterable[dict[str, Any]]) -> Iterable[list[dict[str, Any]]]:
    current_group_id = None
    current: list[dict[str, Any]] = []
    for block in blocks:
        group_id = block["semantic_group_id"]
        if current and group_id != current_group_id:
            yield current
            current = []
        current_group_id = group_id
        current.append(block)
    if current:
        yield current


def _is_processable_source_block(block: dict[str, Any]) -> bool:
    return block.get("block_type") in {"paragraph", "list_item", "template_record", "table", "media"}


def _contextualization_input_item(
    block: dict[str, Any], index: int, media_descriptions: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if block.get("block_type") != "media":
        return {"index": index, "source_kind": "wikitext", "text": block["source_text"]}
    media_id = str(block.get("media_id") or "")
    media = media_descriptions.get(media_id)
    if not isinstance(media, dict):
        raise ValueError(f"Missing visual description for contextualization item: {media_id}")
    return {
        "index": index,
        "source_kind": "image",
        "original_wikitext": block["source_text"],
        "caption_source_text": media.get("caption_source_text", ""),
        "visible_description": media.get("visible_description", ""),
        "has_unresolved_visual_ambiguity": media.get("status") != "supported",
    }


def _source_char_count(blocks: Iterable[dict[str, Any]]) -> int:
    return sum(len(str(block.get("source_text") or "")) for block in blocks)


def _split_list_request_units(
    blocks: list[dict[str, Any]], max_source_chars: int, max_list_items: int,
    max_atomic_source_chars: int,
) -> list[list[dict[str, Any]]]:
    """Split only between complete ordered list items, never by raw characters."""
    units: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for block in blocks:
        block_chars = _source_char_count([block])
        if block_chars > max_atomic_source_chars:
            raise ValueError(
                "Oversize atomic list item exceeds the configured explicit review limit: "
                f"{block.get('evidence_id')} ({block_chars} chars > {max_atomic_source_chars})"
            )
        if block_chars > max_source_chars:
            if current:
                units.append(current)
                current, current_chars = [], 0
            units.append([block])
            continue
        exceeds_size = current and current_chars + block_chars > max_source_chars
        exceeds_count = current and len(current) >= max_list_items
        if exceeds_size or exceeds_count:
            units.append(current)
            current, current_chars = [], 0
        current.append(block)
        current_chars += block_chars
    if current:
        units.append(current)
    return units


def _split_nonlist_request_units(
    blocks: list[dict[str, Any]], max_source_chars: int, max_items: int,
    max_atomic_source_chars: int,
) -> list[list[dict[str, Any]]]:
    """Pack consecutive ordinary source items, without splitting an original item."""
    units: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for block in blocks:
        block_chars = _source_char_count([block])
        if block_chars > max_atomic_source_chars:
            raise ValueError(
                "Oversize atomic source item exceeds the configured explicit review limit: "
                f"{block.get('evidence_id')} ({block_chars} chars > {max_atomic_source_chars})"
            )
        if block_chars > max_source_chars:
            if current:
                units.append(current)
                current, current_chars = [], 0
            units.append([block])
            continue
        if current and (current_chars + block_chars > max_source_chars or len(current) >= max_items):
            units.append(current)
            current, current_chars = [], 0
        current.append(block)
        current_chars += block_chars
    if current:
        units.append(current)
    return units


def _source_group_positions(blocks: Iterable[dict[str, Any]]) -> dict[str, tuple[str, int]]:
    """Map each evidence item to its immutable provenance group and ordinal."""
    positions: dict[str, tuple[str, int]] = {}
    counts: dict[str, int] = {}
    for block in blocks:
        evidence_id = block.get("evidence_id")
        group_id = block.get("semantic_group_id")
        if not evidence_id or not group_id:
            continue
        counts[group_id] = counts.get(group_id, 0) + 1
        positions[evidence_id] = (group_id, counts[group_id])
    return positions


def _make_chunk(
    snapshot: dict[str, Any], section: dict[str, Any], blocks: list[dict[str, Any]], sequence: int,
    group_positions: dict[str, tuple[str, int]],
) -> dict[str, Any]:
    provenance = [
        {
            "evidence_id": block["evidence_id"],
            "source_group_id": group_positions[block["evidence_id"]][0],
            "source_group_item_position": group_positions[block["evidence_id"]][1],
        }
        for block in blocks
    ]
    group_ids = list(dict.fromkeys(item["source_group_id"] for item in provenance))
    group_id = group_ids[0] if len(group_ids) == 1 else None
    chunk = {
        "chunk_id": f"{section['section_id']}.unit_{sequence:03d}",
        "parser_version": snapshot["parser_version"],
        "section_path": list(section["section_path"]),
        "source_group_id": group_id,
        "source_group_ids": group_ids,
        "source_group_item_positions": [item["source_group_item_position"] for item in provenance] if group_id else [],
        "source_item_provenance": provenance,
        "blocks": blocks,
        "source_characters": _source_char_count(blocks),
    }
    chunk["chunk_plan_hash"] = _fingerprint(_canonical_json({
        "parser_version": chunk["parser_version"],
        "source_page_hash": snapshot["source"]["page_hash"],
        "section_path": chunk["section_path"],
        "source_group_ids": group_ids,
        "evidence_ids": [block["evidence_id"] for block in blocks],
    }))
    return chunk


def _model_source_context(snapshot: dict[str, Any], chunk: dict[str, Any]) -> str:
    page_title = snapshot["source"]["page_title"]
    path = " > ".join(chunk["section_path"])
    if all(block["block_type"] == "list_item" for block in chunk["blocks"]):
        positions = chunk["source_group_item_positions"]
        return (
            f"Wikivoyage page: {page_title}. Section: {path}. "
            f"These are consecutive entries {positions[0]} through {positions[-1]} from one continuous source list."
        )
    return f"Wikivoyage page: {page_title}. Section: {path}."


def validate_llm_output(request: dict[str, Any], output: dict[str, Any]) -> None:
    """Validate a model response before it is cached, enabling protocol repair."""
    contract = request.get("validation") or {}
    kind = contract.get("kind")
    item_count = int(contract.get("item_count") or 0)
    if kind == "contextualization":
        _validate_contextualization_output(output, item_count)
    elif kind == "media_description":
        _validate_media_description_output(output)
    elif kind == "rule_extraction":
        _validate_rule_extraction_output(output, item_count)
    elif kind == "stage1_extraction":
        _validate_stage1_extraction_output(output, item_count)
    elif kind == "stage2_assessment":
        _validate_stage2_assessment_output(
            output, len(contract.get("allowed_poi_type_indexes", [])),
        )


def _validate_media_description_output(output: dict[str, Any]) -> None:
    description = output.get("visible_description")
    ambiguity = output.get("has_unresolved_ambiguity")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Media description must contain visible_description")
    if len(description) > 1200:
        raise ValueError("Media description exceeds the 1200-character source limit")
    if not isinstance(ambiguity, bool):
        raise ValueError("Media description must contain has_unresolved_ambiguity")


def _validate_contextualization_output(output: dict[str, Any], item_count: int) -> None:
    items = output.get("items")
    if not isinstance(items, list) or len(items) != item_count:
        raise ValueError(f"Contextualization must return exactly {item_count} items")
    expected = set(range(1, item_count + 1))
    actual: set[int] = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("index"), int):
            raise ValueError("Contextualization item must include an integer index")
        index = item["index"]
        text = item.get("contextualized_text")
        ambiguity = item.get("has_unresolved_ambiguity")
        if not isinstance(text, str) or not text.strip() or not isinstance(ambiguity, bool):
            raise ValueError(f"Contextualization item {index} is incomplete")
        actual.add(index)
    if actual != expected:
        raise ValueError(f"Contextualization indexes must be {sorted(expected)}, received {sorted(actual)}")
    if [item["index"] for item in items] != list(range(1, item_count + 1)):
        raise ValueError("Contextualization items must preserve the supplied order")


def _validate_rule_extraction_output(output: dict[str, Any], item_count: int) -> None:
    candidates = output.get("rule_candidates")
    if not isinstance(candidates, list):
        raise ValueError("Rule extraction must return a rule_candidates list")
    valid_indexes = set(range(1, item_count + 1))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Rule candidate must be an object")
        if not all(isinstance(candidate.get(key), str) and candidate[key].strip()
                   for key in ("rule_category", "subject", "summary")):
            raise ValueError("Rule candidate is missing a category, subject, or summary")
        if candidate.get("extraction_status") not in {"supported", "ambiguous", "incomplete"}:
            raise ValueError("Rule candidate has an invalid extraction status")
        if not isinstance(candidate.get("scope"), dict):
            raise ValueError("Rule candidate must include a scope object")
        clauses = candidate.get("clauses")
        if not isinstance(clauses, list) or not clauses:
            raise ValueError("Rule candidate must include clauses")
        if sum(clause.get("role") == "base_rule" for clause in clauses if isinstance(clause, dict)) != 1:
            raise ValueError("Every rule candidate must contain exactly one base_rule")
        for clause in clauses:
            if not isinstance(clause, dict) or clause.get("role") not in {"base_rule", "condition", "exception", "qualification"}:
                raise ValueError("Rule clause has an invalid role")
            if not isinstance(clause.get("statement"), str) or not clause["statement"].strip():
                raise ValueError("Rule clause is missing a statement")
            indexes = clause.get("item_indexes") if isinstance(clause, dict) else None
            if not isinstance(indexes, list) or not indexes or any(not isinstance(index, int) for index in indexes):
                raise ValueError("Every rule clause must cite local item indexes")
            if not set(indexes) <= valid_indexes:
                raise ValueError("Rule clause cites an item outside this request")


def _validate_stage1_extraction_output(output: dict[str, Any], item_count: int) -> None:
    candidates = output.get("rule_candidates")
    if not isinstance(candidates, list):
        raise ValueError("Stage 1 extraction must return a rule_candidates list")
    if set(output) != {"rule_candidates"}:
        raise ValueError("Stage 1 extraction returned fields outside rule_candidates")
    valid_indexes = set(range(1, item_count + 1))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Stage 1 rule candidate must be an object")
        expected_fields = {"statement", "source_item_indexes", "extraction_status"}
        if set(candidate) != expected_fields:
            raise ValueError("Stage 1 rule candidate returned an invalid field set")
        if not isinstance(candidate.get("statement"), str) or not candidate["statement"].strip():
            raise ValueError("Stage 1 rule candidate must contain a non-empty statement")
        indexes = candidate.get("source_item_indexes")
        if not isinstance(indexes, list) or not indexes or any(not isinstance(index, int) for index in indexes):
            raise ValueError("Stage 1 rule candidate must cite one or more integer source item indexes")
        if not set(indexes) <= valid_indexes:
            raise ValueError("Stage 1 rule candidate cites a source item outside this request")
        if candidate.get("extraction_status") not in {"supported", "ambiguous", "incomplete"}:
            raise ValueError("Stage 1 rule candidate has an invalid extraction status")


def _validate_stage2_assessment_output(output: dict[str, Any], catalog_size: int) -> None:
    """Validate the one-call Stage-2 decision and its closed type selection."""
    atomicity = output.get("atomicity")
    if atomicity not in {"atomic", "needs_split"}:
        raise ValueError("Stage 2 response must contain a valid atomicity status")
    if not isinstance(output.get("reason"), str):
        raise ValueError("Stage 2 response must contain a reason")
    if atomicity == "needs_split":
        expected = {"atomicity", "independent_restrictions", "reason"}
        if set(output) != expected:
            raise ValueError("A needs_split Stage 2 response must contain only atomicity, independent_restrictions, and reason")
        restrictions = output.get("independent_restrictions")
        if (not isinstance(restrictions, list) or len(restrictions) < 2
                or any(not isinstance(value, str) or not value.strip() for value in restrictions)):
            raise ValueError("A needs_split Stage 2 response must contain at least two complete restrictions")
        return
    expected = {
        "atomicity", "checks", "affected_poi_type_indexes",
        "poi_retrieval_phrases", "task_source_eligible", "reason",
    }
    if set(output) != expected:
        raise ValueError(f"An atomic Stage 2 response must contain exactly: {sorted(expected)}")
    eligible = output.get("task_source_eligible")
    checks = output.get("checks")
    required = ("regional_fact", "has_applicability_scope", "can_distinguish_real_choices")
    indexes = output.get("affected_poi_type_indexes")
    phrases = output.get("poi_retrieval_phrases")
    if not isinstance(checks, dict) or any(not isinstance(checks.get(key), bool) for key in required):
        raise ValueError("An atomic Stage 2 response must contain three boolean checks")
    if set(checks) != set(required):
        raise ValueError("An atomic Stage 2 response returned unexpected checks")
    if not isinstance(indexes, list) or any(not isinstance(value, int) for value in indexes):
        raise ValueError("Stage 2 response must contain an integer type-index list")
    if any(index < 1 or index > catalog_size for index in indexes):
        raise ValueError("Stage 2 response returned an index outside the frozen map catalog")
    if not isinstance(phrases, list) or any(not isinstance(value, str) or not value.strip() for value in phrases):
        raise ValueError("Stage 2 response must contain a non-empty-string retrieval phrase list")
    if not isinstance(eligible, bool):
        raise ValueError("An atomic Stage 2 response must contain task_source_eligible")
    derived = all(checks[key] for key in required) and bool(indexes)
    if eligible != derived:
        raise ValueError("Stage 2 eligibility must agree with its checks and type-index list")
    if not eligible and indexes:
        raise ValueError("An ineligible Stage 2 response must return no POI type indexes")


def _materialize_rule_candidate(
    city_id: str, candidate: dict[str, Any], blocks: list[dict[str, Any]], valid_refs: set[str | None],
) -> dict[str, Any]:
    translated = {
        "statement": candidate.get("statement"),
        "evidence_refs": [
            blocks[index - 1]["evidence_id"]
            for index in candidate.get("source_item_indexes", [])
        ],
        "extraction_status": candidate.get("extraction_status"),
    }
    return _validate_rule_candidate(city_id, translated, valid_refs)


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


def _list_context_prefix(page_title: str, section_path: list[str]) -> str:
    return f"In {page_title}, under {' > '.join(section_path)}, the source lists:"


def _validate_rule_candidate(city_id: str, candidate: dict[str, Any], valid_refs: set[str | None]) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise ValueError("Rule candidate must be an object")
    statement = str(candidate.get("statement") or "").strip()
    status = str(candidate.get("extraction_status") or "")
    refs = [str(ref) for ref in candidate.get("evidence_refs", [])]
    if not statement:
        raise ValueError("Rule candidate is missing its complete statement")
    if status not in {"supported", "ambiguous", "incomplete"}:
        raise ValueError("Rule candidate has an invalid extraction status")
    if not refs or not set(refs) <= valid_refs:
        raise ValueError("Rule candidate references unknown source evidence")
    return {
        "statement": statement,
        "evidence_refs": sorted(set(refs)),
        "extraction_status": status,
    }


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _asset_hash(value: Any) -> str:
    return _fingerprint(_canonical_json(value))
