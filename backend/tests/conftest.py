from test_core import admin, app, settings  # noqa: F401
from test_budget import database  # noqa: F401

import pytest


@pytest.fixture(autouse=True)
def isolate_automatic_tests_from_live_mode(monkeypatch, tmp_path):
    # A developer's local .env must never turn the regression suite into paid calls.
    # Tests of live adapters opt in explicitly and use a local fake transport/provider.
    monkeypatch.setenv("MODE", "mock")
    # Real backup rotation runs in integration tests. Never inherit production
    # data/backup locations from either the shell environment or local .env.
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
