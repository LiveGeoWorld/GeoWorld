"""Offline download-contract checks, separate from city registration fixtures."""
import bz2
import hashlib
import importlib
from io import BytesIO

import pytest

from world_module import cultural_assets, wikivoyage_download as downloader


def test_existing_dump_is_reused_without_network(tmp_path, monkeypatch):
    target = tmp_path / downloader.DUMP_NAME
    target.write_bytes(b"existing local source")
    def unexpected_request(_name):
        raise AssertionError("A local dump must be reused")
    monkeypatch.setattr(downloader, "_open_source", unexpected_request)
    assert downloader.ensure_wikivoyage_dump(target) == target
    assert target.read_bytes() == b"existing local source"


@pytest.mark.parametrize("filename", [downloader.DUMP_NAME, "enwikivoyage-20261101-pages-articles.xml.bz2"])
def test_missing_dump_is_checked_and_published_atomically(tmp_path, monkeypatch, filename):
    body = bz2.compress(b"<mediawiki/>")
    checksum = hashlib.sha1(body).hexdigest()
    target = tmp_path / "source" / downloader.DUMP_NAME
    requests = []
    def source(name):
        requests.append(name)
        assert not target.exists()
        return BytesIO(f"{checksum}  {filename}\n".encode() if name.endswith("sha1sums.txt") else body)
    monkeypatch.setattr(downloader, "_open_source", source)
    assert downloader.ensure_wikivoyage_dump(target).read_bytes() == body
    expected = filename if filename == downloader.DUMP_NAME else "../20261101/" + filename
    assert requests == ["enwikivoyage-latest-sha1sums.txt", expected]
    assert not list(target.parent.glob("*.part"))


def test_bad_checksum_is_not_published_and_partial_file_is_removed(tmp_path, monkeypatch):
    target = tmp_path / downloader.DUMP_NAME
    def source(name):
        return BytesIO(("0" * 40 + "  " + downloader.DUMP_NAME).encode() if name.endswith("sha1sums.txt") else b"corrupt data")
    monkeypatch.setattr(downloader, "_open_source", source)
    with pytest.raises(ValueError, match="checksum mismatch"):
        downloader.ensure_wikivoyage_dump(target)
    assert not target.exists()
    assert not list(tmp_path.glob("*.part"))


def test_missing_dump_flows_through_real_download_extraction_and_materialization(tmp_path, monkeypatch):
    kb = importlib.import_module(cultural_assets.CulturalKB.__module__)
    dump = tmp_path / "source" / downloader.DUMP_NAME
    evidence = tmp_path / "evidence"
    monkeypatch.setattr(cultural_assets, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(cultural_assets, "WIKIVOYAGE_DUMP", dump)
    monkeypatch.setattr(cultural_assets, "EVIDENCE_ROOT", evidence)
    monkeypatch.setattr(kb, "EVIDENCE_ROOT", evidence)
    monkeypatch.setattr(kb, "OUTPUT_ROOT", tmp_path / "constraints")
    xml = b'''<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.10/"><page><title>City A</title><revision><text>== Eat ==
The restaurants of this synthetic test city serve local soup and bread. This paragraph is a test fixture and does not describe a real place.
</text></revision></page></mediawiki>'''
    body = bz2.compress(xml)
    requests = []
    def source(name):
        requests.append(name)
        checksum = hashlib.sha1(body).hexdigest()
        return BytesIO((checksum + "  " + downloader.DUMP_NAME).encode() if name.endswith("sha1sums.txt") else body)
    monkeypatch.setattr(downloader, "_open_source", source)
    result = cultural_assets.materialize_world_cultural_assets("test_world", "city_a", "City A")
    assert result["status"] == "available"
    assert result["extracted_from_dump"] is True
    assert result["evidence_cards"] > 0
    assert len(requests) == 2
    assert result["materialization"] == "world_local_snapshot"
    assert dump.read_bytes() == body
