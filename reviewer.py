# =============================================================================
# Reviewer — Pillar Extraction + Solution Review Gate
# =============================================================================
# Two functions:
#   1. extract_pillars()   — Haiku call to extract structured pillars
#   2. run_review_gate()   — Sonnet call to review completeness
#
# Both return structured data and handle timeout/errors gracefully.

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import anthropic
import httpx

from config import (
    CLAUDE_MODEL,
    EXTRACTION_MODEL,
    EXTRACTION_TIMEOUT_S,
    MAX_REVIEW_ENRICHMENTS,
    MAX_REVIEW_GAPS,
    REVIEW_TIMEOUT_S,
)
from review_prompts import EXTRACTION_PROMPT, REVIEW_PROMPT

logger = logging.getLogger(__name__)

# ─── Pillar priority for gap sorting (higher index = lower priority) ─────────
_PILLAR_PRIORITY = {"action": 0, "persona": 1, "goal": 2, "business_value": 3}

# ─── Valid values for schema validation ──────────────────────────────────────
_VALID_SEVERITIES = {"high", "medium"}
_VALID_GAP_PILLARS = {"action", "persona", "goal", "business_value"}
_VALID_ENRICHMENT_PILLARS = {"description", "acceptance_criteria"}
_VALID_CATEGORIES = {
    "implementation_approach", "edge_case", "native_alternative",
    "downstream_impact", "compliance_risk", "integration_dependency",
    "governance_concern", "scope_clarification",
}
_VALID_CONFIDENCES = {"high", "medium", "low"}


# ─── Result Types ────────────────────────────────────────────────────────────

@dataclass
class ExtractionResult:
    """Result of pillar extraction."""
    pillars: dict  # {persona, action, goal, business_value} — values or None
    duration_ms: int = 0
    success: bool = True
    error: Optional[str] = None


@dataclass
class ReviewResult:
    """Result of the solution review gate."""
    gaps: list = field(default_factory=list)
    enrichments: list = field(default_factory=list)
    duration_ms: int = 0
    success: bool = True
    skipped: bool = False
    error: Optional[str] = None


# =============================================================================
# Pillar Extraction
# =============================================================================

def extract_pillars(
    message_history: list[dict[str, str]],
    api_key: str,
) -> ExtractionResult:
    """
    Extract structured pillars from conversation history using Haiku.

    Args:
        message_history: Full conversation history
        api_key: Anthropic API key

    Returns:
        ExtractionResult with parsed pillars dict
    """
    start = time.monotonic()

    try:
        client = anthropic.Anthropic(
            api_key=api_key,
            timeout=httpx.Timeout(EXTRACTION_TIMEOUT_S, connect=5.0),
        )

        # Format conversation for the prompt
        conv_text = _format_conversation(message_history)
        prompt = EXTRACTION_PROMPT.format(conversation_history=conv_text)

        response = client.messages.create(
            model=EXTRACTION_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )

        # Extract text from response
        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text

        pillars = _parse_extraction_json(text)
        duration_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            "pillar_extraction_complete",
            extra={
                "pillars_populated": [k for k, v in pillars.items() if v is not None],
                "pillars_missing": [k for k, v in pillars.items() if v is None],
                "extraction_duration_ms": duration_ms,
            },
        )

        return ExtractionResult(pillars=pillars, duration_ms=duration_ms)

    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "pillar_extraction_skipped",
            extra={
                "reason": type(e).__name__,
                "error": str(e),
                "extraction_duration_ms": duration_ms,
            },
        )
        return ExtractionResult(
            pillars={"persona": None, "action": None, "goal": None, "business_value": None},
            duration_ms=duration_ms,
            success=False,
            error=str(e),
        )


def merge_pillars(existing: dict, new_extraction: dict) -> dict:
    """
    Merge extracted pillars with existing state.

    Ratchet mechanism: a non-null existing value is never overwritten by null.
    This prevents pillar regression where a previously extracted value
    disappears on a later turn due to conversation context shifts.
    """
    merged = dict(existing)
    for key, value in new_extraction.items():
        if value is not None:
            merged[key] = value
    return merged


def core_pillars_ready(pillars: dict) -> bool:
    """Check if all four core pillars are populated (non-null)."""
    return all(
        pillars.get(p) is not None
        for p in ("persona", "action", "goal", "business_value")
    )


# =============================================================================
# Solution Review Gate
# =============================================================================

def run_review_gate(
    pillars: dict,
    message_history: list[dict[str, str]],
    api_key: str,
) -> ReviewResult:
    """
    Run the combined solution review on gathered pillars.

    Args:
        pillars: Structured pillars from extraction
        message_history: Full conversation history for secondary context
        api_key: Anthropic API key

    Returns:
        ReviewResult with gaps and enrichments
    """
    start = time.monotonic()

    try:
        client = anthropic.Anthropic(
            api_key=api_key,
            timeout=httpx.Timeout(REVIEW_TIMEOUT_S, connect=5.0),
        )

        # Build the review prompt
        pillars_text = json.dumps(pillars, indent=2)
        conv_text = _format_conversation(message_history)
        prompt = REVIEW_PROMPT.format(
            structured_pillars=pillars_text,
            conversation_history=conv_text,
        )

        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )

        # Extract text from response
        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text

        gaps, enrichments = _parse_review_json(text)
        duration_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            "review_gate_complete",
            extra={
                "gaps_count": len(gaps),
                "gaps_severity": [g.get("severity") for g in gaps],
                "gaps_pillars": [g.get("pillar") for g in gaps],
                "enrichments_count": len(enrichments),
                "enrichments_categories": [e.get("category") for e in enrichments],
                "review_duration_ms": duration_ms,
                "review_skipped": False,
            },
        )

        return ReviewResult(
            gaps=gaps,
            enrichments=enrichments,
            duration_ms=duration_ms,
        )

    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning(
            "review_gate_skipped",
            extra={
                "reason": type(e).__name__,
                "error": str(e),
                "review_duration_ms": duration_ms,
            },
        )
        return ReviewResult(
            duration_ms=duration_ms,
            success=False,
            skipped=True,
            error=str(e),
        )


# =============================================================================
# JSON Parsing Helpers
# =============================================================================

def _format_conversation(history: list[dict]) -> str:
    """Format message history into readable text for prompts."""
    lines = []
    for msg in history:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _extract_json_object(text: str) -> Optional[dict]:
    """
    Robustly extract a JSON object from Claude's response text.

    Handles common failure modes:
      - Clean JSON (just the object)
      - Markdown fences (```json ... ```)
      - Conversational preamble before/after the JSON
      - Multiple JSON objects (takes the first valid one)

    Returns the parsed dict, or None if no valid JSON found.
    """
    text = text.strip()

    # ─── Attempt 1: Parse the whole string as-is ─────────────────────
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # ─── Attempt 2: Strip markdown fences ────────────────────────────
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fence_match:
        try:
            data = json.loads(fence_match.group(1).strip())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    # ─── Attempt 3: Find first { and last } — try to parse that span
    # This handles preamble like "Here's the analysis:\n{...}"
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace:last_brace + 1]
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    return None


def _parse_extraction_json(text: str) -> dict:
    """
    Parse extraction response into a pillars dict.

    Returns dict with keys persona, action, goal, business_value.
    Values are strings or None.
    """
    _empty = {"persona": None, "action": None, "goal": None, "business_value": None}

    data = _extract_json_object(text)
    if data is None:
        logger.warning(f"Failed to parse extraction JSON: {text[:200]}")
        return _empty

    # Normalize: ensure all four keys exist, convert empty strings to None
    result = {}
    for key in ("persona", "action", "goal", "business_value"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            result[key] = val.strip()
        else:
            result[key] = None

    return result


def _parse_review_json(text: str) -> tuple[list[dict], list[dict]]:
    """
    Parse review response into (gaps, enrichments).

    Performs partial parsing — extracts whatever valid entries exist
    and discards malformed ones with a warning.
    """
    data = _extract_json_object(text)
    if data is None:
        logger.warning(f"Failed to parse review JSON: {text[:300]}")
        return [], []

    # ─── Parse gaps ──────────────────────────────────────────────────
    raw_gaps = data.get("gaps", [])
    if not isinstance(raw_gaps, list):
        raw_gaps = []

    valid_gaps = []
    for gap in raw_gaps:
        if not isinstance(gap, dict):
            continue
        # Validate required fields
        if not all(k in gap for k in ("pillar", "severity", "gap", "suggested_question")):
            logger.warning(f"Discarding malformed gap (missing fields): {gap}")
            continue
        # Validate enum values (lenient — accept if close)
        if gap.get("severity") not in _VALID_SEVERITIES:
            gap["severity"] = "medium"
        if gap.get("pillar") not in _VALID_GAP_PILLARS:
            logger.warning(f"Discarding gap with invalid pillar: {gap.get('pillar')}")
            continue
        valid_gaps.append(gap)

    # Sort by severity (high first), then by pillar priority
    valid_gaps.sort(key=lambda g: (
        0 if g.get("severity") == "high" else 1,
        _PILLAR_PRIORITY.get(g.get("pillar"), 99),
    ))

    # Cap at MAX_REVIEW_GAPS — overflow gaps become enrichments
    overflow_gaps = valid_gaps[MAX_REVIEW_GAPS:]
    valid_gaps = valid_gaps[:MAX_REVIEW_GAPS]

    # ─── Parse enrichments ───────────────────────────────────────────
    raw_enrichments = data.get("enrichments", [])
    if not isinstance(raw_enrichments, list):
        raw_enrichments = []

    valid_enrichments = []
    for enrichment in raw_enrichments:
        if not isinstance(enrichment, dict):
            continue
        if not all(k in enrichment for k in ("pillar", "category", "detail")):
            logger.warning(f"Discarding malformed enrichment (missing fields): {enrichment}")
            continue
        # Default confidence to medium if missing/invalid
        if enrichment.get("confidence") not in _VALID_CONFIDENCES:
            enrichment["confidence"] = "medium"
        # Accept enrichment even if category is non-standard
        valid_enrichments.append(enrichment)

    # Convert overflow gaps to enrichments
    for gap in overflow_gaps:
        valid_enrichments.append({
            "pillar": "description",
            "category": "scope_clarification",
            "detail": f"{gap.get('gap', '')} (Suggested question: {gap.get('suggested_question', '')})",
            "confidence": "medium",
        })

    # Cap enrichments
    valid_enrichments = valid_enrichments[:MAX_REVIEW_ENRICHMENTS]

    return valid_gaps, valid_enrichments
