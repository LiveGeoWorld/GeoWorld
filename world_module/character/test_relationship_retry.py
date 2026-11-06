from __future__ import annotations

from world_module.character.llm_generator.generator import _parse_relationships_with_retry


class _Logger:
    def __init__(self) -> None:
        self.entries: list[str] = []

    def log_step_response(self, _text: str, summary: str) -> None:
        self.entries.append(summary)


def test_relationship_json_is_retried_without_regenerating_profiles() -> None:
    responses = iter([
        '{"relationships": [{"character_id": "char_1" "related_id": "char_2"}]}',
        '{"relationships": [{"character_id": "char_1", "related_id": "char_2", "type": "friend"}]}',
    ])
    logger = _Logger()

    relationships, graph = _parse_relationships_with_retry(
        lambda: next(responses),
        char_ids={"person_a", "person_b"},
        id_remap={"char_1": "person_a", "char_2": "person_b"},
        logger=logger,
        max_attempts=3,
    )

    assert len(relationships) == 1
    assert relationships[0].character_id == "person_a"
    assert graph.edges[0].to_id == "person_b"
    assert "parse failed" in logger.entries[0]
    assert "attempt 2/3" in logger.entries[1]
