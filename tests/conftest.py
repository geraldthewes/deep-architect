"""Shared pytest fixtures."""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _dummy_anthropic_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure a dummy API key is always set so the harness key-guard doesn't
    fire in tests that mock out the actual API calls."""
    if not os.environ.get("ANTHROPIC_AUTH_TOKEN") and not os.environ.get("ANTHROPIC_API_KEY"):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-key")
