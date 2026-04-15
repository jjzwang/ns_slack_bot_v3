# =============================================================================
# Reviewer — Pillar Extraction + Solution Review Gate
# =============================================================================

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

# ─── Module-level client caches ──────────────────────────────────────────────
# Separate caches for the two timeout profiles used in this module.
# Reusing clients preserves the underlying httpx connection pool.

_extraction_clients: dict[str, anthropic.Anthropic] = {}
_review_clients: dict[str, anthropic.Anthropic] = {}


def _get_extraction_client(api_key: str) -> anthropic.Anthropic:
    if api_key not in _extraction_clients:
        _extraction_clients[api_key] = anthropic.Anthropic(
            api_key=api_key,
            timeout=httpx.Timeout(EXTRACTION_TIMEOUT_S, connect=5.0),
        )
    return _extraction_clients[api_key]


def _get_review_client(api_key: str) -> anthropic.Anthropic:
    if api_key not in _review_clients:
        _review_clients[api_key] = anthropic.Anthropic(
            api_key=api_key,
            timeout=httpx.Timeout(REVIEW_TIMEOUT_S, connect=5.0),
        )
    return _review_clients[api_key]


_PILLAR_PRIORITY = {"action": 0, "persona": 1, "goal": 2, "business_value": 3}
_VALID_SEVERITIES = {"high", "medium"}
_VALID_GAP_PILLARS = {"action", "persona", "goal", "business_value"}
_VALID_ENRICHMENT_PILLARS = {"description", "acceptance_criteria"}
_VALID_CATEGORIES = {
    "implementation_approach", "edge_case", "native_alternative",
    "downstream_impact", "compliance_risk", "integration_dependency",
    "governance_concern", "scope_clarification",
}
_VALID_CONFIDENCES = {"high", "medium", "low"}


@dataclass
class ExtractionResult:
    pillars: dict
    duration_ms: int = 0
    success: bool = True
    error: Optional[str] = None


@dataclass
class ReviewResult:
    gaps: list = field(default_factory=list)
    enrichments: list = field(default_factory=list)
    duration_ms: int = 0
    success: bool = True
    skipped: bool = False
    error: Optional[str] = None


def extract_pillars(message_history: list[dict[str, str]], api_key: str) -> ExtractionResult:
    start = time.monotonic()
    try:
        client = _get_extraction_client(api_key)
        conv_text = _format_conversation(message_history)
        prompt = EXTRACTION_PROMPT.format(conversation_history=conv_text)
        response = client.messages.create(
            model=EXTRACTION_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        pillars = _parse_extraction_json(text)
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info("pillar_extraction_complete", extra={
            "pillars_populated": [k for k, v in pillars.items() if v is not None],
            "pillars_missing": [k for k, v in pillars.items() if v is None],
            "extraction_duration_ms": duration_ms,
        })
        return ExtractionResult(pillars=pillars, duration_ms=duration_ms)
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning("pillar_extraction_skipped", extra={
            "reason": type(e).__name__, "error": str(e), "extraction_duration_ms": duration_ms,
        })
        return ExtractionResult(
            pillars={"persona": None, "action": None, "goal": None, "business_value": None},
            duration_ms=duration_ms, success=False, error=str(e),
        )


def merge_pillars(existing: dict, new_extraction: dict) -> dict:
    merged = dict(existing)
    for key, value in new_extraction.items():
        if value is not None:
            merged[key] = value
    return merged


def core_pillars_ready(pillars: dict) -> bool:
    return all(pillars.get(p) is not None for p in ("persona", "action", "goal", "business_value"))


def run_review_gate(pillars: dict, message_history: list[dict[str, str]], api_key: str) -> ReviewResult:
    start = time.monotonic()
    try:
        client = _get_review_client(api_key)
        prompt = REVIEW_PROMPT.format(
            structured_pillars=json.dumps(pillars, indent=2),
            conversation_history=_format_conversation(message_history),
        )
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        gaps, enrichments = _parse_review_json(text)
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info("review_gate_complete", extra={
            "gaps_count": len(gaps),
            "gaps_severity": [g.get("severity") for g in gaps],
            "enrichments_count": len(enrichments),
            "review_duration_ms": duration_ms,
        })
        return ReviewResult(gaps=gaps, enrichments=enrichments, duration_ms=duration_ms)
    except Exception as e:
        duration_ms = int((time.monotonic() - start) * 1000)
        logger.warning("review_gate_skipped", extra={
            "reason": type(e).__name__, "error": str(e), "review_duration_ms": duration_ms,
        })
        return ReviewResult(duration_ms=duration_ms, success=False, skipped=True, error=str(e))


def _format_conversation(history: list[dict]) -> str:
    return "\n".join(f"{m.get('role','unknown').upper()}: {m.get('content','')}" for m in history)


def _extract_json_object(text: str) -> Optional[dict]:
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if fence_match:
        try:
            data = json.loads(fence_match.group(1).strip())
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        try:
            data = json.loads(text[first_brace:last_brace + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return None


def _parse_extraction_json(text: str) -> dict:
    _empty = {"persona": None, "action": None, "goal": None, "business_value": None}
    data = _extract_json_object(text)
    if data is None:
        logger.warning(f"Failed to parse extraction JSON: {text[:200]}")
        return _empty
    result = {}
    for key in ("persona", "action", "goal", "business_value"):
        val = data.get(key)
        result[key] = val.strip() if isinstance(val, str) and val.strip() else None
    return result


def _parse_review_json(text: str) -> tuple[list[dict], list[dict]]:
    data = _extract_json_object(text)
    if data is None:
        logger.warning(f"Failed to parse review JSON: {text[:300]}")
        return [], []

    raw_gaps = data.get("gaps", []) if isinstance(data.get("gaps"), list) else []
    valid_gaps = []
    for gap in raw_gaps:
        if not isinstance(gap, dict):
            continue
        if not all(k in gap for k in ("pillar", "severity", "gap", "suggested_question")):
            logger.warning(f"Discarding malformed gap: {gap}")
            continue
        if gap.get("severity") not in _VALID_SEVERITIES:
            gap["severity"] = "medium"
        if gap.get("pillar") not in _VALID_GAP_PILLARS:
            logger.warning(f"Discarding gap with invalid pillar: {gap.get('pillar')}")
            continue
        valid_gaps.append(gap)

    valid_gaps.sort(key=lambda g: (
        0 if g.get("severity") == "high" else 1,
        _PILLAR_PRIORITY.get(g.get("pillar"), 99),
    ))
    overflow_gaps = valid_gaps[MAX_REVIEW_GAPS:]
    valid_gaps = valid_gaps[:MAX_REVIEW_GAPS]

    raw_enrichments = data.get("enrichments", []) if isinstance(data.get("enrichments"), list) else []
    valid_enrichments = []
    for enrichment in raw_enrichments:
        if not isinstance(enrichment, dict):
            continue
        if not all(k in enrichment for k in ("pillar", "category", "detail")):
            logger.warning(f"Discarding malformed enrichment: {enrichment}")
            continue
        if enrichment.get("confidence") not in _VALID_CONFIDENCES:
            enrichment["confidence"] = "medium"
        valid_enrichments.append(enrichment)

    for gap in overflow_gaps:
        valid_enrichments.append({
            "pillar": "description",
            "category": "scope_clarification",
            "detail": f"{gap.get('gap', '')} (Suggested question: {gap.get('suggested_question', '')})",
            "confidence": "medium",
        })

    return valid_gaps, valid_enrichments[:MAX_REVIEW_ENRICHMENTS]
