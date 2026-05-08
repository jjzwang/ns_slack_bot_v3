# Regression Checklist

This is the manual safety net for the refactor. Walk through every item below
against a live dev bot before merging any phase branch. The behavior described
here is the **pre-refactor baseline** — Phases 1–3 must not change what the
user sees. If a phase deliberately changes behavior, update this file in the
same PR.

Source: derived from `app.py` and `prompt_builder.py` at the
`pre-refactor-baseline` tag. Line numbers are approximate.

## Setup before walking the checklist

- [ ] `.env` populated against a dev workspace (separate Slack app, separate
      Postgres DB, separate Jira project)
- [ ] `RUN_MIGRATIONS=1` for the first run on a fresh DB
- [ ] Bot started: `python app.py`. Log line `⚡ NetSuite Gatekeeper bot
      starting (Socket Mode)...` appears
- [ ] Bot is invited to the triage channel referenced by `TRIAGE_CHANNEL_ID`

## 1. Startup & configuration

- [ ] Removing any required env var (`SLACK_BOT_TOKEN`, `ANTHROPIC_API_KEY`,
      `JIRA_BASE_URL`, `JIRA_PROJECT_KEY`, `JIRA_USER_EMAIL`, `JIRA_API_TOKEN`,
      `TRIAGE_CHANNEL_ID`, `DATABASE_URL` or `PGPASSWORD`) causes
      `validate_config` to raise `ValueError` listing every missing var, and
      the bot does not start
- [ ] Bot connects to Slack and Postgres without error on a clean start

## 2. Slash command — happy entry

- [ ] In a **direct message** to the bot, run `/netsuite-new-change`. Bot posts
      a root message ("👋 Hi! I'm the NetSuite Gatekeeper…"), then a follow-up
      asking what kind of NetSuite change is needed
- [ ] `/netsuite-new-change add a credit limit warning to sales orders` (slash
      with text) — bot extracts the action from the text and asks about the
      next missing pillar rather than re-asking what the change is

## 3. Slash command — guardrails

- [ ] Run `/netsuite-new-change` in a **public channel** (not a DM): bot
      replies ephemerally telling the user to run it in a DM. No thread is
      created
- [ ] Run `/netsuite-new-change` in a channel where the bot is not invited:
      `respond()` ephemeral message tells the user to invite the bot. No crash

## 4. Reply routing

- [ ] Reply directly to the bot in a DM **outside any thread**: bot posts the
      "💡 To start a new request…" nudge
- [ ] Send a message in a thread that is not an active interview: bot ignores
      it (no reply, no log noise beyond the lookup)
- [ ] Send a file-only message (no text) inside an active interview thread:
      bot posts "I see you attached a file…" reminder
- [ ] Send a file-only message in a non-interview thread: bot ignores it
- [ ] Bot's own messages do not trigger another turn (no `bot_id` loop)

## 5. Concurrency & locking

- [ ] Send two thread replies within ~1 second of each other: only one
      `_run_interview_turn` runs. The second one is silently dropped (log:
      "could not acquire lock")
- [ ] After the bot finishes responding, the next user reply works normally

## 6. Pillar gathering (4-pillar baseline)

- [ ] Through normal conversation, the bot collects: persona, action, goal,
      business_value. Pillars accumulate in `interview_state.pillars_json`
      across turns (verify in DB)
- [ ] Once all four core pillars are non-null, the next turn runs the review
      gate (placeholder shows "⏳ _Reviewing details..._"), then proceeds to
      drafting AC

## 7. Review gate

- [ ] On a thin/vague request, the gate identifies gaps and the bot asks one
      gap question in conversational tone (no mention of "review" to user)
- [ ] On a well-formed request, the gate returns 0 gaps and the bot drafts
      acceptance criteria immediately
- [ ] Review enrichments (when present) are visible in the resulting Jira
      ticket's "Implementation Notes" section
- [ ] If the review call fails, the bot retries once. After
      `MAX_REVIEW_ATTEMPTS` (2) failures, conversation continues without
      enrichments. User-visible turn latency does not balloon on the next
      turn (review is not retried again)

## 8. Verify loop

- [ ] After AC and enablement plan are gathered, the bot presents the full
      summary with the 📋/📝/💼/✅/🚀 markers and asks "Is this correct?
      (Yes / Edit)"
- [ ] DB row for that thread now has `is_verifying = TRUE`
- [ ] User says **"Yes"**: `submit_ticket` tool fires, placeholder updates to
      "✅ _Creating your Jira ticket..._", Jira ticket is created, success
      message lists Description / Value / AC / Enablement (and Implementation
      Notes if enrichments existed). Status flips to `READY`
- [ ] User says **"Edit"**: bot asks what to change, then updates only that
      pillar and re-presents the full summary. Other pillars are not re-asked

## 9. Submit-ticket guard

- [ ] If Claude attempts `submit_ticket` before `is_verifying = TRUE` (a
      hallucinated early submit), the call is rejected. Bot responds "I have
      all the details I need! Let me summarize…" and the conversation
      continues. `READY` is **not** set

## 10. Escalation paths

- [ ] Deliberately give vague/contradictory answers on the same pillar 3+
      times: Claude calls the `escalate` tool. User sees "I've connected you
      with the team…" message. Triage channel receives a notification with
      requester name, reason, and thread link. Status flips to `ESCALATED`
- [ ] Triage channel post: `@<user>` mention resolves, `<#channel_id>` link
      is clickable, reason is human-readable
- [ ] If the bot is **not** invited to the triage channel, escalation still
      transitions state to `ESCALATED` and the user still sees the friendly
      message. Server logs warn that the triage post failed
- [ ] Force the conversation past `MAX_CONVERSATION_TURNS` (15 user
      messages): force-escalation fires automatically. Triage channel
      notified, status `ESCALATED`. **No double message** to the user (only
      one escalation message, not one from `_force_escalation` plus one from
      `_post_escalation`)

## 11. Out-of-scope handling

- [ ] Ask for something unrelated to NetSuite (e.g., "I need a Slack channel
      created"): Claude redirects politely and **does not** call `escalate`.
      Status stays `INTERVIEW` for the moment, no triage notification

## 12. Terminal-state replies

- [ ] Reply in a thread that is already `READY`: bot says "This interview has
      already been completed…" — no new turn, no Claude call
- [ ] Reply in a thread that is already `ESCALATED`: bot says "This request
      has been escalated to the team…" — no new turn

## 13. Jira creation outcomes

- [ ] Successful create: ticket has `summary`, `description` (with attribution
      line + user story), `value_to_business`, `acceptance_criteria` (ordered
      list with bold items), `enablement_plan`, configured defaults, and
      reporter set when the requester's email resolves to a Jira account
- [ ] Identity resolution failure (Slack email missing or no Jira match):
      ticket still creates, attribution line uses display name + Slack ID,
      reporter is the bot account
- [ ] Jira returns 4xx (e.g., bad project key in test): user sees "Your
      ticket was verified but Jira creation failed. Your data has been saved
      — please contact your admin." Status reset to `INTERVIEW`. Sanitized
      error in the user message; full body in server logs only
- [ ] Jira returns 5xx: 2 retries with backoff (2s, 4s) before surfacing the
      same friendly failure message
- [ ] Network error to Jira: same retry/backoff path
- [ ] On any Jira failure, the original `message_history` is still persisted
      so the user can retry by replying again

## 14. State recovery

- [ ] Mid-interview, restart the bot process (`Ctrl-C`, `python app.py`).
      Reply in the open thread: bot picks up where it left off, the previous
      conversation history is loaded from Postgres, no state is lost
- [ ] Mid-interview, kill the bot uncleanly (`kill -9` / Task Manager). Same
      check: thread resumes cleanly. The DB row was never left in
      `PROCESSING` long enough to block (or if it was, document the recovery
      story)

## 15. Logging & observability (current baseline)

- [ ] Every log line includes `[thread=<thread_ts>]` once a thread context is
      bound. Lines from before any thread (startup, validate_config, init_db)
      show `[thread=-]`
- [ ] `pillar_extraction_complete` log fires on every gathering turn with
      `pillars_populated` / `pillars_missing` / `extraction_duration_ms`
- [ ] `review_gate_complete` log fires once per thread when the gate succeeds,
      with `gaps_count` / `gaps_severity` / `enrichments_count`
- [ ] Every Claude call (interview, extraction, review gate) emits a
      `claude_call` log line via `log_context.log_claude_call(...)` with:
      domain, phase, model, prompt, response, latency_ms, input_tokens,
      output_tokens, tenant_id. Verify by tailing the bot logs during one
      interview turn and counting at least one `claude_call` line per Claude
      API hit. Storage to Postgres / Langfuse is a Phase 3 task

---

## How to record results

For each phase merge, copy this file into a sub-page (or PR comment) and tick
boxes. Capture the build SHA / branch and the dev environment used. Anything
that fails: file as a Phase-0 fix, not a Phase-1 task — the contract is that
Phase 1 starts from a fully green checklist.
