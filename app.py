# =============================================================================
# NetSuite Requirement Gatekeeper — Main App
# =============================================================================
# Slack Bolt application that handles:
#   1. /netsuite-new-change slash command  → starts a new interview thread
#   2. Thread replies               → continues the interview via Claude
#
# Uses PostgreSQL for state management and the Anthropic SDK for Claude calls.
#
# Phase 1 additions:
#   - Pillar extraction (Haiku) runs every gathering turn
#   - Solution review gate (Sonnet) runs once when all 4 pillars are captured
#   - Review gaps → BSA asks follow-up questions
#   - Review enrichments → injected into AC drafting + Jira ticket

import atexit
import json
import logging
import os

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from claude_client import (
    EscalateResponse,
    MessageResponse,
    SubmitTicketResponse,
    call_claude,
)
from config import (
    MAX_CONVERSATION_TURNS,
    STATUS_ESCALATED,
    STATUS_INTERVIEW,
    STATUS_PROCESSING,
    STATUS_READY,
    TRIAGE_CHANNEL_ID,
    validate_config,
)
from database import (
    InterviewState,
    close_pool,
    create_state,
    get_state,
    init_db,
    try_lock_state,
    update_state,
)
from identity import UserIdentity, resolve_user_identity
from jira_client import create_jira_ticket
from prompt_builder import detect_phase
from reviewer import core_pillars_ready, extract_pillars, merge_pillars, run_review_gate

# ─── Setup ───────────────────────────────────────────────────────────────────

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Validate all required env vars before touching any external service.
# Raises ValueError with a clear list of what's missing.
validate_config()

# Cache the API key once — it's validated above so this will never be empty.
_ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# Initialize database on startup
init_db()

# Graceful connection pool shutdown on process exit
atexit.register(close_pool)


# =============================================================================
# Slash Command: /netsuite-new-change
# =============================================================================

@app.command("/netsuite-new-change")
def handle_slash_command(ack, command, client, respond):
    """Start a new interview when a user runs /netsuite-new-change."""
    ack()

    channel_id = command["channel_id"]
    user_id = command["user_id"]
    text = command.get("text", "").strip()

    # Post a root message to anchor the thread
    try:
        root_msg = client.chat_postMessage(
            channel=channel_id,
            text="👋 Hi! I'm the NetSuite Gatekeeper. Let me get your Jira ticket started...",
        )
    except Exception as e:
        if "channel_not_found" in str(e):
            respond(
                "⚠️ I cannot post here because I haven't been invited to this channel yet! "
                "Please invite me by typing `/invite @NetSuite Gatekeeper` and try again."
            )
            return

        logger.error(f"Failed to post root message: {e}")
        return

    thread_ts = root_msg["ts"]

    # Resolve user identity
    identity = resolve_user_identity(user_id, client)

    # Create initial state
    state = InterviewState(
        thread_id=thread_ts,
        channel_id=channel_id,
        user_id=user_id,
        user_email=identity.email or "",
        user_jira_id=identity.jira_account_id or "",
        user_display_name=identity.display_name,
        status=STATUS_INTERVIEW,
    )
    create_state(state)

    # Build the first user message for Claude
    user_content = "Hi, I'd like to create a new NetSuite requirement ticket."
    if text:
        user_content += f" Here's what I need: {text}"

    history = [{"role": "user", "content": user_content}]

    # Call Claude for the first response
    _run_interview_turn(
        client=client,
        channel_id=channel_id,
        thread_ts=thread_ts,
        user_id=user_id,
        state=state,
        history=history,
    )


# =============================================================================
# Message Event: Thread Replies
# =============================================================================

@app.event("message")
def handle_message(event, client):
    """Handle replies in active interview threads."""
    # Only process threaded replies
    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return

    # Ignore bot messages (prevent loops)
    if event.get("bot_id") or event.get("subtype"):
        return

    channel_id = event.get("channel", "")
    user_id = event.get("user", "")
    user_message = event.get("text", "")

    if not user_message.strip():
        # If the user uploaded a file without text, let them know
        if event.get("files"):
            # Only respond if this is an active interview thread
            state = get_state(thread_ts)
            if state is not None:
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=(
                        "I see you attached a file! I can only read text at the moment. "
                        "Could you type out what you need instead?"
                    ),
                )
        return

    # Look up existing interview state
    state = get_state(thread_ts)
    if state is None:
        # Not an active interview thread — ignore
        return

    # Guard: already done or escalated (no lock needed)
    if state.status == STATUS_READY:
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=(
                "This interview has already been completed and a Jira ticket was created. "
                "Run `/netsuite-new-change` to start a new request."
            ),
        )
        return

    if state.status == STATUS_ESCALATED:
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=(
                "This request has been escalated to the team. "
                "Run `/netsuite-new-change` to start a new request."
            ),
        )
        return

    # Atomic lock: only proceed if status transitions from INTERVIEW → PROCESSING
    # This prevents race conditions when two messages arrive close together.
    if not try_lock_state(thread_ts, STATUS_INTERVIEW, STATUS_PROCESSING):
        logger.info(f"Thread {thread_ts} could not acquire lock (status is not INTERVIEW). Skipping.")
        return

    # Re-read state after acquiring lock to get fresh data
    state = get_state(thread_ts)
    if state is None:
        return

    # Build conversation history from stored state
    history = state.get_history()
    history.append({"role": "user", "content": user_message})

    # ─── Enforce conversation turn limit ─────────────────────────────
    user_turn_count = sum(1 for msg in history if msg["role"] == "user")

    if user_turn_count > MAX_CONVERSATION_TURNS:
        logger.info(
            f"Thread {thread_ts} exceeded {MAX_CONVERSATION_TURNS} user turns. "
            f"Forcing escalation."
        )
        _force_escalation(
            client=client,
            channel_id=channel_id,
            thread_ts=thread_ts,
            user_id=user_id,
            state=state,
            history=history,
            reason=(
                f"Conversation exceeded {MAX_CONVERSATION_TURNS} turns without "
                f"reaching a verified ticket. Auto-escalated."
            ),
        )
        return

    _run_interview_turn(
        client=client,
        channel_id=channel_id,
        thread_ts=thread_ts,
        user_id=user_id,
        state=state,
        history=history,
    )


# =============================================================================
# Core Interview Turn
# =============================================================================

def _run_interview_turn(
    client,
    channel_id: str,
    thread_ts: str,
    user_id: str,
    state: InterviewState,
    history: list[dict],
) -> None:
    """
    Execute one turn of the interview loop:
      1. Set PROCESSING lock (already done by caller for message events)
      2. Post "Thinking..." placeholder
      3. Run pillar extraction (if in gathering phase)
      4. Run review gate (if pillars are ready and review hasn't run)
      5. Call Claude BSA with full history + phase context
      6. Overwrite placeholder with response (message / submit / escalate)
      7. Update state
    """
    # Ensure we're in PROCESSING status (slash command path may not have locked)
    if state.status != STATUS_PROCESSING:
        update_state(thread_ts, status=STATUS_PROCESSING)

    # ─── Post placeholder message ────────────────────────────────────
    try:
        placeholder_msg = client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="⏳ _Thinking..._",
        )
        placeholder_ts = placeholder_msg["ts"]
    except Exception as e:
        logger.error(f"Failed to post placeholder message: {e}")
        update_state(thread_ts, status=STATUS_INTERVIEW)
        return

    try:
        # ─── Step 1: Pillar Extraction (every gathering turn) ────────
        phase = detect_phase(history, state)

        if phase == "gathering":
            extraction = extract_pillars(history, _ANTHROPIC_API_KEY)
            if extraction.success:
                existing_pillars = state.get_pillars()
                merged = merge_pillars(existing_pillars, extraction.pillars)
                update_state(thread_ts, pillars_json=json.dumps(merged))
                # Update local state object for phase re-check
                state.pillars_json = json.dumps(merged)

                # Re-check phase — pillars may now be complete
                phase = detect_phase(history, state)

        # ─── Step 2: Review Gate (once, when pillars are ready) ──────
        if phase == "review":
            # Update placeholder to show progress
            try:
                client.chat_update(
                    channel=channel_id,
                    ts=placeholder_ts,
                    text="⏳ _Reviewing details..._",
                )
            except Exception:
                pass

            pillars = state.get_pillars()
            review = run_review_gate(pillars, history, _ANTHROPIC_API_KEY)

            # Store review results
            review_turn_index = len(history)  # Index at which review happened
            update_state(
                thread_ts,
                review_completed=True,
                review_gaps_json=json.dumps(review.gaps),
                review_enrichments_json=json.dumps(review.enrichments),
                review_turn_index=review_turn_index,
            )

            # Update local state object
            state.review_completed = True
            state.review_gaps_json = json.dumps(review.gaps)
            state.review_enrichments_json = json.dumps(review.enrichments)
            state.review_turn_index = review_turn_index

            # Re-check phase after review
            phase = detect_phase(history, state)

        # ─── Step 3: BSA Response (normal Claude call) ───────────────
        # Pass the already-computed phase so assemble_prompt doesn't re-detect.
        result = call_claude(history, _ANTHROPIC_API_KEY, state=state, phase=phase)

        # ─── Route: Text Message ─────────────────────────────────────
        if isinstance(result, MessageResponse):
            history.append({"role": "assistant", "content": result.text})

            client.chat_update(
                channel=channel_id,
                ts=placeholder_ts,
                text=result.text,
            )

            update_state(
                thread_ts,
                status=STATUS_INTERVIEW,
                message_history=json.dumps(history),
            )

        # ─── Route: Submit Ticket ────────────────────────────────────
        elif isinstance(result, SubmitTicketResponse):
            client.chat_update(
                channel=channel_id,
                ts=placeholder_ts,
                text="✅ _Creating your Jira ticket..._",
            )

            identity = UserIdentity(
                slack_user_id=user_id,
                email=state.user_email or None,
                display_name=state.user_display_name or "Unknown User",
                jira_account_id=state.user_jira_id or None,
            )

            # Pass enrichments to Jira for Implementation Notes section
            enrichments = state.get_review_enrichments()

            jira_result = create_jira_ticket(result, identity, enrichments=enrichments or None)

            if jira_result.success:
                success_text = (
                    f"🎉 Jira ticket created successfully!\n"
                    f"*{jira_result.issue_key}*: {jira_result.issue_url}\n\n"
                    f"The ticket has been populated with:\n"
                    f"• Description\n• Value to the Business\n"
                    f"• Acceptance Criteria (Given/When/Then)\n• Enablement Plan"
                    + ("\n• Implementation Notes (from solution review)" if enrichments else "")
                )
                client.chat_update(
                    channel=channel_id,
                    ts=placeholder_ts,
                    text=success_text,
                )
                # Append assistant reply so history is complete before saving.
                history.append({"role": "assistant", "content": success_text})
                update_state(
                    thread_ts,
                    status=STATUS_READY,
                    message_history=json.dumps(history),
                    pillars_json=json.dumps({
                        "title": result.title,
                        "description": result.description,
                        "value_to_business": result.value_to_business,
                        "acceptance_criteria": result.acceptance_criteria,
                        "enablement_plan": result.enablement_plan,
                    }),
                )
            else:
                # jira_result.error contains the sanitised message from jira_client;
                # raw API response bodies are never surfaced here.
                failure_text = (
                    "⚠️ Your ticket was verified but Jira creation failed. "
                    "Your data has been saved — please contact your admin."
                )
                client.chat_update(
                    channel=channel_id,
                    ts=placeholder_ts,
                    text=failure_text,
                )
                logger.error(f"Jira creation failed for thread {thread_ts}: {jira_result.error}")
                history.append({"role": "assistant", "content": failure_text})
                update_state(
                    thread_ts,
                    status=STATUS_INTERVIEW,
                    message_history=json.dumps(history),
                )

        # ─── Route: Escalate ─────────────────────────────────────────
        elif isinstance(result, EscalateResponse):
            escalation_text = (
                "I've connected you with the team for additional help. "
                "A business analyst will follow up with you shortly."
            )
            history.append({"role": "assistant", "content": escalation_text})
            _post_escalation(
                client=client,
                channel_id=channel_id,
                thread_ts=thread_ts,
                user_id=user_id,
                display_name=state.user_display_name,
                reason=result.reason,
                partial_data=result.partial_data,
                placeholder_ts=placeholder_ts,
                message_history=history,
            )

    except Exception as e:
        logger.exception(f"Error in interview turn for thread {thread_ts}")
        try:
            client.chat_update(
                channel=channel_id,
                ts=placeholder_ts,
                text="Something went wrong on my end. Could you repeat your last message?",
            )
        except Exception:
            pass
        # Save history even on error so the user's last message isn't lost
        update_state(
            thread_ts,
            status=STATUS_INTERVIEW,
            message_history=json.dumps(history),
        )


# =============================================================================
# Escalation Helpers
# =============================================================================

def _force_escalation(
    client,
    channel_id: str,
    thread_ts: str,
    user_id: str,
    state: InterviewState,
    history: list[dict],
    reason: str,
) -> None:
    """Force-escalate when the app-level turn limit is exceeded."""
    partial_data = state.get_pillars()

    client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=(
            "This one seems complex — let me escalate to a BA who can "
            "hop on a quick call with you."
        ),
    )

    # _post_escalation owns all state writes; pass history so it's saved once.
    _post_escalation(
        client=client,
        channel_id=channel_id,
        thread_ts=thread_ts,
        user_id=user_id,
        display_name=state.user_display_name,
        reason=reason,
        partial_data=partial_data,
        message_history=history,
    )


def _post_escalation(
    client,
    channel_id: str,
    thread_ts: str,
    user_id: str,
    display_name: str,
    reason: str,
    partial_data: dict,
    placeholder_ts: str | None = None,
    message_history: list[dict] | None = None,
) -> None:
    """
    Post escalation messages to the triage channel and user thread,
    then write the final ESCALATED state in a single update_state call.

    Args:
        message_history: Full conversation history (including the final
                         assistant message) to persist alongside the escalation.
    """
    # Post to triage channel (graceful failure if bot not invited)
    try:
        client.chat_postMessage(
            channel=TRIAGE_CHANNEL_ID,
            text=(
                f"🔔 *Escalation from NetSuite Gatekeeper*\n\n"
                f"*Requester:* <@{user_id}> ({display_name})\n"
                f"*Reason:* {reason}\n\n"
                f"Thread: <#{channel_id}>"
            ),
        )
    except Exception as e:
        logger.warning(
            f"⚠️ Could not post to Triage Channel ({TRIAGE_CHANNEL_ID}). "
            f"Check your .env file and ensure the bot is invited to the channel! "
            f"Error: {e}"
        )

    # Notify user — overwrite placeholder if available, otherwise post new message
    user_text = (
        "I've connected you with the team for additional help. "
        "A business analyst will follow up with you shortly."
    )

    if placeholder_ts:
        client.chat_update(
            channel=channel_id,
            ts=placeholder_ts,
            text=user_text,
        )
    else:
        client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=user_text,
        )

    # Single state write — include message_history when the caller provides it.
    state_updates: dict = {
        "status": STATUS_ESCALATED,
        "pillars_json": json.dumps(partial_data or {}),
    }
    if message_history is not None:
        state_updates["message_history"] = json.dumps(message_history)
    update_state(thread_ts, **state_updates)


# =============================================================================
# Entry Point
# =============================================================================

if __name__ == "__main__":
    app_token = os.environ.get("SLACK_APP_TOKEN")
    if not app_token:
        raise ValueError(
            "SLACK_APP_TOKEN not set. "
            "Generate an app-level token with connections:write scope."
        )

    logger.info("⚡ NetSuite Gatekeeper bot starting (Socket Mode)...")
    handler = SocketModeHandler(app, app_token)
    handler.start()
