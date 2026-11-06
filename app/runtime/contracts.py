from __future__ import annotations
from pathlib import Path
from typing import Any
from shared.io import read_json
'Public App/Tool contracts for formal benchmark execution.\n\nThe contract defines the observable environment, not a solution strategy.  It\ncontains no city binding, candidate identity, provenance, or judge data.\n'
CONTRACT_SCHEMA_VERSION = 'geoword.agent_environment.v2'
LEGACY_USER_INTERFACE_APP = 'AgentUserInterface'
USER_INTERFACE_APP = 'UserInterfaceApp'
STANDARD_APPLICATIONS: dict[str, str] = {'MapApp': 'Search public places, inspect place details, and query routes.', 'LocalServiceApp': 'Inspect source-backed merchant/service details, availability, and reservations for places already identified in MapApp.', 'CalendarApp': 'Query weekdays, recorded festivals, public holidays and makeup workdays, and inspect participant availability and scheduling conflicts.', 'MessagingApp': 'Inspect contacts and send messages.', 'WeatherApp': 'Query published weather events and clearance notices; no routine weather or forecasts.', 'CityNewsApp': 'Inspect public local notices, events, and city information.', USER_INTERFACE_APP: 'Submit plans and final responses through the user-facing interface.'}

def normalize_application_catalog(applications: dict[str, str]) -> dict[str, str]:
    """Map the legacy public UI alias to the canonical App name."""
    normalized = dict(applications)
    legacy_description = normalized.pop(LEGACY_USER_INTERFACE_APP, None)
    if legacy_description is not None:
        normalized.setdefault(USER_INTERFACE_APP, legacy_description)
    return normalized

def load_agent_environment(task_dir: Path, task_type: str) -> dict[str, Any]:
    """Load a scenario environment without deriving App availability from a task type."""
    path = task_dir / 'agent_environment.json'
    if path.is_file():
        value = read_json(path)
        if _valid_contract(value):
            normalized = dict(value)
            normalized['applications'] = normalize_application_catalog(value['applications'])
            return normalized
        raise ValueError(f'Invalid agent_environment.json for {task_dir}')
    return {'schema_version': CONTRACT_SCHEMA_VERSION, 'source': 'standard_world_environment', 'applications': dict(STANDARD_APPLICATIONS), 'limits': {}}

def _valid_contract(value: Any) -> bool:
    if not isinstance(value, dict) or value.get('schema_version') != CONTRACT_SCHEMA_VERSION:
        return False
    applications = value.get('applications')
    if not isinstance(applications, dict) or not applications:
        return False
    return all((isinstance(name, str) and isinstance(description, str) for (name, description) in applications.items()))
