from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from shared.io import read_json, write_json


def test_write_json_replaces_complete_document_without_leaving_temp_file(tmp_path) -> None:
    path = tmp_path / "state.json"

    write_json(path, {"status": "running", "progress": {"completed": 3}})

    assert read_json(path) == {"status": "running", "progress": {"completed": 3}}
    assert list(tmp_path.glob(".state.json.*.tmp")) == []


def test_concurrent_writes_never_leave_malformed_json(tmp_path) -> None:
    path = tmp_path / "job.json"

    def write(index: int) -> None:
        write_json(path, {"writer": index, "output": [f"line {index}"] * 10})

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(write, range(40)))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["writer"] in range(40)


def test_concurrent_read_and_write_returns_only_complete_json(tmp_path) -> None:
    path = tmp_path / "job.json"
    write_json(path, {"sequence": 0, "payload": [0] * 20})

    def write(sequence: int) -> None:
        write_json(path, {"sequence": sequence, "payload": [sequence] * 200})

    with ThreadPoolExecutor(max_workers=4) as workers:
        futures = [workers.submit(write, sequence) for sequence in range(1, 21)]
        observed = [read_json(path) for _ in range(80)]
        for future in futures:
            future.result()

    assert all(isinstance(item["sequence"], int) for item in observed)
    assert all(len(item["payload"]) in {20, 200} for item in observed)
