"""Compatibility imports from the canonical shared contract."""
from task_module._shared.task_contract import (
    TASK_SCHEMA_VERSION,
    _CJK_RE,
    _HIRAGANA_KATAKANA_RE,
    _ASCII_LETTER_RE,
    _COMMON_RAW_PLACE_TYPES,
    validate_task_folder,
    _validate_legacy_safety_contract,
    _matching_safety_risk_cases,
    _matching_safety_observation_evidence,
    validate_task_language_contract,
    _resolved_prompt_language,
    _infer_prompt_language,
    _mask_known_public_names,
    _world_slice_place_names,
    _validate_dynamic_event_envelope,
    _graph_has_cycle,
)
