# Migration Plan

This is the working spec for refactoring the current NetSuite-Slack-Jira codebase into the layered architecture defined in `CLAUDE.md`. Each phase is scoped to be a small number of Claude Code sessions. Do not skip ahead — the order matters because later phases depend on abstractions established earlier.

## Guiding principles

- **Behavior preservation first.** Phases 1–3 should not change what the bot does from a user's perspective. Same interviews, same tickets, same Slack UX. We are paying down architectural debt, not adding features.
- **One phase per branch, small commits within.** Each phase merges to main when its acceptance criteria pass. Mid-phase commits should each leave the code in a working state.
- **Tests before refactor where possible.** If a behavior isn't covered by a test, write a characterization test before moving the code. This is the safety net that lets us refactor confidently.
- **Update `CLAUDE.md` when reality diverges from plan.** If a phase reveals the architecture needs adjustment, update the doc in the same PR.

---

## Phase 0 — Baseline & safety net

**Goal:** establish the starting line and the tests that prevent regression during the refactor.

**Tasks:**
1. Tag the current main branch as `pre-refactor-baseline` so we can diff against it later.
2. Inventory current behavior: write a one-page document listing every user-visible feature (slash command, DM flow, gate questions, Jira ticket fields populated). This becomes the manual regression checklist.
3. Add characterization tests for the highest-risk paths:
   - End-to-end happy path (mocked Claude + Slack + Jira): slash command → interview → extraction → gate → ticket.
   - Pillar extraction with a known transcript fixture, asserting the extracted JSON.
   - Gate evaluation with a known pillar set, asserting pass/fail/escalate.
4. Set up `ruff`, `black`, `mypy --strict`, `pytest` in CI if not already present.
5. Confirm logging captures every Claude call (prompt, response, model, latency, tokens). If not, add it now — we cannot refactor LLM-touching code blind.

**Acceptance:**
- CI green on main.
- Manual regression checklist exists and has been walked through once successfully.
- At least three characterization tests passing.
- Every Claude call produces a structured log entry.

**Out of scope:** any architectural changes. Resist the urge.

---

## Phase 1 — Carve out the engine

**Goal:** separate domain-and-transport-agnostic logic from NetSuite/Slack-specific logic, without yet introducing formal interfaces.

**Tasks:**
1. Create the target directory structure (`engine/`, `domains/netsuite/`, `transports/slack/`, `outputs/jira/`, `config/`) as empty packages.
2. Move files into their new homes based on what they actually do:
   - State machine, persistence, conversation orchestration → `engine/`.
   - System prompt, pillar definitions, gate rules, ticket field mapping → `domains/netsuite/`.
   - Bolt app, Socket Mode, Block Kit, slash command handlers → `transports/slack/`.
   - Jira API client, field IDs, ticket creation → `outputs/jira/`.
   - Pydantic models that are domain-agnostic (Thread, InterviewState, User) → `engine/models.py`.
3. Fix imports. Run the test suite. Fix what broke. Iterate until green.
4. Note every place where the engine still imports something NetSuite, Slack, or Jira-specific. This is the leak list — Phase 2 closes it.

**Acceptance:**
- Directory structure matches `CLAUDE.md`.
- All Phase 0 tests still pass.
- Manual regression checklist passes.
- Leak list documented in a comment at the top of `engine/__init__.py` for Phase 2 to address.

**Risk:** circular imports during the move. Mitigation: move bottom-up (models first, then engine internals, then transport/domain/output last). Commit after each green state.

---

## Phase 2 — Introduce the three interfaces

**Goal:** replace the leaks from Phase 1 with formal abstract base classes. The engine should now be importable without any NetSuite, Slack, or Jira modules loaded.

**Tasks:**
1. Define `transports/base.py` with the `Transport` ABC per `CLAUDE.md`. Refactor `transports/slack/adapter.py` to implement it. The engine should depend only on `Transport`, never on the Slack adapter directly.
2. Define `domains/base.py` with the `DomainPack` ABC. Move NetSuite pillars and gate rules into `domains/netsuite/config.yaml`. Move the system prompt into `domains/netsuite/system_prompt.md`. Implement `NetSuiteDomainPack` as a class that loads from those files.
3. Define `outputs/base.py` with the `OutputAdapter` ABC. Refactor the Jira client to implement it.
4. Build `config/registry.py` — the wiring layer that, given configuration, instantiates the right transport, domain pack, and output adapter and hands them to the engine.
5. Refactor `app.py` to be just: load settings → build registry → start transport. No business logic.
6. Verify the leak list from Phase 1 is empty. Add a CI check (a simple grep or import-linter rule) that fails if `engine/` imports anything from `transports/`, `domains/`, or `outputs/`.

**Acceptance:**
- `engine/` has no imports from sibling top-level packages.
- All Phase 0 tests still pass.
- Manual regression checklist passes.
- CI enforces the import boundary.
- Bot behaves identically to pre-refactor from a user perspective.

**Risk:** the abstractions might not fit cleanly. If `Transport` or `DomainPack` needs a method that feels NetSuite-shaped or Slack-shaped, that's fine for now — note it, move on. Phase 4 validates whether it really matters.

---

## Phase 3 — Configuration, observability, evals

**Goal:** make the system iterable by non-engineers and debuggable when things go sideways.

**Tasks:**
1. **Config-driven domains:** ensure `domains/netsuite/config.yaml` is the source of truth for pillars, gate rules, and follow-up triggers. A BSA editing the YAML and restarting the bot should change behavior without code changes. Document the YAML schema in `docs/DOMAIN_PACK_SPEC.md`.
2. **Prompt files:** every Claude prompt comes from a file, not a Python string literal. System prompts in `domains/<name>/system_prompt.md`. Engine-level prompts (extraction, gate) in `engine/prompts/`. Templating via Jinja2 or equivalent.
3. **Observability:** decide on Langfuse vs structured Postgres logs vs both. Implement. Every Claude call logged with: timestamp, domain, phase (interview/extraction/gate), prompt, response, model, latency, tokens, thread_id, tenant_id.
4. **Eval harness:** `tests/evals/` with at least 10 canonical interview transcripts and expected pillar extractions. A script that runs them all and reports pass/fail. Wire into CI as a non-blocking check (we want signal, not gate, on prompt changes during the pilot).
5. **Admin commands:** Slack commands like `/gatekeeper-status <thread>` and `/gatekeeper-reset <thread>` for ops. These live in the Slack transport but call into engine APIs.
6. **Cleanup job:** periodic task (cron or simple async loop) that transitions threads idle >72h to `ABANDONED`.

**Acceptance:**
- BSA can edit `config.yaml`, restart the bot, and see new pillars in interviews. Verified by walking through this with an actual BSA.
- Eval harness runs in CI and passes on current prompts.
- Every Claude call shows up in the observability backend.
- Admin commands work end-to-end.

**Out of scope:** the eval harness does not need to be sophisticated yet. A diff against expected JSON is enough. Fancier scoring comes later.

---

## Phase 4 — Salesforce stub (the abstraction test)

**Goal:** validate that the architecture actually supports a second domain by building one. This is the moment of truth — if the abstractions leak, we find out now, while Salesforce is a stub, not after we've shipped.

**Tasks:**
1. Create `domains/salesforce/` with `config.yaml`, `system_prompt.md`, and `ticket_template.py`. Pillars and prompts can be rough-draft quality — this is not a production Salesforce pack, it's an abstraction test.
2. Reasonable Salesforce pillars to start: Object & Field Impact, Automation Impact (flows, triggers, validation rules), Profile/Permission Set Impact, Data Migration Need, Integration Impact, Testing Approach. Do not spend a week perfecting these — get them good enough to run an interview.
3. Add a config option to select the active domain. Run an end-to-end interview against the Salesforce domain pack.
4. **Document every change required to the engine to support Salesforce.** If the answer is "none," the abstraction held. If changes were needed, they go *into the engine generically*, not as Salesforce-specific code. Update `CLAUDE.md` to reflect what was learned.
5. Tickets created by the Salesforce flow can go into the same Jira project for now (different issue type or label is fine). No need for a separate output adapter.

**Acceptance:**
- An end-to-end Salesforce interview produces a Jira ticket without any `if domain == "salesforce"` in the engine.
- Lessons learned documented in `CLAUDE.md`.
- The Salesforce domain pack is committed but disabled by default in the pilot config (we're not actually piloting Salesforce yet).

**Risk:** discovering the abstraction is wrong. This is a feature, not a bug — better now than later. Budget for the possibility of a Phase 4.5 to fix what Phase 4 reveals.

---

## Phase 5 — Pilot polish

**Goal:** make the system production-ready for the internal NetSuite pilot and instrument the metrics that make the case for funding.

**Tasks:**
1. **Metrics dashboard.** Time-to-ready, completion rate, rework rate, BSA hours saved estimate. Even a basic Grafana or Metabase view is fine. The goal is a screen we can show upper management.
2. **Ticket quality rubric.** Define the rubric (clarity, completeness, testability, value articulation) and have BSAs grade a sample of N=20+ bot-produced tickets vs human-produced tickets. Capture results.
3. **State recovery.** What happens if the bot restarts mid-interview? Threads in `INTERVIEW` should resume cleanly from Postgres state. Test this explicitly with a kill -9 during an interview.
4. **Error UX.** When extraction or the gate fails, the user should get a useful message in Slack, not silence. When Jira creation fails, the requirement state should be preserved so we can retry.
5. **Onboarding flow.** First-time users get a short explainer message. Document the user-facing flow in `docs/USER_GUIDE.md` for the pilot rollout.
6. **Rate limiting and retries.** Anthropic API errors retry with backoff. Slack API errors retry. Jira API errors retry. Don't let transient failures kill an interview.

**Acceptance:**
- Pilot rollout to a small group (5–10 NetSuite requesters) for two weeks.
- Metrics dashboard live and populating.
- BSA quality rubric scored on at least 20 tickets.
- Restart-mid-interview test passes.
- Pilot retrospective written up.

---

## Phase 6 — The funding pitch

**Goal:** convert pilot results into upper-management buy-in for the next phase (additional domains, additional transports, possibly multi-tenancy).

**Tasks:**
1. Pull metrics from Phase 5: time-to-ready delta, hours saved, ticket quality scores, rework rate delta.
2. Collect 3–5 testimonials from pilot users and BSAs.
3. Draft a one-page proposal for the next phase: what gets built (Salesforce promotion to real, Teams transport, ServiceNow output, etc.), estimated effort, expected ROI based on pilot data.
4. Present.

**Acceptance:** funding decision made. Update `CLAUDE.md` and this plan to reflect the new scope.

---

## Working norms across all phases

- **One phase per branch.** Don't mix phase work.
- **Manual regression checklist at the end of every phase.** Automated tests are the safety net; the manual checklist is the seatbelt.
- **If a phase takes more than two weeks of calendar time, stop and reassess.** Either the scope was wrong or something is harder than expected. Either way, the plan needs updating.
- **Document surprises in `CLAUDE.md`.** Future-you and future-Claude-Code will thank present-you.
