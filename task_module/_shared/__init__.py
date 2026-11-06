"""Shared infrastructure for task generators.

Modules:
  - base_generator: BaseTaskGenerator abstract class
  - sampler: TaskSampler for character/place/event sampling
  - output_writer: Write task folders and manifest
  - prompts: LLM prompt templates
"""

from task_module._shared.base_generator import BaseTaskGenerator
from task_module._shared.sampler import TaskSampler
from task_module._shared.output_writer import write_task_folder
from task_module._shared.prompts import SYSTEM_USER_PROMPT, build_user_prompt_request

__all__ = [
    "BaseTaskGenerator",
    "TaskSampler",
    "write_task_folder",
    "SYSTEM_USER_PROMPT",
    "build_user_prompt_request",
]
