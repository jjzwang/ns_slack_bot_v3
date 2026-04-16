# =============================================================================
# Prompt Builder — Directive + Full Playbook + Gap/Enrichment Context
# =============================================================================

import logging
from typing import Optional

from config import FULL_PROMPT, PHASE_DIRECTIVES
from database import InterviewState

logger = logging.getLogger(__name__)

_VERIFY_MARKERS = ["📋", "Is this correct?", "Yes / Edit", "(Yes / Edit)", "Does this look right?"]
_AC_MARKERS = ["GIVEN", "WHEN", "THEN", "Given", "When", "Then"]


def detect_phase(history: list[dict], state: InterviewState) -> str:
    """
    Determine the current conversation phase from actual pillar state.
    Returns: "gathering" | "review" | "gathering_with_gaps" | "drafting" | "verify"
    """
    pillars = state.get_pillars()
    core_pillars_ready = all(
        pillars.get(p) is not None for p in ("persona", "action", "goal", "business_value")
    )
    if not core_pillars_ready:
        return "gathering"
    if not state.is_review_completed:
        return "review"
    if state.has_review_gaps():
        post_review_messages = _get_messages_after_review(history, state)
        if not _contains_ac_markers(post_review_messages):
            return "gathering_with_gaps"
    if getattr(state, "is_verifying", False):
        return "verify"
    if _contains_verify_markers(history):
        return "verify"
    return "drafting"


def _get_messages_after_review(history: list[dict], state: InterviewState) -> list[dict]:
    idx = state.review_turn_index
    if idx < 0 or idx >= len(history):
        return []
    return [msg for msg in history[idx:] if msg.get("role") == "assistant"]


def _contains_ac_markers(messages: list[dict]) -> bool:
    for msg in messages:
        content = msg.get("content", "")
        if sum(1 for m in _AC_MARKERS if m in content) >= 2:
            return True
    return False


def _contains_verify_markers(history: list[dict]) -> bool:
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")
            if any(marker in content for marker in _VERIFY_MARKERS):
                return True
            break
    last_user = ""
    for msg in reversed(history):
        if msg.get("role") == "user":
            last_user = msg.get("content", "").lower().strip()
            break
    if last_user in ("yes", "y", "yep", "looks good", "correct", "edit", "change"):
        for msg in reversed(history):
            if msg.get("role") == "assistant":
                if any(marker in msg.get("content", "") for marker in _VERIFY_MARKERS):
                    return True
                break
    return False


def _format_gap_directive(gaps: list[dict]) -> str:
    if not gaps:
        return ""
    lines = [
        "",
        ">>> REVIEW FINDINGS — ASK THESE BEFORE DRAFTING ACCEPTANCE CRITERIA",
        "",
        "The following gaps were identified during solution review. Ask about the",
        "highest-priority unanswered gap in your next message. Follow your normal",
        'conversational style — do not mention that a "review" happened.',
        "",
        "Once all gaps are addressed in the conversation, proceed to draft acceptance criteria.",
        "",
    ]
    for i, gap in enumerate(gaps, 1):
        severity = gap.get("severity", "medium").upper()
        pillar = gap.get("pillar", "unknown")
        desc = gap.get("gap", "")
        question = gap.get("suggested_question", "")
        lines.append(f"{i}. [{severity}] {pillar.title()} gap: {desc}")
        lines.append(f'   Ask: "{question}"')
        lines.append("")
    return "\n".join(lines)


def _format_enrichment_context(enrichments: list[dict]) -> str:
    if not enrichments:
        return ""
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
    grouped: dict[str, list[str]] = {}
    for e in enrichments:
        category = e.get("category", "other")
        detail = e.get("detail", "")
        if detail:
            grouped.setdefault(category, []).append(detail)
    lines = ["", ">>> SOLUTION CONTEXT (use when drafting acceptance criteria)", ""]
    for category, items in grouped.items():
        label = labels.get(category, category.replace("_", " ").title())
        lines.append(f"{label}:")
        for item in items:
            lines.append(f"  • {item}")
        lines.append("")
    return "\n".join(lines)


def assemble_prompt(
    history: list[dict],
    state: Optional[InterviewState] = None,
    phase: Optional[str] = None,
) -> str:
    """
    Assemble the full system prompt for the current turn.

    Args:
        phase: Pre-computed phase from the caller. When provided, detect_phase
               is skipped entirely — avoids redundant computation and guarantees
               the prompt matches the routing decision already made upstream.
               NOTE: "review" is handled in app.py before call_claude is invoked
               and is never a valid value here.
    """
    parts = []

    if phase is None:
        phase = detect_phase(history, state) if state is not None else _heuristic_phase(history)

    directive = PHASE_DIRECTIVES.get(phase, PHASE_DIRECTIVES.get("gathering", ""))
    if directive:
        parts.append(directive.strip())

    parts.append(FULL_PROMPT.strip())

    if state is not None and phase == "gathering_with_gaps":
        gap_directive = _format_gap_directive(state.get_review_gaps())
        if gap_directive:
            parts.append(gap_directive.strip())

    if state is not None and phase in ("drafting", "verify"):
        enrichment_ctx = _format_enrichment_context(state.get_review_enrichments())
        if enrichment_ctx:
            parts.append(enrichment_ctx.strip())

    logger.info(f"Prompt assembly: phase={phase}")
    return "\n\n".join(parts)


_DRAFTING_THRESHOLD = 4


def _heuristic_phase(history: list[dict]) -> str:
    if not history:
        return "gathering"
    last_assistant = ""
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            last_assistant = msg.get("content", "")
            break
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
    for msg in history:
        if msg["role"] == "assistant":
            if sum(1 for m in _AC_MARKERS if m in msg.get("content", "")) >= 2:
                return "drafting"
    user_turn_count = sum(1 for m in history if m["role"] == "user")
    if user_turn_count >= _DRAFTING_THRESHOLD:
        return "drafting"
    return "gathering"
