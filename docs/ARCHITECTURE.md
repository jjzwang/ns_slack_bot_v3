# Architecture

This is the deep-reference companion to `CLAUDE.md`. Where `CLAUDE.md` states the rules, this document explains the reasoning, shows the full interface signatures, and walks through how the pieces fit together at runtime.

## Why a layered architecture

The first version of this bot was NetSuite-on-Slack-into-Jira, and that's still the only configuration we ship in the pilot. So why bother with abstractions?

Three reasons:

**The interview engine is the actual product.** The value isn't NetSuite-specific or Slack-specific; it's the disciplined BSA-style requirement gathering. Every other system worth integrating with — Salesforce, Workday, ServiceNow — has the same need. If we couple the engine to NetSuite, we'll rewrite it for every new domain. If we keep it clean, we add a YAML file and a system prompt.

**Internal pilots beget asks.** Once the NetSuite team sees this working, the Salesforce team will ask. Then the Workday team. Then someone on Teams will ask why it's Slack-only. We want our answer to be "two weeks" not "three months."

**Abstractions are cheaper to build than to retrofit.** Establishing layer boundaries while there's only one implementation per layer is a small tax. Retrofitting them after multiple implementations have grown into each other is a rewrite.

## The three layers

```
┌─────────────────────────────────────────────────────────┐
│                      Transport layer                     │
│  (Slack today; Teams, Google Chat, web widget later)    │
│                                                          │
│  Responsibilities: receive user input, send bot output, │
│  resolve user identities, register command handlers.    │
└────────────────────────┬────────────────────────────────┘
                         │
                         │ Transport ABC
                         ▼
┌─────────────────────────────────────────────────────────┐
│                       Engine layer                       │
│         (domain & transport agnostic; the spine)        │
│                                                          │
│  Responsibilities: state machine, conversation loop,    │
│  Claude orchestration, pillar extraction, gate logic,   │
│  persistence. Knows nothing about NetSuite, Slack, or   │
│  Jira.                                                   │
└──────────────┬──────────────────────────┬──────────────┘
               │                          │
               │ DomainPack ABC           │ OutputAdapter ABC
               ▼                          ▼
┌──────────────────────────┐  ┌──────────────────────────┐
│      Domain layer         │  │      Output layer         │
│  (NetSuite today;         │  │  (Jira today; ServiceNow, │
│   Salesforce stub;        │  │   Azure DevOps later)     │
│   Workday, etc. later)    │  │                           │
│                           │  │  Responsibilities: turn   │
│  Responsibilities: define │  │  a TicketDraft into a     │
│  pillars, system prompt,  │  │  real ticket in the       │
│  gate rules, ticket field │  │  target system.           │
│  mapping for one system.  │  │                           │
└──────────────────────────┘  └──────────────────────────┘
```

The engine is the only layer that imports from `engine/`. The other three are siblings — they don't import from each other. Wiring happens in `config/registry.py`, which is the only place that knows about all four.

## Full interface contracts

### Transport

```python
# transports/base.py
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

@dataclass
class User:
    platform_id: str        # Slack user ID, Teams AAD ID, etc.
    display_name: str
    email: str | None       # may be None if platform restricts

@dataclass
class InboundMessage:
    thread_id: str
    user: User
    text: str
    raw: dict               # platform-native payload, for debugging

@dataclass
class Command:
    name: str               # e.g. "netsuite-new-change"
    user: User
    channel_id: str
    args: str               # raw text after the command
    raw: dict

class Transport(ABC):
    @abstractmethod
    async def send_message(
        self,
        thread_id: str,
        text: str,
        blocks: list | None = None,
    ) -> str:
        """Returns the message ID assigned by the platform."""

    @abstractmethod
    async def open_dm(self, user_id: str) -> str:
        """Open a DM channel with a user. Returns the thread/channel ID."""

    @abstractmethod
    async def resolve_user(self, user_id: str) -> User:
        """Look up display name and email for a platform user ID."""

    @abstractmethod
    async def post_to_channel(
        self,
        channel_id: str,
        text: str,
        blocks: list | None = None,
    ) -> str:
        """Post to a non-DM channel (e.g. triage)."""

    @abstractmethod
    def register_message_handler(
        self,
        handler: Callable[[InboundMessage], Awaitable[None]],
    ) -> None:
        """Wire up the engine's message handler."""

    @abstractmethod
    def register_command_handler(
        self,
        command: str,
        handler: Callable[[Command], Awaitable[None]],
    ) -> None:
        """Wire up a slash command handler."""

    @abstractmethod
    async def start(self) -> None:
        """Begin listening. Blocks for the lifetime of the process."""
```

Note on `blocks`: this is intentionally a pass-through `list` — Slack Block Kit today. When a second transport is added, we'll decide whether to introduce a neutral block format with per-platform translators. Premature generalization is worse than no generalization, so we leave it concrete for now.

### DomainPack

```python
# domains/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class Pillar:
    key: str                    # e.g. "value_to_business"
    display_name: str
    description: str            # what the BSA is trying to capture
    required: bool
    examples: list[str]         # good answer examples for the prompt

@dataclass
class GateRule:
    key: str
    description: str
    severity: str               # "block" | "warn" | "info"

@dataclass
class TicketDraft:
    summary: str
    description: str            # markdown
    fields: dict[str, object]   # output-adapter-specific field mapping
    labels: list[str]

class DomainPack(ABC):
    name: str                   # "netsuite", "salesforce"

    @property
    @abstractmethod
    def pillars(self) -> list[Pillar]: ...

    @property
    @abstractmethod
    def system_prompt(self) -> str: ...

    @abstractmethod
    def gate_rules(self) -> list[GateRule]: ...

    @abstractmethod
    def ticket_template(self, pillars: dict[str, str]) -> TicketDraft: ...

    @abstractmethod
    def followup_prompts(
        self,
        pillar_key: str,
        current_answer: str,
    ) -> str | None:
        """Domain-specific probing questions when an answer is thin.
        Return None if no follow-up is needed."""
```

The `name`, `pillars`, `gate_rules`, and `system_prompt` are loaded from `config.yaml` and `system_prompt.md`. The `ticket_template` and `followup_prompts` are Python because they involve real logic that doesn't compress to YAML well.

### OutputAdapter

```python
# outputs/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class TicketRef:
    id: str                     # platform ticket ID, e.g. "FIN-1234"
    url: str                    # link to the ticket
    raw: dict                   # platform-native response, for debugging

class OutputAdapter(ABC):
    @abstractmethod
    async def create_ticket(
        self,
        draft: TicketDraft,
        requester: User,
    ) -> TicketRef: ...

    @abstractmethod
    async def attach_transcript(
        self,
        ticket_ref: TicketRef,
        transcript: str,
    ) -> None: ...
```

## State machine

State per thread, persisted in Postgres `interview_state` table.

```
                    ┌─────────────┐
                    │   START     │
                    └──────┬──────┘
                           │ /netsuite-new-change
                           ▼
                    ┌─────────────┐
            ┌──────►│  INTERVIEW  │◄──────┐
            │       └──────┬──────┘       │
            │              │ user reply   │
            │              ▼              │
            │       ┌─────────────┐       │
            │       │  PROCESSING │       │
            │       └──────┬──────┘       │
            │              │              │
            │      pillars incomplete     │
            └──────────────┘              │
                           │              │
                  pillars complete        │
                           │              │
                           ▼              │
                    ┌─────────────┐       │
                    │    GATE     │       │
                    └──────┬──────┘       │
                           │              │
              ┌────────────┼────────────┐ │
              │            │            │ │
           pass         needs info    fail
              │            │            │ │
              ▼            └────────────┘ │
       ┌─────────────┐                    │
       │    READY    │                    │
       │ (ticket     │                    │
       │  created)   │                    │
       └─────────────┘                    │
                                          │
                       ┌─────────────┐    │
                       │  ESCALATED  │    │
                       │ (human BSA) │    │
                       └─────────────┘    │
                                          │
                       ┌─────────────┐    │
                       │  ABANDONED  │◄───┘
                       │ (>72h idle) │  cleanup job
                       └─────────────┘
```

State transitions are atomic Postgres updates. The engine never holds state in memory beyond a single message handling — restarts resume cleanly from the database.

## Sequence: a happy-path interview

```
User                Slack            Engine            Claude            Jira
 │                    │                 │                 │                │
 │ /netsuite-new-     │                 │                 │                │
 │ change             │                 │                 │                │
 ├───────────────────►│                 │                 │                │
 │                    │ command_handler │                 │                │
 │                    ├────────────────►│                 │                │
 │                    │                 │ create thread,  │                │
 │                    │                 │ state=INTERVIEW │                │
 │                    │                 │                 │                │
 │                    │                 │ build initial   │                │
 │                    │                 │ system prompt   │                │
 │                    │                 ├────────────────►│                │
 │                    │                 │   first Q       │                │
 │                    │                 │◄────────────────┤                │
 │                    │ send_message    │                 │                │
 │                    │◄────────────────┤                 │                │
 │ first Q            │                 │                 │                │
 │◄───────────────────┤                 │                 │                │
 │                    │                 │                 │                │
 │ answer             │                 │                 │                │
 ├───────────────────►│                 │                 │                │
 │                    │ message_handler │                 │                │
 │                    ├────────────────►│                 │                │
 │                    │                 │ state=PROCESSING│                │
 │                    │                 ├────────────────►│ extract pillar │
 │                    │                 │◄────────────────┤ (Haiku)        │
 │                    │                 │                 │                │
 │                    │                 │ pillars         │                │
 │                    │                 │ incomplete →    │                │
 │                    │                 │ next Q (Sonnet) │                │
 │                    │                 ├────────────────►│                │
 │                    │                 │◄────────────────┤                │
 │                    │ send_message    │                 │                │
 │                    │◄────────────────┤                 │                │
 │ next Q             │                 │                 │                │
 │◄───────────────────┤                 │                 │                │
 │                    │                 │                 │                │
 │       (loop until pillars complete)                                     │
 │                    │                 │                 │                │
 │                    │                 │ run gate        │                │
 │                    │                 ├────────────────►│ (Sonnet)       │
 │                    │                 │◄────────────────┤ pass           │
 │                    │                 │                 │                │
 │                    │                 │ ticket_template │                │
 │                    │                 │ (domain pack)   │                │
 │                    │                 │                 │                │
 │                    │                 │ create_ticket   │                │
 │                    │                 ├──────────────────────────────────►│
 │                    │                 │◄──────────────────────────────────┤
 │                    │                 │ state=READY     │                │
 │                    │ send_message    │                 │                │
 │                    │ (ticket link)   │                 │                │
 │                    │◄────────────────┤                 │                │
 │ ticket link        │                 │                 │                │
 │◄───────────────────┤                 │                 │                │
```

The engine never imports from `transports/slack/`, `domains/netsuite/`, or `outputs/jira/`. It receives concrete instances at construction time via the registry.

## Wiring: how the registry assembles the system

```python
# config/registry.py (sketch)

def build_app(settings: Settings) -> App:
    # Resolve concrete implementations from settings
    transport = _build_transport(settings.transport)       # e.g. SlackTransport
    domain = _build_domain(settings.active_domain)         # e.g. NetSuiteDomainPack
    output = _build_output(settings.output)                # e.g. JiraOutputAdapter
    state_store = PostgresStateStore(settings.database_url)
    claude = AnthropicClient(settings.anthropic_api_key)

    engine = Engine(
        transport=transport,
        domain=domain,
        output=output,
        state_store=state_store,
        claude=claude,
        observability=build_observability(settings),
    )

    # Wire transport callbacks to engine handlers
    transport.register_command_handler("netsuite-new-change", engine.handle_command)
    transport.register_message_handler(engine.handle_message)

    return App(engine=engine, transport=transport)
```

Adding Salesforce later is a config change, not a code change. Adding Teams later is a new transport adapter, no engine change. Adding ServiceNow later is a new output adapter, no engine change. That's the whole point.

## LLM model assignment

| Phase                | Model       | Rationale                                              |
| -------------------- | ----------- | ------------------------------------------------------ |
| Interview loop       | Sonnet      | Reasoning-heavy, multi-turn, persona-driven            |
| Pillar extraction    | Haiku       | Constrained structured output, cost-sensitive at scale |
| Solution review gate | Sonnet      | Reasoning-heavy, judgment call                         |
| Follow-up prompts    | Sonnet      | Light, but benefits from interview context             |

Model choice lives in `config/settings.py` and is overridable per environment. The engine asks the Claude client for the right model for the right phase; it doesn't hardcode model names.

## Observability

Every Claude call produces a structured log entry with at minimum:

- `timestamp`
- `tenant_id` (defaults to `"internal"` for the pilot)
- `thread_id`
- `domain` (e.g. `"netsuite"`)
- `phase` (`"interview" | "extraction" | "gate" | "followup"`)
- `model`
- `prompt` (full)
- `response` (full)
- `latency_ms`
- `prompt_tokens`, `completion_tokens`
- `outcome` (`"ok" | "retry" | "error"`)

Backend choice is one decision point during Phase 3. The leading options are Langfuse (LLM-native, good UX, hosted), structured Postgres logs (cheap, queryable, lives in our existing DB), or both. Pick before Phase 3 ends; don't leave it open.

## Eval harness

`tests/evals/` contains:

```
tests/evals/
├── transcripts/
│   ├── 001_simple_workflow_change.json
│   ├── 002_saved_search_modification.json
│   ├── 003_complex_integration.json
│   └── ...
├── expected/
│   ├── 001_simple_workflow_change.json
│   ├── 002_saved_search_modification.json
│   └── ...
└── run_evals.py
```

Each transcript fixture is a recorded interview. Each expected file is the pillar extraction we expect from that transcript. `run_evals.py` runs the extractor against each fixture and diffs against expected.

Pass criterion in the pilot: 80% pillar-key match rate. We are not benchmarking against perfection; we are catching regressions when prompts change.

## Things deliberately not in this architecture

- **A web UI.** Pilot is chat-only.
- **Multi-tenancy enforcement.** Schema hooks exist; runtime enforcement does not.
- **A second transport.** Slack only.
- **A second output.** Jira only.
- **A real Salesforce domain.** Stub only, used as an abstraction test.
- **HTTP mode for Slack.** Socket Mode only.
- **A custom auth system.** Slack identity → Jira email lookup is the entire auth story.

Each of these is a defensible cut for the pilot. Each will need to be revisited after Phase 6 funding, in priority order driven by what upper management buys into.
