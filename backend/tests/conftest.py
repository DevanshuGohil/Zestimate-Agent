"""Shared pytest config: skip `@pytest.mark.live` tests unless explicitly selected."""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    marker_expr = config.getoption("-m") or ""
    if "live" in marker_expr:
        return  # user explicitly opted in via `-m live`
    skip_live = pytest.mark.skip(reason="live test; run with `pytest -m live`")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
