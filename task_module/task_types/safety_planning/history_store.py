"""Algorithm-scoped generation history; frozen task replay remains independent."""

from pathlib import Path

from shared.io import read_json, write_json
from .evidence_index import SafetyEvidenceIndex
from .task_time import OCCUPANCY_CONTRACTS

STATE_HISTORY_VERSION = "safety_state.v1"
OCCUPANCY_HISTORY_VERSION = "safety_occupancy.v6"


def assert_current_batch(output):
    output = Path(output)
    metadata = output / "batch_metadata.json"
    if metadata.is_file() and read_json(metadata).get("generation_origin") == "legacy_writer_compat":
        raise ValueError("Legacy Writer compatibility batches are read-only for generation; create a new batch")
    binding = output / "safety_batch_binding.json"
    if binding.is_file():
        value = read_json(binding)
        if value.get("algorithm_version") != OCCUPANCY_HISTORY_VERSION or value.get("batch_id") != output.name:
            raise ValueError("Safety batch version mismatch; create a new batch")
    elif (output / "safety_evidence_history.json").exists() or any((output / "tasks").glob("*/task.json")):
        raise ValueError("Old Safety batch is read-only for new generation; create a new batch")


def bind_occupancy_batch(output, *, world_binding):
    output = Path(output)
    assert_current_batch(output)
    path = output / "safety_batch_binding.json"
    expected = {"algorithm_version": OCCUPANCY_HISTORY_VERSION,
                "batch_id": output.name, "world_binding": world_binding}
    if path.exists():
        if read_json(path) != expected:
            raise ValueError("Safety batch identity or algorithm differs; create a new batch")
    else:
        if (output / "safety_evidence_history.json").exists() or any((output / "tasks").glob("*/task.json")):
            raise ValueError("Old Safety batch is read-only for new generation; create a new batch")
        write_json(path, expected)
    return expected


def generation_history_path(output, algorithm_version):
    output = Path(output)
    if algorithm_version == STATE_HISTORY_VERSION:
        return output / "safety_evidence_history.json"
    if algorithm_version in OCCUPANCY_CONTRACTS:
        return output / "algorithm_history" / algorithm_version / "safety_evidence_history.json"
    raise ValueError("Unsupported Safety history algorithm version")


def load_generation_history(output, *, world_binding, algorithm_version):
    path = generation_history_path(output, algorithm_version)
    if not path.is_file():
        index = SafetyEvidenceIndex(world_binding)
        if algorithm_version in OCCUPANCY_CONTRACTS:
            index.generation_binding = {"algorithm_version": algorithm_version, "batch_id": Path(output).name}
        return index
    payload = read_json(path)
    recorded = payload.get("algorithm_version")
    if recorded is None and algorithm_version == STATE_HISTORY_VERSION:
        recorded = STATE_HISTORY_VERSION
    if recorded != algorithm_version:
        raise ValueError("Safety history algorithm version mismatch; no automatic migration")
    if algorithm_version in OCCUPANCY_CONTRACTS and payload.get("batch_id") != Path(output).name:
        raise ValueError("Safety history batch identity mismatch")
    return SafetyEvidenceIndex.from_dict(payload, world_binding=world_binding)


def save_generation_history(output, index, *, algorithm_version):
    path = generation_history_path(output, algorithm_version)
    if algorithm_version in OCCUPANCY_CONTRACTS and any(
            entry["context"].get("protection_contract") != algorithm_version for entry in index.entries):
        raise ValueError("Cannot relabel a Safety history as another algorithm version")
    # Validate an existing destination before replacing it, never relabel it.
    if path.exists():
        load_generation_history(output, world_binding=index.world_binding,
                                algorithm_version=algorithm_version)
    payload = index.to_dict()
    payload["algorithm_version"] = algorithm_version
    if algorithm_version in OCCUPANCY_CONTRACTS:
        payload["batch_id"] = Path(output).name
    write_json(path, payload)
