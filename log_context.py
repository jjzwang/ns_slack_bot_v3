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

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

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


# =============================================================================
# Claude-call observability
# =============================================================================
# Every Claude API call (interview turn, pillar extraction, review gate) emits
# one structured log entry through this helper. The summary message is human-
# readable for tail-the-logs debugging; the full prompt and response live in
# the LogRecord's `claude_call` extra so a future Langfuse / Postgres handler
# (Phase 3) can pick them up without changing call sites.

_claude_call_logger = logging.getLogger("claude_call")


def serialize_message_content(content: Any) -> str:
    """Best-effort JSON serialization of an Anthropic response's content
    blocks. Falls back to repr() for SDK objects that aren't JSON-native."""
    try:
        return json.dumps(content, default=lambda o: getattr(o, "__dict__", repr(o)))
    except (TypeError, ValueError):
        return repr(content)


def log_claude_call(
    *,
    domain: str,
    phase: str | None,
    model: str,
    prompt: str,
    response: str,
    latency_ms: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    tenant_id: str = "internal",
) -> None:
    """Emit one structured log entry for a Claude API call.

    `phase` is the interview phase for BSA calls (gathering / review /
    gathering_with_gaps / drafting / verify) or a tag like "extraction" /
    "review_gate" for the helper calls.

    `prompt` and `response` should already be strings — caller decides how to
    serialize (typically `json.dumps` for structured payloads, or the raw
    text for single-message prompts).

    thread_id is read from thread_context automatically and surfaced via the
    `thread_ts` log field, so callers don't pass it.
    """
    summary = (
        f"claude_call domain={domain} phase={phase} model={model} "
        f"latency_ms={latency_ms} in_tokens={input_tokens} out_tokens={output_tokens}"
    )
    _claude_call_logger.info(
        summary,
        extra={
            "claude_call": {
                "domain": domain,
                "phase": phase,
                "model": model,
                "prompt": prompt,
                "response": response,
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tenant_id": tenant_id,
            }
        },
    )
