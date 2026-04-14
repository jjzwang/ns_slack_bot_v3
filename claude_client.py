# =============================================================================
# Claude API Client
# =============================================================================
# Handles all communication with the Anthropic Claude API, including
# retry logic with exponential backoff and structured tool_use parsing.
#
# Uses the prompt_builder to assemble a focused system prompt per turn
# instead of sending the full monolithic prompt every time.

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional, Union

import anthropic

from config import CLAUDE_MODEL, CLAUDE_TOOLS
from database import InterviewState
from prompt_builder import assemble_prompt

logger = logging.getLogger(__name__)

# ─── Response Types ──────────────────────────────────────────────────────────


@dataclass
class MessageResponse:
    """Claude returned a text message to continue the conversation."""
    text: str


@dataclass
class SubmitTicketResponse:
    """Claude called submit_ticket — all pillars are verified."""
    title: str
    description: str
    value_to_business: str
    acceptance_criteria: str
    enablement_plan: str


@dataclass
class EscalateResponse:
    """Claude called escalate — user couldn't clarify after multiple attempts."""
    reason: str
    partial_data: dict


ClaudeResponse = Union[MessageResponse, SubmitTicketResponse, EscalateResponse]


# ─── Internal Tag Patterns to Strip ──────────────────────────────────────────
# Claude may output internal state tracking in XML-style tags that must never
# be shown to the user. Add patterns here as needed.

_INTERNAL_TAG_PATTERNS = [
    re.compile(r"<state>.*?</state>", re.DOTALL),
    re.compile(r"<internal>.*?</internal>", re.DOTALL),
    re.compile(r"<tracking>.*?</tracking>", re.DOTALL),
]


def _strip_internal_tags(text: str) -> str:
    """Remove any internal state/tracking tags Claude may include in its response."""
    for pattern in _INTERNAL_TAG_PATTERNS:
        text = pattern.sub("", text)
    return text.strip()


# ─── Tool Response Validation ────────────────────────────────────────────────

_SUBMIT_TICKET_REQUIRED = ["title", "description", "value_to_business", "acceptance_criteria", "enablement_plan"]
_ESCALATE_REQUIRED = ["reason"]


def _validate_submit_ticket(inp: dict) -> SubmitTicketResponse:
    """Validate and parse submit_ticket tool input."""
    missing = [f for f in _SUBMIT_TICKET_REQUIRED if f not in inp or not inp[f]]
    if missing:
        raise ValueError(f"submit_ticket tool call missing required fields: {missing}")
    return SubmitTicketResponse(
        title=str(inp["title"]),
        description=str(inp["description"]),
        value_to_business=str(inp["value_to_business"]),
        acceptance_criteria=str(inp["acceptance_criteria"]),
        enablement_plan=str(inp["enablement_plan"]),
    )


def _validate_escalate(inp: dict) -> EscalateResponse:
    """Validate and parse escalate tool input."""
    if "reason" not in inp or not inp["reason"]:
        raise ValueError("escalate tool call missing required field: reason")
    return EscalateResponse(
        reason=str(inp["reason"]),
        partial_data=inp.get("partial_data", {}),
    )


# ─── API Client ──────────────────────────────────────────────────────────────

MAX_RETRIES = 3
INITIAL_BACKOFF_S = 1.0


def call_claude(
    message_history: list[dict[str, str]],
    api_key: str,
    state: Optional[InterviewState] = None,
) -> ClaudeResponse:
    """
    Call the Claude API with the full conversation history.

    The system prompt is assembled dynamically per turn by the prompt_builder,
    which selects the appropriate phase instructions and domain probes based
    on the conversation state.

    Args:
        message_history: List of {"role": "user"|"assistant", "content": str}
        api_key: Anthropic API key
        state: InterviewState for pillar-based phase detection.
               If None, falls back to heuristic detection.

    Returns:
        MessageResponse, SubmitTicketResponse, or EscalateResponse
    """
    client = anthropic.Anthropic(api_key=api_key)

    # Assemble the system prompt for this specific turn
    system_prompt = assemble_prompt(message_history, state=state)

    last_error: Optional[Exception] = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=2048,
                system=system_prompt,
                tools=CLAUDE_TOOLS,
                messages=message_history,
            )
            return _parse_response(response)

        except anthropic.RateLimitError as e:
            last_error = e
            if attempt < MAX_RETRIES:
                wait = INITIAL_BACKOFF_S * (2 ** attempt)
                logger.warning(f"Rate limited. Retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
            else:
                raise

        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                last_error = e
                if attempt < MAX_RETRIES:
                    wait = INITIAL_BACKOFF_S * (2 ** attempt)
                    logger.warning(f"Server error {e.status_code}. Retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})")
                    time.sleep(wait)
                else:
                    raise
            else:
                # Client error (4xx except rate limit) — don't retry
                raise

        except anthropic.APIConnectionError as e:
            last_error = e
            if attempt < MAX_RETRIES:
                wait = INITIAL_BACKOFF_S * (2 ** attempt)
                logger.warning(f"Connection error. Retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
            else:
                raise

    # Should not reach here, but just in case
    raise last_error or RuntimeError("Claude API call failed after all retries")


def _parse_response(response: Any) -> ClaudeResponse:
    """Parse Claude's response content blocks into typed responses."""
    text_content = ""
    tool_call: Optional[dict] = None

    for block in response.content:
        if block.type == "text":
            text_content += block.text
        elif block.type == "tool_use":
            tool_call = {"name": block.name, "input": block.input}

    # Tool calls take priority over text
    if tool_call:
        if tool_call["name"] == "submit_ticket":
            return _validate_submit_ticket(tool_call["input"])
        if tool_call["name"] == "escalate":
            return _validate_escalate(tool_call["input"])
        # Unknown tool — log and fall through to text
        logger.warning(f"Unknown tool call: {tool_call['name']}")

    if text_content:
        # Strip any internal state/tracking tags before returning to user
        clean_text = _strip_internal_tags(text_content)
        if clean_text:
            return MessageResponse(text=clean_text)

    raise RuntimeError("Claude returned empty response with no text or tool calls.")
