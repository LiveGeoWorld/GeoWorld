"""Pipeline configuration loader.

config.yaml structure (v2 — with model endpoint registry):

  google_maps:
    api_key: "..."

  model_endpoints:           # named model connections
    qwen_vl:
      provider: openai_compatible
      model: qwen-vl-max
      base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
      api_key: sk-xxx
      supports_vision: true
    qwen_default:
      provider: openai_compatible
      model: qwen-plus
      ...

  stage_models:              # stage → endpoint name
    vision_parse:   qwen_vl
    task_design:    qwen_default
    quality_llm:    qwen_default
    query_rewrite:  qwen_strong
    query_verify:   qwen_default

Backward compat: old config.yaml with flat dashscope + models blocks
is still readable via legacy properties (dashscope_api_key, model_text, etc.).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import yaml

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

# Default stage → endpoint fallback when stage_models is absent from config
_DEFAULT_STAGE_MODELS = {
    # v4 stages
    "vision_parse":   "claude_sonnet",
    "task_create":    "claude_sonnet",
    "task_judge":     "claude_sonnet",
    # v3 legacy stages (kept for backward compatibility)
    "task_design":    "qwen38_max",
    "quality_llm":    "qwen38_max",
    "query_rewrite":  "qwen38_max",
    "query_verify":   "qwen38_max",
    # v5 scenario generation
    "scenario_patch": "cliproxy_gpt55_pro",
}


class PipelineConfig:
    """Typed wrapper around config.yaml."""

    def __init__(self, path: Path | str = _DEFAULT_CONFIG_PATH):
        self._path = Path(path)
        with open(self._path, encoding="utf-8") as f:
            self._raw: Dict[str, Any] = yaml.safe_load(f)

    # ── Google Maps ──────────────────────────────────────────────────────────

    @property
    def google_maps_api_key(self) -> str:
        return self._raw["google_maps"]["api_key"]

    # ── Model endpoint registry ──────────────────────────────────────────────

    def model_endpoint(self, name: str) -> Dict[str, Any]:
        """Return the raw endpoint config dict for a named endpoint.

        Raises KeyError with a helpful message if not found.
        """
        endpoints = self._raw.get("model_endpoints", {})
        if name not in endpoints:
            available = list(endpoints.keys())
            raise KeyError(
                f"Model endpoint {name!r} not found in config.yaml. "
                f"Available endpoints: {available}"
            )
        return endpoints[name]

    def stage_model(self, stage: str) -> str:
        """Resolve an endpoint key or return the endpoint assigned to a stage.

        Direct endpoint keys let interactive task construction select an
        explicitly registered model, while stage keys preserve the historical
        pipeline defaults. Falls back to _DEFAULT_STAGE_MODELS if the
        stage_models block is absent.
        """
        endpoints = self._raw.get("model_endpoints", {})
        if stage in endpoints:
            return stage
        stage_map = self._raw.get("stage_models", {})
        if stage in stage_map:
            return stage_map[stage]
        if stage in _DEFAULT_STAGE_MODELS:
            return _DEFAULT_STAGE_MODELS[stage]
        raise KeyError(
            f"Stage {stage!r} has no assigned model endpoint. "
            "Add it to config.yaml → stage_models."
        )

    def all_stage_models(self) -> Dict[str, str]:
        """Return the complete stage → endpoint mapping (merged with defaults)."""
        merged = dict(_DEFAULT_STAGE_MODELS)
        merged.update(self._raw.get("stage_models", {}))
        return merged

    # ── Legacy / backward-compat properties ─────────────────────────────────
    # These read the old config format (flat dashscope + models blocks).
    # Kept so DashScopeClient and existing code keep working.

    @property
    def dashscope_api_key(self) -> str:
        # New format: first dashscope-provider endpoint's api_key
        for cfg in self._raw.get("model_endpoints", {}).values():
            if cfg.get("provider", "openai_compatible") == "openai_compatible":
                return cfg["api_key"]
        # Old format fallback
        return self._raw["dashscope"]["api_key"]

    @property
    def dashscope_base_url(self) -> str:
        for cfg in self._raw.get("model_endpoints", {}).values():
            if cfg.get("provider", "openai_compatible") == "openai_compatible":
                return cfg.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        return self._raw.get("dashscope", {}).get(
            "base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )

    @property
    def model_vision(self) -> str:
        try:
            ep = self.model_endpoint(self.stage_model("vision_parse"))
            return ep["model"]
        except (KeyError, Exception):
            return self._raw.get("models", {}).get("vision", "qwen-vl-max")

    @property
    def model_text(self) -> str:
        try:
            ep = self.model_endpoint(self.stage_model("task_design"))
            return ep["model"]
        except (KeyError, Exception):
            return self._raw.get("models", {}).get("text", "qwen-plus")

    @property
    def model_text_strong(self) -> str:
        try:
            ep = self.model_endpoint(self.stage_model("query_rewrite"))
            return ep["model"]
        except (KeyError, Exception):
            return self._raw.get("models", {}).get("text_strong", "qwen-max")

    # ── Raw access ───────────────────────────────────────────────────────────

    def get(self, dotted_key: str, default: Any = None) -> Any:
        parts = dotted_key.split(".")
        cur = self._raw
        for p in parts:
            if isinstance(cur, dict):
                cur = cur.get(p)
            else:
                return default
            if cur is None:
                return default
        return cur
