"""Task types — 8 benchmark task type generators.

Each type extends BaseTaskGenerator from _shared/.
See task_module/README.md for the full task type catalog.
"""

from task_module.task_types.registry import get_task_generator

__all__ = ["get_task_generator"]
