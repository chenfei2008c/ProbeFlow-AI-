from test_core import admin, app, settings  # noqa: F401
from test_budget import database  # noqa: F401

import pytest


@pytest.fixture(autouse=True)
def isolate_automatic_tests_from_live_mode(monkeypatch):
    # A developer's local .env must never turn the regression suite into paid calls.
    # Tests of live adapters opt in explicitly and use a local fake transport/provider.
    monkeypatch.setenv("MODE", "mock")
