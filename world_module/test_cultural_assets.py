from __future__ import annotations

import json
from pathlib import Path

from world_module.culture import kb_manager
from world_module.culture.wikivoyage_ingestor import WikivoyageIngestor
from world_module import cultural_assets


def test_wikivoyage_writer_uses_canonical_city_id_for_multiword_title(tmp_path: Path) -> None:
    ingestor = WikivoyageIngestor(str(tmp_path / "dump.xml"), str(tmp_path / "evidence"))
    path = ingestor.write_evidence({
        "city_id": "new york",
        "page_title": "New York",
        "evidence_cards": [{"evidence_id": "wv_new_york_001"}],
    }, "new_york_united_states")

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "new_york_united_states.json"
    assert payload["city_id"] == "new_york_united_states"
    assert payload["source_city_id"] == "new york"
    assert payload["page_title"] == "New York"


def test_empty_source_page_is_not_reported_as_available(tmp_path: Path, monkeypatch) -> None:
    evidence_root = tmp_path / "evidence"
    constraints_root = tmp_path / "constraints"
    dump_path = tmp_path / "dump.xml"
    dump_path.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(cultural_assets, "EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(cultural_assets, "WIKIVOYAGE_DUMP", dump_path)
    monkeypatch.setattr(kb_manager, "EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(kb_manager, "OUTPUT_ROOT", constraints_root)

    class EmptyIngestor:
        def __init__(self, *_args) -> None:
            pass

        def extract_city(self, title: str):
            return {"city_id": title.lower(), "page_title": title, "evidence_cards": []}

        def write_evidence(self, *_args):
            raise AssertionError("Empty evidence must not be persisted as a usable city asset")

    monkeypatch.setattr(cultural_assets, "WikivoyageIngestor", EmptyIngestor)
    result = cultural_assets.ensure_cultural_assets(
        "new_york_united_states", "New York", source_titles=["New York City"],
    )

    assert result["status"] == "unavailable"
    assert result["reason"] == "wikivoyage_page_without_eligible_evidence"
    assert [item["title"] for item in result["source_attempts"]] == ["New York City", "New York"]
    assert not (evidence_root / "new_york_united_states.json").exists()


def test_source_alias_writes_and_imports_canonical_assets(tmp_path: Path, monkeypatch) -> None:
    evidence_root = tmp_path / "evidence"
    constraints_root = tmp_path / "constraints"
    dump_path = tmp_path / "dump.xml"
    dump_path.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(cultural_assets, "EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(cultural_assets, "WIKIVOYAGE_DUMP", dump_path)
    monkeypatch.setattr(kb_manager, "EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(kb_manager, "OUTPUT_ROOT", constraints_root)

    class AliasIngestor:
        def __init__(self, _dump, output_dir) -> None:
            self.output_dir = Path(output_dir)

        def extract_city(self, title: str):
            if title != "New York City":
                return None
            return {
                "city_id": "new york city", "page_title": title,
                "evidence_cards": [{
                    "evidence_id": "wv_nyc_001", "section": "Respect",
                    "text": "A sufficiently long evidence paragraph for testing.", "keywords": ["custom"],
                }],
            }

        def write_evidence(self, evidence, output_key):
            self.output_dir.mkdir(parents=True, exist_ok=True)
            path = self.output_dir / f"{output_key}.json"
            payload = dict(evidence)
            payload["city_id"] = output_key
            path.write_text(json.dumps(payload), encoding="utf-8")
            return path

    monkeypatch.setattr(cultural_assets, "WikivoyageIngestor", AliasIngestor)
    result = cultural_assets.ensure_cultural_assets(
        "new_york_united_states", "New York", source_titles=["New York City"],
    )

    assert result["status"] == "available"
    assert result["evidence_cards"] == 1
    assert Path(result["evidence_path"]).name == "new_york_united_states.json"
    assert Path(result["constraint_path"]).name == "new_york_united_states.json"
