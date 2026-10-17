"""Fork Journal for fork_explore — PR-1 core, PR-2 scoring + shadow metrics.

Full branch traces live here, NEVER in the main context (SPEC principle 4):
the session only ever sees the sanitized JoinPacket plus a ``journal_ref``.
PR-2 persists the scoring-pipeline breakdown per branch (judge win-rate,
consensus/cluster size, cost, failed gates) and exposes ``shadow_stats()`` —
fork_rate / win_over_default / wasted_tokens over a window. Battle mode is
only switched on after these numbers are calibrated (SPEC 7.4, 13.4).

State location: profile-aware ``get_hermes_home()/forks/fork_journal.db`` —
never ``~/.hermes`` hardcoded (HX001).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

_RETENTION_RUNS = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fork_run (
  join_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  strategy TEXT NOT NULL,
  winner_ref TEXT,
  prefix_hash TEXT NOT NULL DEFAULT '',
  params_json TEXT NOT NULL DEFAULT '{}',
  packet_json TEXT NOT NULL DEFAULT '{}',
  tokens_total INTEGER NOT NULL DEFAULT 0,
  wasted_tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'joined'
);
CREATE TABLE IF NOT EXISTS fork_branch (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  join_id TEXT NOT NULL,
  branch_ref TEXT NOT NULL,
  strategy_text TEXT NOT NULL DEFAULT '',
  steps_json TEXT NOT NULL DEFAULT '[]',
  intents_json TEXT NOT NULL DEFAULT '[]',
  gates_json TEXT NOT NULL DEFAULT '{}',
  score REAL NOT NULL DEFAULT 0,
  score_components_json TEXT NOT NULL DEFAULT '{}',
  tokens INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  stop_reason TEXT NOT NULL DEFAULT '',
  reads_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_fork_branch_run ON fork_branch(join_id);
CREATE INDEX IF NOT EXISTS idx_fork_run_session ON fork_run(session_id, created_at);
-- PR-2 contract v4 (пошаговая пауза): checkpoint ветки на КАЖДОЙ границе шага —
-- trace/бюджет/step_index. Ветка, отсечённая меж-шаговым гейтом до join,
-- остаётся в журнале полностью; возобновление читает состояние отсюда.
CREATE TABLE IF NOT EXISTS fork_checkpoint (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  join_id TEXT NOT NULL,
  branch_ref TEXT NOT NULL,
  step_index INTEGER NOT NULL,
  created_at REAL NOT NULL,
  tokens INTEGER NOT NULL DEFAULT 0,
  budget_remaining_tokens INTEGER NOT NULL DEFAULT 0,
  cont_state INTEGER,
  stop_reason TEXT NOT NULL DEFAULT '',
  steps_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_fork_cp_run ON fork_checkpoint(join_id, branch_ref, step_index);
"""


def _ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(_SCHEMA)
    # PR-2 migration for journals written by PR-1: add the score-components column.
    cols = {r[1] for r in db.execute("PRAGMA table_info(fork_branch)").fetchall()}
    if "score_components_json" not in cols:
        db.execute("ALTER TABLE fork_branch ADD COLUMN score_components_json"
                   " TEXT NOT NULL DEFAULT '{}'")


def journal_path() -> Path:
    return get_hermes_home() / "forks" / "fork_journal.db"


def write_run(run: dict[str, Any]) -> str:
    """Persist one fork: header row + one trace row per branch. Returns journal_ref."""
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        _ensure_schema(db)
        db.execute(
            "INSERT OR REPLACE INTO fork_run (join_id, session_id, created_at, strategy,"
            " winner_ref, prefix_hash, params_json, packet_json, tokens_total, wasted_tokens,"
            " latency_ms, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (run["join_id"], run.get("session_id", ""), time.time(), run.get("strategy", ""),
             (run.get("winner") or {}).get("path_ref"), run.get("prefix_hash", ""),
             json.dumps(run.get("params") or {}, ensure_ascii=False),
             json.dumps(run.get("packet") or {}, ensure_ascii=False),
             int(run.get("tokens_total", 0)), int(run.get("wasted_tokens", 0)),
             int(run.get("latency_ms", 0)), run.get("status", "joined")))
        for br in run.get("branches", []):
            db.execute(
                "INSERT INTO fork_branch (join_id, branch_ref, strategy_text, steps_json,"
                " intents_json, gates_json, score, score_components_json, tokens,"
                " latency_ms, stop_reason, reads_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (run["join_id"], br.get("ref", ""), br.get("strategy_text", ""),
                 json.dumps(br.get("steps") or [], ensure_ascii=False),
                 json.dumps(br.get("intents") or [], ensure_ascii=False),
                 json.dumps(br.get("gates") or {}, ensure_ascii=False),
                 float(br.get("score", 0)),
                 json.dumps(br.get("score_components") or {}, ensure_ascii=False),
                 int(br.get("tokens", 0)),
                 int(br.get("latency_ms", 0)), br.get("stop_reason", ""),
                 json.dumps(br.get("reads") or [], ensure_ascii=False)))
        # Bound growth: drop whole runs older than the retention window.
        keep = [r[0] for r in db.execute(
            "SELECT join_id FROM fork_run ORDER BY created_at DESC LIMIT ?",
            (_RETENTION_RUNS,)).fetchall()]
        if keep:
            marks = ",".join("?" * len(keep))
            db.execute(f"DELETE FROM fork_branch WHERE join_id NOT IN ({marks})", keep)
            db.execute(f"DELETE FROM fork_checkpoint WHERE join_id NOT IN ({marks})", keep)
            db.execute(f"DELETE FROM fork_run WHERE join_id NOT IN ({marks})", keep)
    return f"journal://forks/{run['join_id']}"


def write_checkpoint(join_id: str, cp: dict[str, Any]) -> None:
    """One branch-boundary checkpoint (stepwise pause). Cheap single-row insert;
    concurrent branch threads each open their own short transaction — the
    busy_timeout absorbs lock contention, and a lost checkpoint is diagnostics,
    never a fork failure (caller must still never die on it)."""
    path = journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=2.0) as db:
        _ensure_schema(db)
        db.execute(
            "INSERT INTO fork_checkpoint (join_id, branch_ref, step_index, created_at,"
            " tokens, budget_remaining_tokens, cont_state, stop_reason, steps_json)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (join_id, cp.get("branch_ref", ""), int(cp.get("step_index", 0)),
             time.time(), int(cp.get("tokens", 0)),
             int(cp.get("budget_remaining_tokens", 0)),
             (None if cp.get("cont_state") is None else int(cp["cont_state"])),
             cp.get("stop_reason", ""),
             json.dumps(cp.get("steps") or [], ensure_ascii=False)))


def get_checkpoints(join_id: str, branch_ref: str) -> list[dict[str, Any]]:
    """Boundary history of one branch, oldest first — the resume view
    (trace/budget/step_index at every pause point)."""
    path = journal_path()
    if not path.exists():
        return []
    with sqlite3.connect(path, timeout=2.0) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(
            "SELECT * FROM fork_checkpoint WHERE join_id=? AND branch_ref=?"
            " ORDER BY step_index, id", (join_id, branch_ref)).fetchall()]


def get_run(join_id: str) -> dict[str, Any] | None:
    path = journal_path()
    if not path.exists():
        return None
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM fork_run WHERE join_id=?", (join_id,)).fetchone()
        if not row:
            return None
        run = dict(row)
        run["branches"] = [dict(b) for b in db.execute(
            "SELECT * FROM fork_branch WHERE join_id=? ORDER BY id", (join_id,)).fetchall()]
    return run


def session_tokens_used(session_id: str, window_s: float = 86400.0) -> int:
    """Tokens burned by this session's forks in the window — budget-lite input."""
    path = journal_path()
    if not path.exists():
        return 0
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT COALESCE(SUM(tokens_total),0) FROM fork_run"
            " WHERE session_id=? AND created_at >= ?",
            (session_id, time.time() - window_s)).fetchone()
    return int(row[0] or 0)


def shadow_stats(window_s: float = 604800.0) -> dict[str, Any]:
    """Shadow-mode calibration view (SPEC 12): aggregates over recent runs so
    boёvка can be judged by data, not vibes.

      fork_rate          forks per day in the window (SPEC 12's per-step rate
                         needs turn counts that live outside the journal; this
                         is the journal-computable proxy)
      win_over_default   share of decided forks whose winner is NOT the
                         first-listed path — if the judge keeps picking the
                         default order anyway, the fork buys nothing
      wasted_tokens      loser/cancelled-branch tokens, total and share
      gate_reject_rate   share of runs that ended in reject (ForkGate was asked
                         to fork where no direction survived)
    """
    path = journal_path()
    empty = {"window_s": window_s, "runs": 0, "runs_shadow": 0, "fork_rate": 0.0,
             "win_over_default": None, "wasted_tokens": 0, "wasted_share": 0.0,
             "gate_reject_rate": 0.0}
    if not path.exists():
        return empty
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT strategy, winner_ref, params_json, tokens_total, wasted_tokens, status"
            " FROM fork_run WHERE created_at >= ?", (time.time() - window_s,)).fetchall()
    runs = len(rows)
    if not runs:
        return empty
    decided = over_default = rejected = shadow_runs = 0
    tokens_sum = wasted_sum = 0
    for strategy, winner_ref, params_json, tokens_total, wasted, status in rows:
        shadow_runs += int((status or "joined") == "shadow")
        tokens_sum += int(tokens_total or 0)
        wasted_sum += int(wasted or 0)
        rejected += int(strategy == "reject")
        if winner_ref:
            decided += 1
            try:
                first = ((json.loads(params_json or "{}").get("paths") or [{}])[0]
                         .get("ref"))
            except (ValueError, AttributeError, IndexError):
                first = None
            if first and winner_ref != first:
                over_default += 1
    return {
        "window_s": window_s,
        "runs": runs,
        "runs_shadow": shadow_runs,
        "fork_rate": round(runs / max(window_s / 86400.0, 1e-9), 3),
        "win_over_default": (round(over_default / decided, 3) if decided else None),
        "wasted_tokens": wasted_sum,
        "wasted_share": round(wasted_sum / tokens_sum, 3) if tokens_sum else 0.0,
        "gate_reject_rate": round(rejected / runs, 3),
    }
