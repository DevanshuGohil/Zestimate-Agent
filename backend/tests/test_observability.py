"""Tests for observability.configure()."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import structlog

from zestimate_agent.observability import configure


def _settings(log_level="INFO", langsmith_tracing=False, langsmith_key=None):
    s = MagicMock()
    s.log_level = log_level
    s.langsmith_tracing = langsmith_tracing
    s.langsmith_api_key = MagicMock(get_secret_value=lambda: langsmith_key) if langsmith_key else None
    return s


# ---------------------------------------------------------------------------
# Basic configuration
# ---------------------------------------------------------------------------


def test_configure_with_no_settings_does_not_raise():
    configure(settings=None)


def test_configure_with_settings_does_not_raise():
    configure(settings=_settings())


def test_configure_sets_structlog_processors():
    configure(settings=_settings(log_level="DEBUG"))
    cfg = structlog.get_config()
    assert cfg["processors"]  # non-empty list


def test_configure_is_idempotent():
    configure(settings=_settings())
    configure(settings=_settings())  # second call must not raise


# ---------------------------------------------------------------------------
# LangSmith tracing
# ---------------------------------------------------------------------------


def test_langsmith_not_configured_when_tracing_false():
    env_backup = os.environ.pop("LANGSMITH_TRACING", None)
    lc_backup = os.environ.pop("LANGCHAIN_TRACING_V2", None)
    try:
        configure(settings=_settings(langsmith_tracing=False))
        assert os.environ.get("LANGSMITH_TRACING") is None
        assert os.environ.get("LANGCHAIN_TRACING_V2") is None
    finally:
        if env_backup is not None:
            os.environ["LANGSMITH_TRACING"] = env_backup
        if lc_backup is not None:
            os.environ["LANGCHAIN_TRACING_V2"] = lc_backup


def test_langsmith_env_vars_set_when_tracing_enabled():
    for key in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_PROJECT", "LANGSMITH_API_KEY"):
        os.environ.pop(key, None)
    try:
        configure(settings=_settings(langsmith_tracing=True, langsmith_key="test-key-123"))
        assert os.environ.get("LANGSMITH_TRACING") == "true"
        assert os.environ.get("LANGCHAIN_TRACING_V2") == "true"
        assert os.environ.get("LANGSMITH_PROJECT") == "zestimate-agent"
        assert os.environ.get("LANGSMITH_API_KEY") == "test-key-123"
    finally:
        for key in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_PROJECT", "LANGSMITH_API_KEY"):
            os.environ.pop(key, None)


def test_langsmith_does_not_overwrite_existing_env_vars():
    os.environ["LANGSMITH_PROJECT"] = "my-custom-project"
    try:
        configure(settings=_settings(langsmith_tracing=True))
        assert os.environ["LANGSMITH_PROJECT"] == "my-custom-project"
    finally:
        os.environ.pop("LANGSMITH_PROJECT", None)
        for key in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY"):
            os.environ.pop(key, None)


def test_langsmith_tracing_without_api_key_does_not_raise():
    for key in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_API_KEY"):
        os.environ.pop(key, None)
    try:
        configure(settings=_settings(langsmith_tracing=True, langsmith_key=None))
        assert os.environ.get("LANGSMITH_TRACING") == "true"
        assert os.environ.get("LANGSMITH_API_KEY") is None
    finally:
        for key in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_PROJECT"):
            os.environ.pop(key, None)
