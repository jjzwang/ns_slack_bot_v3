# =============================================================================
# Claude API Client
# =============================================================================

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional, Union

import anthropic

from config import CLAUDE_MODEL, CLAUDE_TOOLS, MAX_ATTEMPTS
from database import InterviewState
from prompt_builder import assemble_prompt

logger = logging.getLogger(__name__)


@dataclass
class MessageResponse:
    text: str


@dataclass
class SubmitTicketResponse:
    title: str
    description: str
    value_to_business: str
    acceptance_criteria: str
    enablement_plan: str


@dataclass
class EscalateResponse:
    reason: str
    partial_data: dict


ClaudeResponse = Union[MessageResponse, SubmitTicketResponse, EscalateResponse]

_INTERNAL_TAG_PATTERNS = [
    re.compile(r"<state>.*?</state>", re.DOTALL),
    re.compile(r"<internal>.*?</internal>", re.DOTALL),
    re.compile(r"<tracking>.*?</tracking>", re.DOTALL),
]


def _strip_internal_tags(text: str) -> str:
    for pattern in _INTERNAL_TAG_PATTERNS:
        text = pattern.sub("", text)
    return text.strip()


_SUBMIT_TICKET_REQUIRED = ["title", "description", "value_to_business", "acceptance_criteria", "enablement_plan"]


def _validate_submit_ticket(inp: dict) -> SubmitTicketResponse:
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
    if "reason" not in inp or not inp["reason"]:
        raise ValueError("escalate tool call missing required field: reason")
    return EscalateResponse(reason=str(inp["reason"]), partial_data=inp.get("partial_data", {}))


# ─── API Client ──────────────────────────────────────────────────────────────

INITIAL_BACKOFF_S = 1.0

# Module-level client cache — reusing the same Anthropic client preserves its
# internal httpx connection pool, avoiding repeated TLS handshakes per turn.
_clients: dict[str, anthropic.Anthropic] = {}


def _get_client(api_key: str) -> anthropic.Anthropic:
    if api_key not in _clients:
        _clients[api_key] = anthropic.Anthropic(api_key=api_key)
    return _clients[api_key]


def call_claude(
    message_history: list[dict[str, str]],
    api_key: str,
    state: Optional[InterviewState] = None,
    phase: Optional[str] = None,
) -> ClaudeResponse:
    """
    Call the Claude API with the full conversation history.

    Args:
        message_history: List of {"role": "user"|"assistant", "content": str}
        api_key: Anthropic API key
        state: InterviewState for pillar-based phase detection.
        phase: Pre-computed phase from _run_interview_turn. When provided,
               assemble_prompt skips re-detection (avoids redundant call and
               guarantees the prompt matches the routing decision upstream).
    """
    client = _get_client(api_key)
    system_prompt = assemble_prompt(message_history, state=state, phase=phase)

    last_error: Optional[Exception] = None

    for attempt in range(MAX_ATTEMPTS):
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
            if attempt < MAX_ATTEMPTS - 1:
                wait = INITIAL_BACKOFF_S * (2 ** attempt)
                logger.warning(f"Rate limited. Retrying in {wait}s (attempt {attempt + 1}/{MAX_ATTEMPTS})")
                time.sleep(wait)
            else:
                raise

        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                last_error = e
                if attempt < MAX_ATTEMPTS - 1:
                    wait = INITIAL_BACKOFF_S * (2 ** attempt)
                    logger.warning(f"Server error {e.status_code}. Retrying in {wait}s (attempt {attempt + 1}/{MAX_ATTEMPTS})")
                    time.sleep(wait)
                else:
                    raise
            else:
                raise

        except anthropic.APIConnectionError as e:
            last_error = e
            if attempt < MAX_ATTEMPTS - 1:
                wait = INITIAL_BACKOFF_S * (2 ** attempt)
                logger.warning(f"Connection error. Retrying in {wait}s (attempt {attempt + 1}/{MAX_ATTEMPTS})")
                time.sleep(wait)
            else:
                raise

    raise last_error or RuntimeError("Claude API call failed after all retries")


def _parse_response(response: Any) -> ClaudeResponse:
    text_content = ""
    tool_call: Optional[dict] = None

    for block in response.content:
        if block.type == "text":
            text_content += block.text
        elif block.type == "tool_use":
            tool_call = {"name": block.name, "input": block.input}

    if tool_call:
        if tool_call["name"] == "submit_ticket":
            return _validate_submit_ticket(tool_call["input"])
        if tool_call["name"] == "escalate":
            return _validate_escalate(tool_call["input"])
        logger.warning(f"Unknown tool call: {tool_call['name']}")

    if text_content:
        clean_text = _strip_internal_tags(text_content)
        if clean_text:
            return MessageResponse(text=clean_text)

    raise RuntimeError("Claude returned empty response with no text or tool calls.")
