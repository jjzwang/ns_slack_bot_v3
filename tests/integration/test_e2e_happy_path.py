# =============================================================================
# E2E happy path — characterization test
# =============================================================================
# Drives _run_interview_turn through the verify → submit_ticket transition
# with a fully scripted environment:
#
#   - Slack client is a MagicMock; we assert what messages were posted
#   - Claude returns a pre-canned SubmitTicketResponse
#   - Jira returns a pre-canned successful JiraCreateResult
#   - Postgres is replaced by an in-memory fake (just a captured-calls list,
#     since _run_interview_turn calls update_state directly)
#
# This test is the regression contract for "verify-loop → ticket created →
# state = READY". Anything Phase 1+ does that breaks this transition will
# fail this test.

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

import app
from claude_client import SubmitTicketResponse
from config import STATUS_INTERVIEW, STATUS_PROCESSING, STATUS_READY
from database import InterviewState
from jira_client import JiraCreateResult

# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def submit_ticket_response() -> SubmitTicketResponse:
    return SubmitTicketResponse(
        title="[NetSuite] AP Clerk — Sales Order — Credit Limit Validation",
        description=(
            "As an AP Clerk\n"
            "I want to block over-credit sales orders\n"
            "So that we avoid revenue leakage"
        ),
        value_to_business="Avoid revenue leakage and reduce manual reconciliation",
        acceptance_criteria=(
            "GIVEN a sales order exceeds credit limit\n"
            "WHEN the user attempts to approve\n"
            "THEN NetSuite blocks approval and shows a warning"
        ),
        enablement_plan="UAT: Jane Doe. Training: No special training required.",
    )


@pytest.fixture
def verifying_state() -> InterviewState:
    """A thread mid-verify-loop: pillars complete, review done, is_verifying."""
    return InterviewState(
        thread_id="1700000000.000100",
        channel_id="D_TEST",
        user_id="U_TEST",
        user_email="ap.clerk@example.com",
        user_jira_id="jira-acct-123",
        user_display_name="AP Clerk",
        status=STATUS_PROCESSING,
        pillars_json=json.dumps(
            {
                "persona": "AP Clerk",
                "action": "Block sales order approval over credit limit",
                "goal": "Stop processing over-credit orders",
                "business_value": "Avoid revenue leakage",
            }
        ),
        message_history=json.dumps(
            [
                {"role": "user", "content": "I want to block over-credit sales orders."},
                {
                    "role": "assistant",
                    "content": "📋 Here's what will go into Jira. Is this correct?",
                },
            ]
        ),
        review_completed=True,
        is_verifying=True,
    )


@pytest.fixture
def patched_app(monkeypatch, submit_ticket_response):
    """Monkeypatch all external calls in `app` so _run_interview_turn runs
    without DB / Anthropic / Jira / Slack network traffic.

    Returns a dict the test can inspect for assertions.
    """
    captured: dict[str, Any] = {
        "update_state_calls": [],
        "create_jira_ticket_calls": [],
        "call_claude_calls": [],
    }

    def fake_update_state(thread_id: str, **updates: Any) -> None:
        captured["update_state_calls"].append({"thread_id": thread_id, **updates})

    def fake_call_claude(history, api_key, state=None, phase=None):
        captured["call_claude_calls"].append({"phase": phase, "history_len": len(history)})
        return submit_ticket_response

    def fake_create_jira_ticket(ticket_data, identity, enrichments=None):
        captured["create_jira_ticket_calls"].append(
            {"ticket_data": ticket_data, "identity": identity, "enrichments": enrichments}
        )
        return JiraCreateResult(
            success=True,
            issue_key="TEST-1234",
            issue_url="https://test.atlassian.net/browse/TEST-1234",
        )

    monkeypatch.setattr(app, "update_state", fake_update_state)
    monkeypatch.setattr(app, "call_claude", fake_call_claude)
    monkeypatch.setattr(app, "create_jira_ticket", fake_create_jira_ticket)

    return captured


# ─── Tests ───────────────────────────────────────────────────────────────────


def test_verify_loop_submit_creates_ticket_and_marks_ready(
    patched_app, verifying_state, submit_ticket_response
):
    """Happy path: state is in verify loop, Claude returns submit_ticket,
    Jira succeeds → state transitions to READY with the ticket data persisted."""
    slack_client = MagicMock()
    slack_client.chat_postMessage.return_value = {"ts": "placeholder-ts"}

    history = verifying_state.get_history()

    app._run_interview_turn(
        client=slack_client,
        channel_id=verifying_state.channel_id,
        thread_ts=verifying_state.thread_id,
        user_id=verifying_state.user_id,
        state=verifying_state,
        history=history,
    )

    # ─── Claude was called once with phase=verify ─────────────────────────
    assert len(patched_app["call_claude_calls"]) == 1
    assert patched_app["call_claude_calls"][0]["phase"] == "verify"

    # ─── Jira was called with the submit_ticket payload ───────────────────
    assert len(patched_app["create_jira_ticket_calls"]) == 1
    jira_call = patched_app["create_jira_ticket_calls"][0]
    assert jira_call["ticket_data"] is submit_ticket_response
    assert jira_call["identity"].slack_user_id == "U_TEST"
    assert jira_call["identity"].email == "ap.clerk@example.com"

    # ─── Final state write was status=READY with all 5 ticket fields ──────
    final_update = patched_app["update_state_calls"][-1]
    assert final_update["thread_id"] == "1700000000.000100"
    assert final_update["status"] == STATUS_READY
    saved_pillars = json.loads(final_update["pillars_json"])
    assert saved_pillars["title"] == submit_ticket_response.title
    assert saved_pillars["description"] == submit_ticket_response.description
    assert saved_pillars["value_to_business"] == submit_ticket_response.value_to_business
    assert saved_pillars["acceptance_criteria"] == submit_ticket_response.acceptance_criteria
    assert saved_pillars["enablement_plan"] == submit_ticket_response.enablement_plan

    # ─── User saw the success message ─────────────────────────────────────
    update_calls = slack_client.chat_update.call_args_list
    success_text = update_calls[-1].kwargs["text"]
    assert "TEST-1234" in success_text
    assert "https://test.atlassian.net/browse/TEST-1234" in success_text


def test_early_submit_before_verifying_is_rejected(
    patched_app, verifying_state, submit_ticket_response
):
    """Submit-ticket guard: if Claude tries to submit before is_verifying=True,
    the call is rejected, an error message is posted, and status reverts to
    INTERVIEW. No Jira call happens."""
    verifying_state.is_verifying = False
    slack_client = MagicMock()
    slack_client.chat_postMessage.return_value = {"ts": "placeholder-ts"}

    app._run_interview_turn(
        client=slack_client,
        channel_id=verifying_state.channel_id,
        thread_ts=verifying_state.thread_id,
        user_id=verifying_state.user_id,
        state=verifying_state,
        history=verifying_state.get_history(),
    )

    # No Jira call should have happened.
    assert patched_app["create_jira_ticket_calls"] == []

    # The state should be reset to INTERVIEW, not READY.
    final_update = patched_app["update_state_calls"][-1]
    assert final_update["status"] == STATUS_INTERVIEW

    # The user should see the "let me summarize" recovery message.
    update_calls = slack_client.chat_update.call_args_list
    recovery_text = update_calls[-1].kwargs["text"]
    assert "summarize" in recovery_text.lower()
