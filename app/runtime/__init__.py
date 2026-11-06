"""Runtime App layer — executable counterparts to the declarative AppSpecs.

``app/*.py`` declares the *contract* (AppSpec). ``app/runtime/*.py`` provides the
*executable* apps the benchmark runner drives: each is a :class:`RuntimeApp` with
its own state, registered tools, and typed event log.

Currently implemented:
  - :class:`MapAppRuntime`           — global, neutral map index
  - :class:`LocalServiceAppRuntime`  — read-only service/merchant information
  - :class:`UserInterfaceAppRuntime` — user <-> agent message channel
"""

from __future__ import annotations

from app.runtime.base import AppEvent, OperationType, RuntimeApp, ToolMeta, app_tool
from app.runtime.local_service import LocalServiceAppRuntime
from app.runtime.map_app import MapAppRuntime
from app.runtime.user_interface import UserInterfaceAppRuntime

__all__ = [
    "AppEvent",
    "OperationType",
    "RuntimeApp",
    "ToolMeta",
    "app_tool",
    "MapAppRuntime",
    "LocalServiceAppRuntime",
    "UserInterfaceAppRuntime",
]
