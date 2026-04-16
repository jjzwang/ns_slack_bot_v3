# =============================================================================
# Log Context — per-thread correlation IDs for structured debugging
# =============================================================================
# Usage:
#   from log_context import thread_context
#
#   with thread_context(thread_ts):
#       ... any log calls here (or in functions called from here) will
#       automatically include [thread=1729...] in the formatted output.
#
# Why contextvars instead of passing thread_ts as a parameter:
#   - No signature changes to helper functions (reviewer, jira_client, etc.)
#   - Works across module boundaries automatically
#   - Thread-safe: each worker gets its own copy

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional

# The "-" default means "no active thread context" — useful for startup
# logs (validate_config, init_db) that happen before any interview starts.
_thread_ts_var: ContextVar[str] = ContextVar("thread_ts", default="-")


@contextmanager
def thread_context(thread_ts: str):
    """Bind thread_ts to the current execution context for the duration
    of the `with` block. All log calls inside will include it."""
    token = _thread_ts_var.set(thread_ts)
    try:
        yield
    finally:
        _thread_ts_var.reset(token)


class ThreadContextFilter(logging.Filter):
    """Injects the current thread_ts (or '-' if none) into every LogRecord
    so it can be referenced in the format string as %(thread_ts)s."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.thread_ts = _thread_ts_var.get()
        return True  # never drop records; we're only annotating