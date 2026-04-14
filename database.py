# =============================================================================
# Database — PostgreSQL Interview State
# =============================================================================
# One record per Slack thread (thread_id = primary key).
#
# Requires:
#   pip install psycopg2-binary
#
# Environment variables:
#   DATABASE_URL  — full connection string (preferred), e.g.:
#                   postgresql://gatekeeper:secret@localhost:5432/gatekeeper
#
#   Or individual variables:
#     PGHOST      — default: localhost
#     PGPORT      — default: 5432
#     PGDATABASE  — default: gatekeeper
#     PGUSER      — default: gatekeeper
#     PGPASSWORD  — required
#
# Status lifecycle:
#   INTERVIEW → PROCESSING → INTERVIEW (gathering loop)
#                                ↓
#                         Pillars 1-4 populated (via extraction)
#                                ↓
#                           REVIEWING → gaps found → INTERVIEW (follow-up)
#                                ↓                       ↓
#                           no gaps                 gaps addressed
#                                ↓                       ↓
#                           INTERVIEW (drafting) ←──────┘
#                                ↓
#                           PROCESSING → VERIFY (summary shown)
#                                ↓
#                           User confirms "Yes"
#                                ↓
#                           PROCESSING → READY (Jira created)

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)

# ─── Connection Pool ─────────────────────────────────────────────────────────
# A threaded pool hands each caller its own connection and recycles them.
# minconn=2  → keeps 2 connections warm (main thread + one Slack handler)
# maxconn=10 → ceiling for concurrent Slack events (generous for a pilot)

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def _get_dsn() -> str:
    """Build the PostgreSQL connection string from environment."""
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return dsn

    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    dbname = os.environ.get("PGDATABASE", "gatekeeper")
    user = os.environ.get("PGUSER", "gatekeeper")
    password = os.environ.get("PGPASSWORD", "")

    if not password:
        raise ValueError(
            "Database password not set. Provide DATABASE_URL or PGPASSWORD."
        )

    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Lazy-initialize and return the connection pool."""
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=_get_dsn(),
        )
    return _pool


def _get_conn():
    """Get a connection from the pool."""
    return _get_pool().getconn()


def _put_conn(conn):
    """Return a connection to the pool."""
    try:
        _get_pool().putconn(conn)
    except Exception:
        pass


def close_pool() -> None:
    """Close all connections in the pool. Call on shutdown."""
    global _pool
    if _pool is not None and not _pool.closed:
        _pool.closeall()
        _pool = None


def init_db() -> None:
    """
    Create the interview_state table and indexes if they don't exist.

    Safe to call on every startup — uses IF NOT EXISTS throughout.
    Also runs migrations for adding new columns to existing tables.
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS interview_state (
                    thread_id               TEXT PRIMARY KEY,
                    channel_id              TEXT NOT NULL,
                    user_id                 TEXT NOT NULL,
                    user_email              TEXT NOT NULL DEFAULT '',
                    user_jira_id            TEXT NOT NULL DEFAULT '',
                    user_display_name       TEXT NOT NULL DEFAULT 'Unknown User',
                    status                  TEXT NOT NULL DEFAULT 'INTERVIEW',
                    pillars_json            JSONB NOT NULL DEFAULT '{}'::jsonb,
                    message_history         JSONB NOT NULL DEFAULT '[]'::jsonb,
                    attempts                INTEGER NOT NULL DEFAULT 0,
                    review_completed        BOOLEAN NOT NULL DEFAULT FALSE,
                    review_gaps_json        JSONB NOT NULL DEFAULT '[]'::jsonb,
                    review_enrichments_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                    review_turn_index       INTEGER NOT NULL DEFAULT -1,
                    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
                    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)

            # ─── Indexes ─────────────────────────────────────────────
            # Status — used by try_lock_state (WHERE status = 'INTERVIEW')
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_interview_state_status
                ON interview_state (status)
            """)

            # User ID — for "show me my open interviews" queries
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_interview_state_user_id
                ON interview_state (user_id)
            """)

            # GIN on pillars — for observability JSONB queries
            # (e.g., "which interviews have persona = null?")
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_interview_state_pillars
                ON interview_state USING GIN (pillars_json)
            """)

            # ─── Migrations: add columns if missing ──────────────────
            _migrate_add_column(cur, "review_completed", "BOOLEAN NOT NULL DEFAULT FALSE")
            _migrate_add_column(cur, "review_gaps_json", "JSONB NOT NULL DEFAULT '[]'::jsonb")
            _migrate_add_column(cur, "review_enrichments_json", "JSONB NOT NULL DEFAULT '[]'::jsonb")
            _migrate_add_column(cur, "review_turn_index", "INTEGER NOT NULL DEFAULT -1")

        conn.commit()
        logger.info("Database initialized successfully.")
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


def _migrate_add_column(cur, column: str, definition: str) -> None:
    """Add a column to interview_state if it doesn't already exist."""
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'interview_state' AND column_name = %s
    """, (column,))
    if cur.fetchone() is None:
        cur.execute(
            f"ALTER TABLE interview_state ADD COLUMN {column} {definition}"
        )
        logger.info(f"Migration: added column '{column}' to interview_state.")


# ─── Data Class ──────────────────────────────────────────────────────────────

@dataclass
class InterviewState:
    thread_id: str
    channel_id: str
    user_id: str
    user_email: str = ""
    user_jira_id: str = ""
    user_display_name: str = "Unknown User"
    status: str = "INTERVIEW"
    pillars_json: str = "{}"
    message_history: str = "[]"
    attempts: int = 0
    review_completed: bool = False
    review_gaps_json: str = "[]"
    review_enrichments_json: str = "[]"
    review_turn_index: int = -1
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def get_history(self) -> list[dict]:
        """Parse message_history into a list of dicts."""
        try:
            # psycopg2 auto-deserializes JSONB → Python list/dict
            if isinstance(self.message_history, list):
                return self.message_history
            return json.loads(self.message_history)
        except (json.JSONDecodeError, TypeError):
            return []

    def set_history(self, history: list[dict]) -> None:
        """Serialize conversation history to JSON string."""
        self.message_history = json.dumps(history)

    def get_pillars(self) -> dict:
        try:
            if isinstance(self.pillars_json, dict):
                return self.pillars_json
            return json.loads(self.pillars_json)
        except (json.JSONDecodeError, TypeError):
            return {}

    def set_pillars(self, pillars: dict) -> None:
        self.pillars_json = json.dumps(pillars)

    @property
    def is_review_completed(self) -> bool:
        return bool(self.review_completed)

    def get_review_gaps(self) -> list[dict]:
        try:
            if isinstance(self.review_gaps_json, list):
                return self.review_gaps_json
            gaps = json.loads(self.review_gaps_json)
            return gaps if isinstance(gaps, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    def has_review_gaps(self) -> bool:
        return len(self.get_review_gaps()) > 0

    def get_review_enrichments(self) -> list[dict]:
        try:
            if isinstance(self.review_enrichments_json, list):
                return self.review_enrichments_json
            enrichments = json.loads(self.review_enrichments_json)
            return enrichments if isinstance(enrichments, list) else []
        except (json.JSONDecodeError, TypeError):
            return []


# ─── Row → DataClass Helper ─────────────────────────────────────────────────

def _row_to_state(row: dict) -> InterviewState:
    """
    Convert a database row (RealDictRow) into an InterviewState.

    Handles type normalization:
      - JSONB columns come back as Python dicts/lists from psycopg2,
        but InterviewState stores them as JSON strings for compatibility
        with the rest of the codebase (app.py, reviewer.py, etc.).
      - TIMESTAMPTZ comes back as datetime, convert to ISO string.
    """
    data = dict(row)

    # Normalize JSONB → JSON string
    for json_field in ("pillars_json", "message_history", "review_gaps_json", "review_enrichments_json"):
        val = data.get(json_field)
        if val is not None and not isinstance(val, str):
            data[json_field] = json.dumps(val)

    # Normalize TIMESTAMPTZ → ISO string
    for ts_field in ("created_at", "updated_at"):
        val = data.get(ts_field)
        if isinstance(val, datetime):
            data[ts_field] = val.isoformat()

    return InterviewState(**data)


# ─── CRUD Operations ────────────────────────────────────────────────────────

def get_state(thread_id: str) -> Optional[InterviewState]:
    """Retrieve interview state by thread_id."""
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM interview_state WHERE thread_id = %s",
                (thread_id,),
            )
            row = cur.fetchone()
        conn.commit()
        if row is None:
            return None
        return _row_to_state(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


def create_state(state: InterviewState) -> None:
    """Insert a new interview state record."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO interview_state
                    (thread_id, channel_id, user_id, user_email, user_jira_id,
                     user_display_name, status, pillars_json, message_history,
                     attempts, review_completed, review_gaps_json,
                     review_enrichments_json, review_turn_index,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                """,
                (
                    state.thread_id,
                    state.channel_id,
                    state.user_id,
                    state.user_email,
                    state.user_jira_id,
                    state.user_display_name,
                    state.status,
                    state.pillars_json,
                    state.message_history,
                    state.attempts,
                    state.review_completed,
                    state.review_gaps_json,
                    state.review_enrichments_json,
                    state.review_turn_index,
                    state.created_at,
                    state.updated_at,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


def update_state(thread_id: str, **updates) -> None:
    """Update specific fields on an existing interview state record."""
    if not updates:
        return
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()

    # JSONB columns need an explicit cast — psycopg2 sends Python strings
    # as text type, and Postgres won't implicitly cast text → jsonb.
    _jsonb_columns = {"pillars_json", "message_history", "review_gaps_json", "review_enrichments_json"}

    set_fragments = []
    for k in updates:
        if k in _jsonb_columns:
            set_fragments.append(f"{k} = %s::jsonb")
        else:
            set_fragments.append(f"{k} = %s")

    set_clause = ", ".join(set_fragments)
    values = list(updates.values()) + [thread_id]

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE interview_state SET {set_clause} WHERE thread_id = %s",
                values,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


def try_lock_state(thread_id: str, expected_status: str, new_status: str) -> bool:
    """
    Atomically update status only if it currently matches expected_status.

    PostgreSQL guarantees this is atomic at the row level — no explicit
    SELECT FOR UPDATE is needed because the UPDATE's WHERE clause acts
    as a conditional lock. Only one concurrent caller can match and
    update the row.

    Returns True if the lock was acquired (row was updated).
    """
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE interview_state
                SET status = %s, updated_at = %s
                WHERE thread_id = %s AND status = %s
                """,
                (
                    new_status,
                    datetime.now(timezone.utc).isoformat(),
                    thread_id,
                    expected_status,
                ),
            )
            row_count = cur.rowcount
        conn.commit()
        return row_count > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)
