"""Review must use the task's city version and preserve ordered route evidence."""
from shared.io import write_json
from vis.tasks import workbench_http


def test_route_review_resolves_versioned_legs_and_coordinate_alias(tmp_path, monkeypatch):
    task = tmp_path / "batch" / "tasks" / "task_0001"
    task.mkdir(parents=True)
    write_json(task / "task_provenance.json", {
        "world_id": "world_002", "city_id": "city",
        "generation_context": {"city_version": "v2"},
    })
    cache = tmp_path / "route_cache.json"
    route = {
        "available": True, "duration_s": 360, "distance_m": 500,
        "walking_distance_m": 500, "steps": [
            {"polyline": {"encodedPolyline": "frozen_geometry"}},
        ],
    }
    write_json(cache, {"cache": [
        {"route_id": "leg_2", "mode": "DRIVE", "route": route},
        {"route_id": "leg_1", "mode": "WALK", "route_ref": "leg_1_source"},
        {"route_id": "leg_1_source", "mode": "WALK", "route": route},
    ]})

    def resolve(world, city, version):
        assert (world, city, version) == ("world_002", "city", "v2")
        return cache

    monkeypatch.setattr(workbench_http, "resolve_world_route_cache", resolve)
    sandbox = {"correct_leg_route_ids": ["leg_1", "leg_2"]}
    workbench_http.inject_bound_route_evidence(task, sandbox)
    legs = sandbox["gt_segments"]
    assert [leg["route_id"] for leg in legs] == ["leg_1", "leg_2"]
    assert [leg["mode"] for leg in legs] == ["WALK", "DRIVE"]
    assert legs[0]["route_ref"] == "leg_1_source"
    assert all(leg["route"] == route and leg["polyline_encodings"] == ["frozen_geometry"] for leg in legs)
