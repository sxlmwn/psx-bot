"""
Structured JSON logging module for VeteranDesk.
Guarantees every log event carries trade_id and session_id context.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any, Optional
import structlog

# Context variables for automatic tracing
ctx_session_id: ContextVar[str] = ContextVar("ctx_session_id", default="default_session")
ctx_trade_id: ContextVar[Optional[str]] = ContextVar("ctx_trade_id", default=None)


def add_tracing_context(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Inject session_id and trade_id into every structured log event."""
    if "session_id" not in event_dict:
        event_dict["session_id"] = ctx_session_id.get()
    if "trade_id" not in event_dict:
        trade_id = ctx_trade_id.get()
        if trade_id is not None:
            event_dict["trade_id"] = trade_id
        else:
            event_dict["trade_id"] = "n/a"
    return event_dict


def setup_logging(log_level: str = "INFO", json_format: bool = True) -> None:
    """Configure structlog for production JSON output and robust tracing."""
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        add_tracing_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: Any
    if json_format:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "veterandesk") -> Any:
    """Retrieve a bound logger instance."""
    return structlog.get_logger(name)


def set_trade_context(trade_id: Optional[str]) -> None:
    """Set the trade_id context var for the current execution flow."""
    ctx_trade_id.set(trade_id)


def set_session_context(session_id: str) -> None:
    """Set the session_id context var for the current execution flow."""
    ctx_session_id.set(session_id)


# Auto-initialize logging on import
setup_logging()
logger = get_logger("veterandesk.core")
