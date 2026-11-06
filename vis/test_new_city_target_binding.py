from __future__ import annotations

from vis.server import _apply_new_city_targets


def test_new_city_targets_override_all_generated_scale_fields() -> None:
    original = {
        "city_id": "tokyo_complex",
        "district_count": 14,
        "corridor_count": 16,
        "target_place_count": 0,
    }

    result = _apply_new_city_targets(original, {
        "districts": 28,
        "corridors": 15,
        "places": 238,
        "characters": 12,
    })

    assert original["district_count"] == 14
    assert result["quality_profile"] == "initial_city"
    assert result["district_count"] == 28
    assert result["corridor_count"] == 15
    assert result["target_place_count"] == 238
