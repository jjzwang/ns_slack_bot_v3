# =============================================================================
# Prompt Builder — Directive + Full Playbook + Gap/Enrichment Context
# =============================================================================
# Assembles the system prompt per turn by combining:
#   1. PHASE DIRECTIVE   — short focus instruction for this turn (prepended)
#   2. FULL PROMPT       — complete playbook, always loaded (reference)
#   3. GAP DIRECTIVE     — review-found gaps, injected during gap follow-up
#   4. ENRICHMENT CONTEXT — reviewer insights, injected during drafting
#
# Phase detection uses actual pillar state (from extraction) instead of
# heuristic turn-count / keyword markers.

import json
import logging
from typing import Optional

from config import FULL_PROMPT, PHASE_DIRECTIVES
from database import InterviewState

logger = logging.getLogger(__name__)


# ─── Markers for content-based detection ─────────────────────────────────────

_VERIFY_MARKERS = ["📋", "Is this correct?", "Yes / Edit", "(Yes / Edit)", "Does this look right?"]
_AC_MARKERS = ["GIVEN", "WHEN", "THEN", "Given", "When", "Then"]


# =============================================================================
# Phase Detection — State-Based
# =============================================================================

def detect_phase(history: list[dict], state: InterviewState) -> str:
    """
    Determine the current conversation phase from actual pillar state.

    Replaces heuristic-based detection (turn count, keyword markers)
    with real state checks against extracted pillar data.

    Returns:
        "gathering" | "gathering_with_gaps" | "drafting" | "verify"
    """
    pillars = state.get_pillars()

    # ─── Check if core pillars are captured ──────────────────────────
    core_pillars_ready = all(
        pillars.get(p) is not None
        for p in ("persona", "action", "goal", "business_value")
    )

    if not core_pillars_ready:
        return "gathering"

    # ─── Review gate ─────────────────────────────────────────────────
    # If review hasn't run yet, signal that it should
    if not state.is_review_completed:
        return "review"

    # ─── Gap follow-up ───────────────────────────────────────────────
    # If review found gaps, check if BSA has moved past them
    if state.has_review_gaps():
        post_review_messages = _get_messages_after_review(history, state)
        if not _contains_ac_markers(post_review_messages):
            return "gathering_with_gaps"

    # ─── Drafting / Verify ───────────────────────────────────────────
    if _contains_verify_markers(history):
        return "verify"

    return "drafting"


def _get_messages_after_review(history: list[dict], state: InterviewState) -> list[dict]:
    """
    Get assistant messages that occurred after the review was completed.

    Uses review_turn_index stored in state to slice the history.

    NOTE: This assumes history is append-only. If context-window trimming
    is ever added (deleting oldest messages to save tokens), switch to
    a message-level marker (e.g., a flag on individual message dicts)
    instead of relying on array index position.
    """
    idx = state.review_turn_index
    if idx < 0 or idx >= len(history):
        return []
    # Return only assistant messages after the review point
    return [
        msg for msg in history[idx:]
        if msg.get("role") == "assistant"
    ]


def _contains_ac_markers(messages: list[dict]) -> bool:
    """Check if any messages contain acceptance criteria markers."""
    for msg in messages:
        content = msg.get("content", "")
        marker_count = sum(1 for m in _AC_MARKERS if m in content)
        if marker_count >= 2:
            return True
    return False


def _contains_verify_markers(history: list[dict]) -> bool:
    """Check if the latest assistant messages contain verify markers."""
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if any(marker in content for marker in _VERIFY_MARKERS):
                return True
            # Also check if user is responding to a prior verify
            break

    # Check if user is responding to a prior verify with yes/edit
    last_user = ""
    for msg in reversed(history):
        if msg.get("role") == "user":
            last_user = msg.get("content", "").lower().strip()
            break

    if last_user in ("yes", "y", "yep", "looks good", "correct", "edit", "change"):
        for msg in reversed(history):
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if any(marker in content for marker in _VERIFY_MARKERS):
                    return True
                break

    return False


# =============================================================================
# Gap & Enrichment Formatting
# =============================================================================

def _format_gap_directive(gaps: list[dict]) -> str:
    """
    Format review gaps as a directive for the BSA prompt.

    Injected during the gathering_with_gaps phase so the BSA asks
    about gaps in its normal conversational style.
    """
    if not gaps:
        return ""

    lines = [
        "",
        ">>> REVIEW FINDINGS — ASK THESE BEFORE DRAFTING ACCEPTANCE CRITERIA",
        "",
        "The following gaps were identified during solution review. Ask about the",
        "highest-priority unanswered gap in your next message. Follow your normal",
        "conversational style — do not mention that a \"review\" happened.",
        "",
        "Once all gaps are addressed in the conversation, proceed to draft",
        "acceptance criteria.",
        "",
    ]

    for i, gap in enumerate(gaps, 1):
        severity = gap.get("severity", "medium").upper()
        pillar = gap.get("pillar", "unknown")
        desc = gap.get("gap", "")
        question = gap.get("suggested_question", "")
        lines.append(f"{i}. [{severity}] {pillar.title()} gap: {desc}")
        lines.append(f"   Ask: \"{question}\"")
        lines.append("")

    return "\n".join(lines)


def _format_enrichment_context(enrichments: list[dict]) -> str:
    """
    Format review enrichments as solution context for the BSA prompt.

    Injected during the drafting phase so AC scenarios incorporate
    the reviewer's findings.
    """
    if not enrichments:
        return ""

    # Category display names
    labels = {
        "implementation_approach": "Implementation Approach",
        "edge_case": "Edge Cases to Cover in AC",
        "native_alternative": "Native Alternative Considered",
        "downstream_impact": "Downstream Impact",
        "compliance_risk": "Compliance & Audit",
        "integration_dependency": "Integration Dependencies",
        "governance_concern": "Governance & Performance",
        "scope_clarification": "Scope Notes",
    }

    # Group by category
    grouped: dict[str, list[str]] = {}
    for e in enrichments:
        category = e.get("category", "other")
        detail = e.get("detail", "")
        if detail:
            grouped.setdefault(category, []).append(detail)

    lines = [
        "",
        ">>> SOLUTION CONTEXT (use when drafting acceptance criteria)",
        "",
    ]

    for category, items in grouped.items():
        label = labels.get(category, category.replace("_", " ").title())
        lines.append(f"{label}:")
        for item in items:
            lines.append(f"  • {item}")
        lines.append("")

    return "\n".join(lines)


# =============================================================================
# Prompt Assembly
# =============================================================================

def assemble_prompt(history: list[dict], state: Optional[InterviewState] = None) -> str:
    """
    Assemble the full system prompt for the current turn.

    Structure:
      1. Phase directive (short, at the top — anchors Claude's focus)
      2. Full playbook (complete reference — always loaded)
      3. Gap directive (if in gap follow-up phase)
      4. Enrichment context (if in drafting phase)

    Args:
        history: Full conversation history
        state: InterviewState for pillar-based phase detection.
               If None, falls back to heuristic detection.

    Returns:
        Assembled system prompt string
    """
    parts = []

    # Determine phase
    if state is not None:
        phase = detect_phase(history, state)
    else:
        phase = _heuristic_phase(history)

    # 1. Phase directive (prepended — first thing Claude reads)
    directive = PHASE_DIRECTIVES.get(phase, PHASE_DIRECTIVES.get("gathering", ""))
    if directive:
        parts.append(directive.strip())

    # 2. Full playbook (always loaded)
    parts.append(FULL_PROMPT.strip())

    # 3. Gap directive (during gap follow-up)
    if state is not None and phase == "gathering_with_gaps":
        gap_directive = _format_gap_directive(state.get_review_gaps())
        if gap_directive:
            parts.append(gap_directive.strip())

    # 4. Enrichment context (during drafting and verify)
    if state is not None and phase in ("drafting", "verify"):
        enrichment_ctx = _format_enrichment_context(state.get_review_enrichments())
        if enrichment_ctx:
            parts.append(enrichment_ctx.strip())

    logger.info(f"Prompt assembly: phase={phase}")

    return "\n\n".join(parts)


# =============================================================================
# Heuristic Fallback (used when state is unavailable)
# =============================================================================

_DRAFTING_THRESHOLD = 4


def _heuristic_phase(history: list[dict]) -> str:
    """
    Fallback phase detection using turn count and keyword markers.

    Used only when InterviewState is not available (e.g., extraction failed
    and no pillar data exists). Matches the original Phase 0 behavior.
    """
    if not history:
        return "gathering"

    last_assistant = ""
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            last_assistant = msg.get("content", "")
            break

    # Verify phase
    if any(marker in last_assistant for marker in _VERIFY_MARKERS):
        return "verify"

    last_user = ""
    for msg in reversed(history):
        if msg.get("role") == "user":
            last_user = msg.get("content", "").lower().strip()
            break

    if last_user in ("yes", "y", "yep", "looks good", "correct", "edit", "change"):
        for msg in reversed(history):
            if msg.get("role") == "assistant" and msg.get("content", "") != last_assistant:
                if any(marker in msg["content"] for marker in _VERIFY_MARKERS):
                    return "verify"
                break

    # Drafting phase
    user_turn_count = sum(1 for m in history if m["role"] == "user")
    for msg in history:
        if msg["role"] == "assistant":
            content = msg.get("content", "")
            ac_count = sum(1 for m in _AC_MARKERS if m in content)
            if ac_count >= 2:
                return "drafting"

    if user_turn_count >= _DRAFTING_THRESHOLD:
        return "drafting"

    return "gathering"
