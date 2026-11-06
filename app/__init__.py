"""App registry — all 7 benchmark apps.

Pattern follows ARE (Meta Agents Research Environments):
  - ALL_APPS: flat list of all app specs
  - APP_REGISTRY: name → AppSpec dict for O(1) lookup
  - Helper functions for querying by name/function
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.user_interface import APP_USER_INTERFACE
from app.local_service import APP_LOCAL_SERVICE
from app.map_app import APP_MAP
from app.messaging import APP_MESSAGING
from app.weather import APP_WEATHER
from app.calendar import APP_CALENDAR
from app.city_news import APP_CITY_NEWS

if TYPE_CHECKING:
    from app.base import AppSpec, AppFunction

ALL_APPS: list["AppSpec"] = [
    APP_USER_INTERFACE,
    APP_LOCAL_SERVICE,
    APP_MAP,
    APP_MESSAGING,
    APP_WEATHER,
    APP_CALENDAR,
    APP_CITY_NEWS,
]

APP_REGISTRY: dict[str, "AppSpec"] = {app.name: app for app in ALL_APPS}


def get_app(name: str) -> "AppSpec":
    """Get an AppSpec by canonical name. Raises KeyError if not found."""
    if name not in APP_REGISTRY:
        raise KeyError(f"App {name!r} not found. Available: {list_apps()}")
    return APP_REGISTRY[name]


def get_function(app_name: str, func_name: str) -> "AppFunction":
    """Get a specific function from a specific app. Raises if not found."""
    app = get_app(app_name)
    func = app.get_function(func_name)
    if func is None:
        raise KeyError(
            f"Function {func_name!r} not found in {app_name}. "
            f"Available: {app.list_functions()}"
        )
    return func


def list_apps() -> list[str]:
    """List all registered app names."""
    return list(APP_REGISTRY.keys())


def list_functions(app_name: str) -> list[str]:
    """List all function names for an app."""
    return get_app(app_name).list_functions()


def list_all_functions() -> dict[str, list[str]]:
    """Return {app_name: [func_names]} for all apps."""
    return {name: list_functions(name) for name in APP_REGISTRY}


def search_functions(oracle_action: str | None = None, access: str | None = None) -> list[tuple[str, str]]:
    """Search functions across all apps by oracle_action_type and/or access.

    Returns list of (app_name, function_name) tuples.
    """
    results: list[tuple[str, str]] = []
    for app in ALL_APPS:
        for func in app.functions:
            match = True
            if oracle_action and func.oracle_action_type != oracle_action:
                match = False
            if access and func.access != access:
                match = False
            if match:
                results.append((app.name, func.name))
    return results
