"""Evidence-preserved batch generation for place_grounding tasks.

This package builds draft curated grounding examples from Bench map worlds,
preserving the evidence/provenance needed for human review.
"""

from task_module.task_types.place_grounding.batch_generation.config import BatchGenerationConfig
from task_module.task_types.place_grounding.batch_generation.pipeline import generate_batch

__all__ = ["BatchGenerationConfig", "generate_batch"]
