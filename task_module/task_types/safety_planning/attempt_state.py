"""Persist attempt reservations before any potentially interrupted preparation."""

from shared.io import read_json, write_json


def last_attempt(output, task_index, attempts):
    path = output / "seed_attempts.json"
    state = read_json(path) if path.is_file() else {}
    values = [int(state.get(str(task_index), {}).get("attempt_index", 0))]
    for record in attempts:
        lineage = record.get("seed_lineage") or {}
        if lineage.get("task_index") == task_index:
            values.append(int(lineage["attempt_index"]))
    return max(values)


def reserve_attempt(output, lineage):
    path = output / "seed_attempts.json"
    state = read_json(path) if path.is_file() else {}
    state[str(lineage["task_index"])] = dict(lineage)
    write_json(path, state)
