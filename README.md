# NetSuite Requirement Gatekeeper — Slack Bot

A Slack bot that interviews requesters via Claude, extracts six requirement
pillars, runs a solution-review gate, and creates a Definition-of-Ready Jira
ticket. Runs in **Socket Mode** — no public HTTP endpoint required.

---

## Architecture

Slack DM ──► /netsuite-new-change ──► Bolt App (Socket Mode)
│
├─► Claude (Sonnet) — BSA interview
├─► Claude (Haiku) — pillar extraction
├─► Claude (Sonnet) — review gate
├─► PostgreSQL — thread state
└─► Jira Cloud API — ticket creation


**State machine** (stored per thread in `interview_state`):
`INTERVIEW → PROCESSING → INTERVIEW` (loop) → `READY` | `ESCALATED`.

---

## Prerequisites

- Python 3.11+
- PostgreSQL 13+ (local or managed)
- Slack workspace with admin rights to install a custom app
- Atlassian Cloud Jira instance with a service-account API token
- Anthropic API key

---

## Slack App Setup

Create a Slack app at <https://api.slack.com/apps> → **From scratch**.

### Required Bot Token Scopes (`OAuth & Permissions`)

| Scope                | Why |
| -------------------- | --- |
| `chat:write`         | Post messages / thread replies |
| `chat:write.public`  | Post to triage channel without being a member (optional) |
| `commands`           | Register `/netsuite-new-change` |
| `users:read`         | Resolve Slack user → display name |
| `users:read.email`   | Resolve Slack user → email (for Jira lookup) |
| `im:history`         | Receive messages in DMs |
| `im:write`           | Open DMs |
| `channels:read`      | Validate triage channel ID |

### Event Subscriptions

Enable **Events** and subscribe to:
- `message.im` — thread replies in DMs

### Slash Command

Register `/netsuite-new-change` (no request URL needed in Socket Mode).

### Socket Mode

Enable Socket Mode and generate an **App-Level Token** with the
`connections:write` scope. This is the `SLACK_APP_TOKEN` env var (starts with
`xapp-`). The `SLACK_BOT_TOKEN` is your bot OAuth token (starts with `xoxb-`).

### Install & Invite

1. Install the app to your workspace.
2. Invite the bot to your **triage channel**: `/invite @NetSuite Gatekeeper`.
3. Copy the triage channel ID (right-click channel → Copy link → last path segment) into `TRIAGE_CHANNEL_ID`.

---

## Jira Setup

1. Create an API token at <https://id.atlassian.com/manage-profile/security/api-tokens> for the service account that will own tickets. Use this account's email in `JIRA_USER_EMAIL` and the token in `JIRA_API_TOKEN`.
2. Identify the target **project key** (e.g. `FIN`) — `JIRA_PROJECT_KEY`.
3. Identify your **issue-type ID** (usually `10001` for Story) — `JIRA_ISSUE_TYPE_ID`.
4. **Custom field IDs are tenant-specific.** Find them via:

curl -u "$JIRA_USER_EMAIL:$JIRA_API_TOKEN"
"$JIRA_BASE_URL/rest/api/3/field" | jq '.[] | select(.custom) | {id, name}'

Map the IDs into:
- `JIRA_CF_VALUE_TO_BUSINESS` — your "Value to the Business" field
- `JIRA_CF_ACCEPTANCE_CRITERIA` — your "Acceptance Criteria" field
- `JIRA_CF_ENABLEMENT_PLAN` — your "Enablement Plan" field
- `JIRA_CF_DEFAULT_1/2/3` — any other required fields for your project (optional)

---

## PostgreSQL Setup

Run once on your DB host (or any machine with psql access):

```bash
chmod +x setup_postgres.sh
sudo -u postgres ./setup_postgres.sh

The script creates the role + database and prints a DATABASE_URL. Paste that
into your .env. On first startup the app auto-creates the interview_state
table and indexes (database.init_db()).

Production note: init_db() runs CREATE TABLE IF NOT EXISTS +
ALTER TABLE ADD COLUMN IF NOT EXISTS on every boot. Under a rolling
multi-instance deploy, run migrations out-of-band or serialize with a
pg_advisory_lock before scaling past a single worker.