"""Durable pending-approval ledger (state.db / pending_approvals).

Fixes the dead-letter at the heart of #133514: when a detached background
child (or any gateway path with no live notify callback) hits an approval
gate, ``tools/approval._pending_result`` used to park the request in a
module-global ``_pending`` dict that **no surface ever read** — the request
was unanswerable and silently overwritten by the next one.

This ledger persists every such request so ``/approve`` / ``/deny`` stay
answerable after the parent turn ends, across processes, and (once merged)
across restarts. Rows FIFO-resolve under the same session key the live
gateway queue uses (background children inherit the parent's approval
contextvars via ``thread_context``, so the child's request is already keyed
to the parent conversation).

Design notes:
- Default OFF (``approvals.durable_pending_ledger: false``): behavior is
  byte-identical to the dict path until a deployment flips the flag. The
  in-memory dict is kept as a fast cache so no existing reader regresses.
- Single short-lived connection per operation (same pattern as
  ``tools/async_delegation``): the approval path must never hold DB locks
  across user interaction. Every entry point swallows DB errors — the
  ledger may degrade to the legacy dict, it must never break a tool call.
- No wake plumbing here: resolving a row also records the pattern as
  session-approved (``approve_session``) so a re-run in the same session
  passes the gate without a second card. Notifying the agent that the
  answer arrived is the caller's next turn's job (see PR body for the
  deliberate scope cut).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_approvals (
    approval_id TEXT PRIMARY KEY,
    session_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    resolution TEXT,
    resolved_at REAL,
    reason TEXT,
    command TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    pattern_key TEXT NOT NULL DEFAULT '',
    extra_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_pending_approvals_lookup
    ON pending_approvals(session_key, state, created_at);
"""


def _enabled() -> bool:
    try:
        from tools.approval_context import _get_approval_config

        return bool(_get_approval_config().get("durable_pending_ledger", False))
    except Exception:
        # Config subsystem unavailable: treat as flag-off (legacy dict path).
        logger.debug("approval ledger: config read failed; staying on dict path", exc_info=True)
        return False


def _ttl_seconds() -> int:
    try:
        from tools.approval_context import _get_approval_config

        return max(30, int(_get_approval_config().get("pending_ttl_seconds", 1800)))
    except Exception:
        logger.debug("approval ledger: ttl config read failed; using default", exc_info=True)
        return 1800


def _connect() -> sqlite3.Connection:
    from hermes_cli.sqlite_util import open_db
    from hermes_constants import mkdir_under_hermes_home, get_hermes_home
    from hermes_state import _secure_state_db_files

    path = get_hermes_home() / "state.db"
    mkdir_under_hermes_home(path.parent)
    _secure_state_db_files(path, create_main=True)
    conn = open_db(
        path,
        db_label="state.db (approval_ledger)",
        busy_timeout_ms=10_000,
        wal=False,
        row_factory=None,
        initialize=None,
    )
    _secure_state_db_files(path)
    conn.executescript(_SCHEMA)
    return conn


def record_pending(session_key: str, pending: dict) -> str | None:
    """Durably store one unanswerable approval request. Returns its id, or
    None when the feature is off or the write failed (legacy dict still
    holds the copy in that case). Multiple rows per session accumulate —
    unlike the dict, a later request never overwrites an earlier one."""
    if not _enabled() or not session_key:
        return None
    approval_id = f"ap-{uuid.uuid4().hex[:12]}"
    now = time.time()
    extra = {
        k: v
        for k, v in pending.items()
        if k not in ("command", "description", "pattern_key", "pattern_keys")
    }
    try:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO pending_approvals (approval_id, session_key, created_at,"
                    " expires_at, state, command, description, pattern_key, extra_json)"
                    " VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                    (
                        approval_id,
                        session_key,
                        now,
                        now + _ttl_seconds(),
                        str(pending.get("command", "")),
                        str(pending.get("description", "")),
                        str(pending.get("pattern_key", "")),
                        json.dumps(extra, default=str),
                    ),
                )
        finally:
            conn.close()
        return approval_id
    except Exception:
        logger.debug(
            "approval ledger: record failed for %s (dict fallback stays)",
            session_key,
            exc_info=True,
        )
        return None


def list_pending(session_key: str) -> list[dict]:
    """Unexpired pending rows for a session, oldest first. Rows past their
    TTL are flipped to 'expired' as a side effect (lazy GC, no scheduler)."""
    if not _enabled() or not session_key:
        return []
    now = time.time()
    try:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE pending_approvals SET state='expired'"
                    " WHERE session_key=? AND state='pending' AND expires_at < ?",
                    (session_key, now),
                )
                rows = conn.execute(
                    "SELECT approval_id, created_at, command, description, pattern_key"
                    " FROM pending_approvals"
                    " WHERE session_key=? AND state='pending' ORDER BY created_at ASC",
                    (session_key,),
                ).fetchall()
        finally:
            conn.close()
        return [
            {
                "approval_id": r[0],
                "created_at": r[1],
                "command": r[2],
                "description": r[3],
                "pattern_key": r[4],
            }
            for r in rows
        ]
    except Exception:
        logger.debug("approval ledger: list failed for %s", session_key, exc_info=True)
        return []


def pending_count(session_key: str | None = None) -> int:
    """Unexpired pending rows for one session (or across all sessions when
    *session_key* is None). Same lazy-GC side effect as list_pending."""
    if not _enabled():
        return 0
    now = time.time()
    try:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE pending_approvals SET state='expired' WHERE state='pending'"
                    " AND expires_at < ?",
                    (now,),
                )
                q = "SELECT COUNT(*) FROM pending_approvals WHERE state='pending'" + (
                    " AND session_key=?" if session_key else ""
                )
                n = conn.execute(q, (session_key,) if session_key else ()).fetchone()[0]
        finally:
            conn.close()
        return int(n)
    except Exception:
        logger.debug("approval ledger: count failed (session_key=%s)", session_key, exc_info=True)
        return 0


def resolve_pending(
    session_key: str,
    choice: str,
    *,
    resolve_all: bool = False,
    reason: str | None = None,
    request_id: str | None = None,
) -> list[dict]:
    """Answer durable rows the way ``/approve``/``/deny`` answer the live
    queue: oldest-first FIFO, or ``all``, or one by *request_id*. Marks
    state and returns the resolved rows (for pattern pre-approval and
    logging). A row already resolved/expired/canceled is never double-
    answered: the UPDATE is guarded on state='pending' and rowcount."""
    if not _enabled() or not session_key:
        return []
    now = time.time()
    resolved: list[dict] = []
    try:
        conn = _connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE pending_approvals SET state='expired'"
                    " WHERE session_key=? AND state='pending' AND expires_at < ?",
                    (session_key, now),
                )
                if request_id:
                    rows = conn.execute(
                        "SELECT approval_id, command, description, pattern_key FROM"
                        " pending_approvals WHERE session_key=? AND state='pending'"
                        " AND approval_id=?",
                        (session_key, request_id),
                    ).fetchall()
                elif resolve_all:
                    rows = conn.execute(
                        "SELECT approval_id, command, description, pattern_key FROM"
                        " pending_approvals WHERE session_key=? AND state='pending'"
                        " ORDER BY created_at ASC",
                        (session_key,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT approval_id, command, description, pattern_key FROM"
                        " pending_approvals WHERE session_key=? AND state='pending'"
                        " ORDER BY created_at ASC LIMIT 1",
                        (session_key,),
                    ).fetchall()
                for r in rows:
                    cur = conn.execute(
                        "UPDATE pending_approvals SET state=?, resolution=?, resolved_at=?,"
                        " reason=? WHERE approval_id=? AND state='pending'",
                        (
                            choice
                            if choice in ("approved", "denied")
                            else (
                                "approved"
                                if choice in ("once", "session", "always")
                                else "denied"
                            ),
                            choice,
                            now,
                            reason,
                            r[0],
                        ),
                    )
                    if cur.rowcount == 1:
                        resolved.append(
                            {
                                "approval_id": r[0],
                                "command": r[1],
                                "description": r[2],
                                "pattern_key": r[3],
                            }
                        )
        finally:
            conn.close()
        return resolved
    except Exception:
        logger.debug(
            "approval ledger: resolve failed for %s", session_key, exc_info=True
        )
        return []


def cancel_session(session_key: str) -> int:
    """Subtree teardown (/stop, /new, session end): every still-pending row
    for the session becomes 'cancelled'. Returns rows cancelled."""
    if not _enabled() or not session_key:
        return 0
    try:
        conn = _connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE pending_approvals SET state='cancelled', resolved_at=?"
                    " WHERE session_key=? AND state='pending'",
                    (time.time(), session_key),
                )
                return cur.rowcount
        finally:
            conn.close()
    except Exception:
        logger.debug(
            "approval ledger: cancel failed for %s", session_key, exc_info=True
        )
        return 0
