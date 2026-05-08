# CLAUDE.md — Requirements Gatekeeper

This file is the standing context for Claude Code working on this repository. Read it at the start of every session. If the architecture evolves, update this file in the same change.

## What this project is

An LLM-driven Business Systems Analyst that interviews requesters via chat, extracts structured requirements, runs a solution-review gate, and creates Definition-of-Ready tickets in the team's tracker. Currently piloting internally with **NetSuite** as the domain, **Slack** as the transport, and **Jira** as the output. Designed from day one to add other domains (Salesforce, Workday, etc.), transports (Teams, Google Chat), and outputs (ServiceNow, Azure DevOps).

## Audience for this codebase

This is an **internal platform** during the pilot phase. We are not building multi-tenant SaaS yet. We are demonstrating value to upper management to fund the next phase. Optimize for: clarity, ease of iteration on prompts and pillars, observability, and a clean abstraction that proves out when we add Salesforce.

## Architectural rules — non-negotiable

These rules exist because the whole point of the refactor is a clean abstraction. Violating them defeats the project.

1. **The engine knows nothing about NetSuite, Slack, or Jira.** No `if domain == "netsuite"`, no Slack imports, no Jira field IDs. If the engine needs domain or transport context, it gets it through the interface, not by knowing the concrete type.
2. **Domain-specific logic lives in `domains/<name>/`.** Pillars, system prompts, gate rules, ticket templates, follow-up prompts. Nothing else.
3. **Transport-specific logic lives in `transports/<name>/`.** Bolt, Socket Mode, Block Kit formatting, slash command registration. Nothing else.
4. **Output-specific logic lives in `outputs/<name>/`.** API clients, field mappings, ticket creation. Nothing else.
5. **Config-driven over code-driven.** Pillars and prompts are YAML and markdown files, not Python literals. BSAs should be able to iterate without a code change.
6. **Premature generalization is worse than no generalization.** With a sample size of one domain and one transport, the abstractions will be slightly NetSuite-shaped and slightly Slack-shaped. That's fine. Refactor when Salesforce or Teams reveals what's actually variable, not before.

## Directory structure

```
gatekeeper/
├── engine/                    # Domain & transport agnostic
│   ├── interview.py           # Conversation loop, Claude calls
│   ├── extraction.py          # Pillar extraction (Haiku)
│   ├── gate.py                # Solution review gate (Sonnet)
│   ├── state.py               # State machine + Postgres persistence
│   └── models.py              # Pydantic: Requirement, Pillar, Thread, etc.
├── domains/                   # Pluggable domain packs
│   ├── base.py                # DomainPack ABC
│   ├── netsuite/
│   │   ├── config.yaml        # Pillars, gate rules
│   │   ├── system_prompt.md   # BSA persona + NetSuite context
│   │   └── ticket_template.py # Pillars → output fields
│   └── salesforce/            # Stub for now; proves the abstraction
├── transports/                # Pluggable chat platforms
│   ├── base.py                # Transport ABC
│   └── slack/
│       ├── adapter.py         # Bolt app, Socket Mode
│       ├── handlers.py        # Slash command, message events
│       └── formatting.py      # Slack-specific Block Kit
├── outputs/                   # Where requirements land
│   ├── base.py                # OutputAdapter ABC
│   └── jira/
│       └── adapter.py
├── config/
│   ├── settings.py            # Pydantic Settings, env-driven
│   └── registry.py            # Wires domains + transports + outputs
├── tests/
│   ├── evals/                 # Canonical interviews + expected extractions
│   └── unit/
└── app.py                     # Entry point
```

The directory layout *is* the architecture. When asking "where does X live?", the answer should be obvious from this tree.

## Interface contracts

These three interfaces are the spine of the system. Changes to them are architectural decisions, not implementation details — flag them explicitly in PRs.

### Transport (`transports/base.py`)

```python
class Transport(ABC):
    async def send_message(self, thread_id: str, text: str, blocks: list | None = None) -> str: ...
    async def open_dm(self, user_id: str) -> str: ...
    async def resolve_user(self, user_id: str) -> User: ...
    async def post_to_channel(self, channel_id: str, text: str, blocks: list | None = None) -> str: ...
    def register_message_handler(self, handler: Callable[[InboundMessage], Awaitable[None]]): ...
    def register_command_handler(self, command: str, handler: Callable[[Command], Awaitable[None]]): ...
```

`blocks` is intentionally pass-through (Slack Block Kit today). When a second transport is added, decide then whether to introduce a neutral block format. Don't over-engineer it now.

### DomainPack (`domains/base.py`)

```python
class DomainPack(ABC):
    name: str
    pillars: list[Pillar]
    system_prompt: str
    
    def gate_rules(self) -> list[GateRule]: ...
    def ticket_template(self, pillars: dict) -> TicketDraft: ...
    def followup_prompts(self, pillar: str, current_answer: str) -> str | None: ...
```

Pillars and gate rules are loaded from `config.yaml`. The system prompt is loaded from `system_prompt.md`. The ticket template is the one piece of Python because field mapping is genuinely code.

### OutputAdapter (`outputs/base.py`)

```python
class OutputAdapter(ABC):
    async def create_ticket(self, draft: TicketDraft, requester: User) -> TicketRef: ...
    async def attach_transcript(self, ticket_ref: TicketRef, transcript: str) -> None: ...
```

## State machine

Per-thread state in Postgres (`interview_state` table):

```
INTERVIEW → PROCESSING → INTERVIEW (loop until pillars complete)
                      → READY (gate passed, ticket created)
                      → ESCALATED (gate failed or requester needs human BSA)
                      → ABANDONED (timeout — see below)
```

Threads idle in `INTERVIEW` for >72h transition to `ABANDONED` via a periodic cleanup job. Don't garbage-collect them; they're useful for analytics.

## LLM usage conventions

- **Sonnet** for the interview loop and the review gate (reasoning-heavy).
- **Haiku** for pillar extraction (constrained, structured output).
- **Every Claude call is logged**: domain, pillar (if applicable), full prompt, full response, latency, tokens, model. Use structured logs to Postgres or Langfuse — decide once and stick with it.
- **All prompts that go to Claude come from files**, not Python string literals. System prompts in `domains/<name>/system_prompt.md`. Engine-level prompts (extraction, gate) in `engine/prompts/`.
- **Extraction failures retry once with a stricter prompt, then escalate.** Don't silently fall back to garbage data.

## Eval harness — required, not optional

`tests/evals/` contains canonical interview transcripts with expected pillar extractions. Run on every prompt change. Without this, prompt iteration becomes vibes-driven and regressions go silent.

Minimum viable harness: a script that loads transcript fixtures, runs the extractor, diffs against expected output, and prints a pass/fail summary. N=10 transcripts is enough to start. Grow it as the pilot surfaces edge cases.

## Multi-tenancy hooks (forward-looking)

Even though we're single-tenant for the pilot, every Postgres table has a `tenant_id` column defaulting to `"internal"`. Every config load takes a tenant. We will not use this during the pilot. We will be glad we have it the day someone says "can finance run their own instance with different domains?"

## Pilot metrics — instrument from day one

These are the metrics that make the case for funding the next phase. Wire them up in week one so we have a trend line, not a snapshot.

- **Time-to-ready**: from `/new-change` to ticket created. Compare to baseline.
- **Ticket quality**: BSA-graded rubric on bot output vs human output. Sample N=20+.
- **BSA hours saved**: rough estimate based on intake volume × time per intake.
- **Rework rate**: tickets returned by engineering with missing info.
- **Completion rate**: interviews started vs interviews reaching READY.

## What is explicitly out of scope for the pilot

- Salesforce or any second domain beyond a stub
- Teams, Google Chat, or any second transport
- ServiceNow, Azure DevOps, or any second output
- Multi-tenancy (hooks only, no enforcement)
- HTTP mode for Slack (Socket Mode only)
- A web UI of any kind
- Per-user authentication beyond Slack identity → Jira email lookup

When tempted to build any of these "while we're in there," don't. Note it as a follow-up and keep moving.

## Conventions

- Python 3.11+, type hints everywhere, Pydantic for all data models.
- Async by default for I/O. Sync only for pure CPU work.
- `ruff` for lint, `black` for format, `pytest` for tests, `mypy --strict` for types.
- Secrets via `.env` for the pilot. Document the migration path to a real secret manager but don't build it.
- `RUN_MIGRATIONS=1` is acceptable for the pilot. Before scaling past one instance, run migrations out-of-band and unset the flag.
- Commit after each working step. Small reversible commits beat big unreviewable ones, especially for refactors.

## Working with Claude Code on this repo

- Start every session by reading this file and the relevant migration plan section in `docs/MIGRATION_PLAN.md`.
- Scope tasks tightly. "Implement the Transport ABC and refactor existing Slack code to conform to it" is a session. "Do the refactor" is not.
- Propose changes before writing them. List files to touch and risks. Get approval, then execute.
- Update this file when architecture evolves. A stale CLAUDE.md is worse than none.
- When in doubt about an abstraction boundary, default to keeping things concrete in the domain or transport layer rather than promoting them to the engine. Promotion is easy later; demotion after dependencies form is hard.

## Pointers to other docs

- `docs/ARCHITECTURE.md` — deeper dive on layers, full interface signatures, sequence diagrams.
- `docs/MIGRATION_PLAN.md` — week-by-week refactor plan; the working spec for sessions.
- `docs/DOMAIN_PACK_SPEC.md` — how to author a domain pack. For BSAs (with engineering help) iterating on NetSuite and eventually authoring Salesforce.
