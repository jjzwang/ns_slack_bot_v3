# =============================================================================
# Database — PostgreSQL Interview State
# =============================================================================
# One record per Slack thread (thread_id = primary key).
#
# Environment variables:
#   DATABASE_URL  — full connection string (preferred)
#   Or: PGHOST, PGPORT, PGDATABASE, PGUSER, PGPASSWORD

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

# ─── Allowlists for safe dynamic SQL construction ────────────────────────────
# Column names come from **kwargs callers. They MUST be validated against these
# sets before being interpolated into SQL — psycopg2 cannot parameterise
# identifiers (only values), so an explicit allowlist is the safe alternative.

_ALLOWED_UPDATE_COLUMNS = {
    "status", "message_history", "pillars_json", "attempts",
    "review_completed", "review_gaps_json", "review_enrichments_json",
    "review_turn_index", "review_attempts", "updated_at","is_verifying"
}

_ALLOWED_MIGRATION_COLUMNS = {
    "review_completed", "review_gaps_json",
    "review_enrichments_json", "review_turn_index",
    "review_attempts","is_verifying"
}
# ─── Connection Pool ─────────────────────────────────────────────────────────

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def _get_dsn() -> str:
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        return dsn
    host = os.environ.get("PGHOST", "localhost")
    port = os.environ.get("PGPORT", "5432")
    dbname = os.environ.get("PGDATABASE", "gatekeeper")
    user = os.environ.get("PGUSER", "gatekeeper")
    password = os.environ.get("PGPASSWORD", "")
    if not password:
        raise ValueError("Database password not set. Provide DATABASE_URL or PGPASSWORD.")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None or _pool.closed:
        _pool = psycopg2.pool.ThreadedConnectionPool(minconn=2, maxconn=10, dsn=_get_dsn())
    return _pool


def _get_conn():
    return _get_pool().getconn()


def _put_conn(conn):
    try:
        _get_pool().putconn(conn)
    except Exception as e:
        logger.warning(f"Failed to return connection to pool: {e}")


def close_pool() -> None:
    global _pool
    if _pool is not None and not _pool.closed:
        _pool.closeall()
        _pool = None


def init_db() -> None:
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
                    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
                    review_attempts         INTEGER NOT NULL DEFAULT 0,
                    is_verifying            BOOLEAN NOT NULL DEFAULT FALSE
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_interview_state_status ON interview_state (status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_interview_state_user_id ON interview_state (user_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_interview_state_pillars ON interview_state USING GIN (pillars_json)")
            _migrate_add_column(cur, "review_completed", "BOOLEAN NOT NULL DEFAULT FALSE")
            _migrate_add_column(cur, "review_gaps_json", "JSONB NOT NULL DEFAULT '[]'::jsonb")
            _migrate_add_column(cur, "review_enrichments_json", "JSONB NOT NULL DEFAULT '[]'::jsonb")
            _migrate_add_column(cur, "review_turn_index", "INTEGER NOT NULL DEFAULT -1")
            _migrate_add_column(cur, "review_attempts", "INTEGER NOT NULL DEFAULT 0")
            _migrate_add_column(cur, "is_verifying", "BOOLEAN NOT NULL DEFAULT FALSE")
        conn.commit()
        logger.info("Database initialized successfully.")
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


def _migrate_add_column(cur, column: str, definition: str) -> None:
    if column not in _ALLOWED_MIGRATION_COLUMNS:
        raise ValueError(f"Migration: disallowed column name '{column}'")
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'interview_state' AND column_name = %s
    """, (column,))
    if cur.fetchone() is None:
        # Safe: column is validated against _ALLOWED_MIGRATION_COLUMNS above.
        cur.execute(f"ALTER TABLE interview_state ADD COLUMN {column} {definition}")
        logger.info(f"Migration: added column '{column}' to interview_state.")


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
    review_attempts: int = 0
    is_verifying: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def get_history(self) -> list[dict]:
        try:
            if isinstance(self.message_history, list):
                return self.message_history
            return json.loads(self.message_history)
        except (json.JSONDecodeError, TypeError):
            return []

    def set_history(self, history: list[dict]) -> None:
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


def _row_to_state(row: dict) -> InterviewState:
    data = dict(row)
    for json_field in ("pillars_json", "message_history", "review_gaps_json", "review_enrichments_json"):
        val = data.get(json_field)
        if val is not None and not isinstance(val, str):
            data[json_field] = json.dumps(val)
    for ts_field in ("created_at", "updated_at"):
        val = data.get(ts_field)
        if isinstance(val, datetime):
            data[ts_field] = val.isoformat()
    return InterviewState(**data)


def get_state(thread_id: str) -> Optional[InterviewState]:
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM interview_state WHERE thread_id = %s", (thread_id,))
            row = cur.fetchone()
        conn.commit()
        return _row_to_state(row) if row else None
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


def create_state(state: InterviewState) -> None:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO interview_state
                    (thread_id, channel_id, user_id, user_email, user_jira_id,
                     user_display_name, status, pillars_json, message_history,
                     attempts, review_completed, review_gaps_json,
                     review_enrichments_json, review_turn_index, review_attempts, is_verifying,
                     created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s)
                """,
                (
                    state.thread_id, state.channel_id, state.user_id,
                    state.user_email, state.user_jira_id, state.user_display_name,
                    state.status, state.pillars_json, state.message_history,
                    state.attempts, state.review_completed, state.review_gaps_json,
                    state.review_enrichments_json, state.review_turn_index,
                    state.review_attempts,state.is_verifying,
                    state.created_at, state.updated_at,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


def update_state(thread_id: str, **updates) -> None:
    if not updates:
        return

    unknown = set(updates.keys()) - _ALLOWED_UPDATE_COLUMNS
    if unknown:
        raise ValueError(f"update_state called with unknown column(s): {unknown}")

    updates["updated_at"] = datetime.now(timezone.utc).isoformat()

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
            cur.execute(f"UPDATE interview_state SET {set_clause} WHERE thread_id = %s", values)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)


def try_lock_state(thread_id: str, expected_status: str, new_status: str) -> bool:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE interview_state
                SET status = %s, updated_at = %s
                WHERE thread_id = %s AND status = %s
                """,
                (new_status, datetime.now(timezone.utc).isoformat(), thread_id, expected_status),
            )
            row_count = cur.rowcount
        conn.commit()
        return row_count > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)
