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

# IMPORTANT: config.py calls load_dotenv() at its top before any os.environ.get()
# so we import it first. Do not reorder imports without reading config.py.
from config import (
    MAX_CONVERSATION_TURNS,
    MAX_REVIEW_ATTEMPTS,
    STATUS_ESCALATED,
    STATUS_INTERVIEW,
    STATUS_PROCESSING,
    STATUS_READY,
    TRIAGE_CHANNEL_ID,
    validate_config,
)
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from claude_client import (
    EscalateResponse,
    MessageResponse,
    SubmitTicketResponse,
    call_claude,
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
from prompt_builder import detect_phase, _VERIFY_MARKERS
from reviewer import core_pillars_ready, extract_pillars, merge_pillars, run_review_gate

# ─── Setup ───────────────────────────────────────────────────────────────────

from log_context import ThreadContextFilter, thread_context

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s [thread=%(thread_ts)s]: %(message)s",
)
# Attach the filter to the root logger so EVERY module's logs get thread_ts,
# not just app.py's. This includes claude_client, reviewer, jira_client, etc.
logging.getLogger().addFilter(ThreadContextFilter())
logger = logging.getLogger(__name__)

def _safe_update_or_post(
    client,
    channel_id: str,
    thread_ts: str,
    placeholder_ts: str,
    text: str,
) -> None:
    """
    Update the placeholder message if possible; otherwise post a new thread
    reply. Ensures terminal outcomes (ticket created, ticket failed) always
    reach the user even if the placeholder message can't be edited (e.g.,
    Slack rate limit, message too old, channel archived).
    """
    try:
        client.chat_update(channel=channel_id, ts=placeholder_ts, text=text)
        return
    except Exception as e:
        logger.warning(
            f"chat_update failed for thread {thread_ts} (placeholder {placeholder_ts}): {e}. "
            f"Falling back to chat_postMessage."
        )

    try:
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text)
    except Exception as e:
        # Last resort — log loudly. The user won't see the outcome, but the
        # state is still persisted correctly and a manual follow-up is possible.
        logger.error(
            f"Both chat_update and chat_postMessage failed for thread {thread_ts}: {e}. "
            f"User will not see the terminal message. Text was: {text!r}"
        )

# Validate all required env vars before touching any external service.
# Raises ValueError with a clear list of what's missing.
validate_config()

# Cache the API key once — it's validated above so this will never be empty.
_ANTHROPIC_API_KEY: str = os.environ["ANTHROPIC_API_KEY"]

app = App(token=os.environ["SLACK_BOT_TOKEN"])

# Initialize database on startup.
# Gated behind RUN_MIGRATIONS to prevent concurrent ALTER TABLE calls when
# multiple instances start simultaneously. For pilot (single instance), set
# RUN_MIGRATIONS=1 in the deploy environment. For multi-instance deploys,
# run migrations out-of-band before rollout and leave this unset.
if os.environ.get("RUN_MIGRATIONS", "").lower() in ("1", "true", "yes"):
    logger.info("RUN_MIGRATIONS is set — running init_db()")
    init_db()
else:
    logger.info("Skipping init_db() — set RUN_MIGRATIONS=1 to enable")

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

    # ─── Limit to Direct Messages Only ──────────────────────────────────────────
    # In Slack, Direct Message channel IDs always start with the letter "D"
    if not channel_id.startswith("D"):
        respond(
            text=(
                "🔒 Let's keep your request private and organized! "
                "Please click on my name under **Apps** in your left sidebar "
                "and run this command in a Direct Message with me."
            ),
            response_type="ephemeral"
        )
        return
    # ────────────────────────────────────────────────────────────────────────────

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

    with thread_context(thread_ts):
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
            status=STATUS_PROCESSING,
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
    
    # Ignore bot messages (prevent loops) and system events
    if event.get("bot_id") or event.get("subtype"):
        return

    channel_id = event.get("channel", "")
    thread_ts = event.get("thread_ts")
    
    # ─── UX Enhancement: Nudge user if they aren't using threads ────────────────
    if not thread_ts:
        if channel_id.startswith("D"):
            client.chat_postMessage(
                channel=channel_id,
                text="💡 To start a new request, please type `/netsuite-new-change`. To continue an existing request, please reply directly in its thread!"
            )
        return
    # ────────────────────────────────────────────────────────────────────────────

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
    
    with thread_context(thread_ts):
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
            try:
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
            except Exception:
                logger.exception(f"Force escalation failed for thread {thread_ts}. Resetting to INTERVIEW.")
                update_state(thread_ts, status=STATUS_INTERVIEW)
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
    Execute one turn of the interview loop. The caller is responsible for
    transitioning state to PROCESSING before invoking this function.

    1. Post "Thinking..." placeholder
    2. Run pillar extraction (if in gathering phase)
    3. Run review gate (if pillars are ready and review hasn't run)
    4. Call Claude BSA with full history + phase context
    5. Overwrite placeholder with response (message / submit / escalate)
    6. Update state
    """
    assert state.status == STATUS_PROCESSING, (
        f"_run_interview_turn called with state.status={state.status!r}; "
        f"caller must lock the state to PROCESSING first."
    )

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

            if review.success:
                # Store review results and mark review as completed so it
                # doesn't run again on subsequent turns.
                review_turn_index = len(history)
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
            else:
                new_attempts = state.review_attempts + 1
                if new_attempts >= MAX_REVIEW_ATTEMPTS:
                    # Give up — mark review as completed with empty results so the
                    # conversation can proceed without further retries. The ticket
                    # won't have enrichments, but the user won't be stuck eating
                    # 30s of latency on every turn.
                    logger.warning(
                        f"Review gate failed {new_attempts} times for thread {thread_ts}. "
                        f"Giving up and proceeding without enrichments. Error: {review.error}"
                    )
                    update_state(
                        thread_ts,
                        review_completed=True,
                        review_gaps_json=json.dumps([]),
                        review_enrichments_json=json.dumps([]),
                        review_turn_index=len(history),
                        review_attempts=new_attempts,
                    )
                    state.review_completed = True
                    state.review_gaps_json = "[]"
                    state.review_enrichments_json = "[]"
                    state.review_turn_index = len(history)
                    state.review_attempts = new_attempts
                else:
                    logger.warning(
                        f"Review gate failed for thread {thread_ts} "
                        f"(attempt {new_attempts}/{MAX_REVIEW_ATTEMPTS}). "
                        f"Will retry on next turn. Error: {review.error}"
                    )
                    update_state(thread_ts, review_attempts=new_attempts)
                    state.review_attempts = new_attempts

                # In both cases, proceed to drafting so the conversation keeps moving.
                phase = "drafting"


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

            state_updates = {
                "status": STATUS_INTERVIEW,
                "message_history": json.dumps(history),
            }

            if not getattr(state, "is_verifying", False) and any(marker in result.text for marker in _VERIFY_MARKERS):
                state_updates["is_verifying"] = True
                state.is_verifying = True

            update_state(
                thread_ts,
                **state_updates
            )
            

        # ─── Route: Submit Ticket ────────────────────────────────────
        elif isinstance(result, SubmitTicketResponse):
            #Guard against hallucinated/early tool calls
            if not getattr(state, "is_verifying", False):
                logger.warning(f"Claude attempted early submit_ticket in thread {thread_ts}. Rejecting.")
                error_text = "I have all the details I need! Let me summarize them for you to review before we submit."
                history.append({"role": "assistant", "content": error_text})
                client.chat_update(channel=channel_id, ts=placeholder_ts, text=error_text)
                update_state(thread_ts, status=STATUS_INTERVIEW, message_history=json.dumps(history))
                return
            
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
                _safe_update_or_post(
                    client, channel_id, thread_ts, placeholder_ts, success_text
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
                _safe_update_or_post(
                    client, channel_id, thread_ts, placeholder_ts, failure_text
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
        error_text = "Something went wrong on my end. Could you repeat your last message?"
        try:
            client.chat_update(
                channel=channel_id,
                ts=placeholder_ts,
                text=error_text,
            )
        except Exception:
            pass

        history.append({"role": "assistant", "content": error_text})
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

    # _post_escalation owns the single user-facing message and all state writes.
    # Do NOT post a separate message here — that would produce a double message
    # since _post_escalation also posts when placeholder_ts is None.
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

    # Notify user — overwrite placeholder if available, otherwise post new message.
    # Wrapped in try/except so a transient Slack error doesn't prevent the state
    # write below; without this guard, the triage channel would be notified but the
    # row would stay at STATUS_PROCESSING (or be reset to INTERVIEW by the outer
    # except in _run_interview_turn), creating an inconsistency.
    user_text = (
        "This one seems complex — let me escalate to a BA who can "
        "hop on a quick call with you. A business analyst will follow up shortly."
    )

    try:
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
    except Exception as e:
        logger.warning(
            f"Could not deliver escalation message to user in thread {thread_ts}: {e}. "
            f"State will still be written as ESCALATED."
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