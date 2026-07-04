from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[1]


@pytest.fixture(autouse=True)
def run_from_repo_root(monkeypatch):
    """Relative paths in the config (fence, bathymetry) assume the repo root."""
    monkeypatch.chdir(REPO_ROOT)
