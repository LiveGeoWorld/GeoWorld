"""Repository-wide pytest compatibility hooks."""

from __future__ import annotations

import os
from pathlib import Path


def pytest_configure() -> None:
    """Avoid an inaccessible private tmp directory in this Windows terminal.

    Python 3.10 on Windows applies a restrictive ACL for ``mode=0o700``.
    The current Codex terminal token cannot subsequently enumerate that ACL,
    while pytest's ``tmp_path`` factory deliberately uses that mode.  This is
    a test-process-only adjustment: it changes no application output or data.
    """
    if os.name != "nt":
        return

    import _pytest.pathlib
    test_temp_root = Path.cwd() / ".test_tmp"
    test_temp_root.mkdir(exist_ok=True)
    os.environ["PYTEST_DEBUG_TEMPROOT"] = str(test_temp_root)

    original_make_numbered_dir = _pytest.pathlib.make_numbered_dir

    def make_numbered_dir(root, prefix, mode=0o700):
        return original_make_numbered_dir(root, prefix, mode=0o777)

    _pytest.pathlib.make_numbered_dir = make_numbered_dir

    # pytest also makes one 0o700 directory per tmp_path fixture.  Keep the
    # replacement inside the pytest process, so all of those directories stay
    # accessible to the current Windows terminal token.
    original_mkdir = Path.mkdir

    def compatible_mkdir(path, mode=0o777, parents=False, exist_ok=False):
        if mode == 0o700:
            mode = 0o777
        return original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    Path.mkdir = compatible_mkdir


import importlib
import json
import pytest


@pytest.fixture
def local_city_culture(tmp_path, monkeypatch):
    """Local City A evidence for registry tests; use the real materializer.

    Download/extraction is tested separately. An accidental download in these
    registration tests is an error, rather than a dependency on external DNS.
    """
    from world_module import cultural_assets
    kb = importlib.import_module(cultural_assets.CulturalKB.__module__)
    evidence_root = tmp_path / "cultural_source_fixture" / "evidence"
    constraint_root = tmp_path / "cultural_source_fixture" / "constraints"
    evidence_root.mkdir(parents=True)
    evidence = {
        "city_id": "city_a", "page_title": "City A", "source": "test_fixture",
        "evidence_cards": [{
            "evidence_id": "fixture_city_a_001", "section": "Respect",
            "text": "This synthetic City A paragraph is local test evidence, not a real regional rule.",
            "keywords": ["custom"],
        }],
    }
    (evidence_root / "city_a.json").write_text(json.dumps(evidence), encoding="utf-8")
    monkeypatch.setattr(cultural_assets, "PIPELINE_ROOT", tmp_path)
    monkeypatch.setattr(cultural_assets, "EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(kb, "EVIDENCE_ROOT", evidence_root)
    monkeypatch.setattr(kb, "OUTPUT_ROOT", constraint_root)

    def unexpected_download(*_args, **_kwargs):
        raise AssertionError("Registry fixture unexpectedly attempted a Wikivoyage download")

    monkeypatch.setattr(cultural_assets, "ensure_wikivoyage_dump", unexpected_download)
    return evidence


@pytest.fixture
def example_pipeline_config():
    """Unit tests use the bundled blank config, never a user's credentials."""
    from shared.config_loader import PipelineConfig
    return PipelineConfig(Path(__file__).resolve().parent / "config.yaml.example")


@pytest.fixture(scope='session',autouse=True)
def local_stdlib_tempdir(tmp_path_factory):
    """Keep unittest TemporaryDirectory fixtures in the same isolated test tree."""
    import tempfile
    previous=tempfile.tempdir
    tempfile.tempdir=str(tmp_path_factory.mktemp('stdlib'))
    yield
    tempfile.tempdir=previous
