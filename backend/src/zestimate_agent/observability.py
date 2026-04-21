"""Observability: structlog configuration and LangSmith tracing setup.

Call `configure()` once at process startup (cli.py, run_eval.py).
It is safe to call multiple times — subsequent calls reconfigure in place.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog


def configure(settings: Any = None) -> None:
    """Configure structlog processors and (optionally) LangSmith tracing.

    When connected to a TTY the output is human-readable via ConsoleRenderer.
    In CI or when piped, output is newline-delimited JSON for log aggregators.

    If settings.langsmith_tracing is True, the env vars that LangGraph reads
    for remote tracing (LANGSMITH_TRACING, LANGCHAIN_TRACING_V2,
    LANGSMITH_API_KEY, LANGSMITH_PROJECT) are set from settings.
    """
    level_name = "INFO"
    if settings is not None:
        try:
            level_name = str(settings.log_level).upper()
        except AttributeError:
            pass

    level = getattr(logging, level_name, logging.INFO)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
    ]

    processors = shared + (
        [structlog.dev.ConsoleRenderer()]
        if sys.stderr.isatty()
        else [structlog.processors.JSONRenderer()]
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    _configure_langsmith(settings)


def _configure_langsmith(settings: Any) -> None:
    """Propagate LangSmith settings to the env vars LangChain/LangGraph checks."""
    if settings is None:
        return
    if not getattr(settings, "langsmith_tracing", False):
        return

    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")  # legacy compat

    langsmith_key = getattr(settings, "langsmith_api_key", None)
    if langsmith_key is not None:
        try:
            os.environ.setdefault("LANGSMITH_API_KEY", langsmith_key.get_secret_value())
        except AttributeError:
            os.environ.setdefault("LANGSMITH_API_KEY", str(langsmith_key))

    os.environ.setdefault("LANGSMITH_PROJECT", "zestimate-agent")

    import structlog as _sl
    _sl.get_logger(__name__).debug(
        "langsmith.tracing.enabled",
        project=os.environ.get("LANGSMITH_PROJECT"),
    )
