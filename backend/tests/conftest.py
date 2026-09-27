import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Keep aliases / LLM cache out of /data during tests."""
    import taxonomy, llm, accounts
    monkeypatch.setattr(taxonomy, "ALIASES_FILE", tmp_path / "category_aliases.json")
    monkeypatch.setattr(accounts, "MAP_FILE", tmp_path / "account_map.json")
    import review
    monkeypatch.setattr(review, "DATA_DIR", tmp_path)
    monkeypatch.setattr(review, "DB_FILE", tmp_path / "review.db")
    monkeypatch.setattr(llm, "CACHE_FILE", tmp_path / "llm_cache.json")
    monkeypatch.setenv("LLM_PROVIDER", "none")
    yield
