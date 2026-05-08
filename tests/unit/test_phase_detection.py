# =============================================================================
# Phase detection — characterization test
# =============================================================================
# Locks in the current behavior of prompt_builder.detect_phase. This is the
# state machine the engine uses to choose a system prompt directive each turn.
# If a phase refactor changes any of these transitions, the change must be
# explicit in the PR — either updating these tests with a justification or
# updating CLAUDE.md if the architecture moves.

import json

import pytest

from database import InterviewState
from prompt_builder import detect_phase

# ─── Fixture builders ────────────────────────────────────────────────────────

ALL_PILLARS = {
    "persona": "AP Clerk",
    "action": "Add credit limit validation on Sales Order",
    "goal": "Prevent over-credit orders from being processed",
    "business_value": "Avoid revenue leakage and reduce manual rework",
}

ASSISTANT_VERIFY_MSG = (
    "📋 **Title:** [NetSuite] AP Clerk — Sales Order — Credit Limit Validation\n\n"
    "Here's what will go into Jira. Is this correct? (Yes / Edit)"
)

ASSISTANT_AC_MSG = (
    "Here's how we'd test this:\n"
    "GIVEN a sales order exceeds the customer's credit limit\n"
    "WHEN the user attempts to approve\n"
    "THEN NetSuite blocks approval"
)


def _state(
    pillars: dict | None = None,
    review_completed: bool = False,
    review_gaps: list | None = None,
    review_turn_index: int = -1,
    is_verifying: bool = False,
) -> InterviewState:
    return InterviewState(
        thread_id="t1",
        channel_id="D1",
        user_id="U1",
        pillars_json=json.dumps(pillars or {}),
        review_completed=review_completed,
        review_gaps_json=json.dumps(review_gaps or []),
        review_turn_index=review_turn_index,
        is_verifying=is_verifying,
    )


# ─── Tests ───────────────────────────────────────────────────────────────────


def test_empty_state_returns_gathering():
    assert detect_phase([], _state()) == "gathering"


def test_partial_pillars_returns_gathering():
    pillars = dict(ALL_PILLARS)
    pillars["business_value"] = None
    assert detect_phase([], _state(pillars=pillars)) == "gathering"


def test_all_pillars_no_review_returns_review():
    assert detect_phase([], _state(pillars=ALL_PILLARS)) == "review"


def test_review_complete_no_gaps_no_verify_returns_drafting():
    state = _state(pillars=ALL_PILLARS, review_completed=True)
    assert detect_phase([], state) == "drafting"


def test_review_with_gaps_no_ac_returns_gathering_with_gaps():
    history = [
        {"role": "user", "content": "I want to validate credit limits"},
        {"role": "assistant", "content": "Got it — which subsidiaries?"},
    ]
    state = _state(
        pillars=ALL_PILLARS,
        review_completed=True,
        review_gaps=[{"pillar": "action", "severity": "high", "gap": "...", "suggested_question": "..."}],
        review_turn_index=2,
    )
    assert detect_phase(history, state) == "gathering_with_gaps"


def test_review_with_gaps_but_ac_drafted_falls_through_to_drafting():
    history = [
        {"role": "user", "content": "I want to validate credit limits"},
        {"role": "assistant", "content": "Got it"},
        {"role": "assistant", "content": ASSISTANT_AC_MSG},
    ]
    state = _state(
        pillars=ALL_PILLARS,
        review_completed=True,
        review_gaps=[{"pillar": "action", "severity": "high", "gap": "...", "suggested_question": "..."}],
        review_turn_index=1,
    )
    assert detect_phase(history, state) == "drafting"


def test_is_verifying_flag_returns_verify():
    state = _state(pillars=ALL_PILLARS, review_completed=True, is_verifying=True)
    assert detect_phase([], state) == "verify"


def test_last_assistant_verify_markers_returns_verify():
    history = [
        {"role": "user", "content": "yes please"},
        {"role": "assistant", "content": ASSISTANT_VERIFY_MSG},
    ]
    state = _state(pillars=ALL_PILLARS, review_completed=True)
    assert detect_phase(history, state) == "verify"


@pytest.mark.parametrize(
    "user_reply",
    ["yes", "y", "yep", "looks good", "correct", "edit", "change"],
)
def test_user_confirmation_after_verify_summary_returns_verify(user_reply):
    # User sends affirmation; previous assistant message had verify markers.
    history = [
        {"role": "assistant", "content": ASSISTANT_VERIFY_MSG},
        {"role": "user", "content": user_reply},
    ]
    state = _state(pillars=ALL_PILLARS, review_completed=True)
    assert detect_phase(history, state) == "verify"
