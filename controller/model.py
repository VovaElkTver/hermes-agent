"""controller.model — durable entities for managed autonomy (spec §12–16, 24, 36).

Tables live in state.db under the ctl_* prefix, created by the canonical SCHEMA_SQL
(hermes_state_common). Connection reuses the async_delegation precedent: open_db +
reconcile_state_schema, no second DDL authority.

Task state and Run status are SEPARATE columns on SEPARATE rows — a task may be
cancelled while its run is still interrupt_requested (spec §15).

Every mutation goes through here or through transitions.py; nothing outside writes
these tables. Idempotency: ctl_event.idempotency_key UNIQUE — a duplicate delivery
is absorbed, not replayed (spec §23).
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path


def _connect() -> sqlite3.Connection:
    """Canonical opener: same barriers/authority as async_delegation (state.db)."""
    from hermes_constants import get_hermes_home
    from hermes_cli.sqlite_util import open_db
    from hermes_state_repair import apply_durability_barriers
    from hermes_state_schema import reconcile_state_schema

    path = get_hermes_home() / "state.db"

    def initialize(conn: sqlite3.Connection) -> None:
        apply_durability_barriers(conn)
        reconcile_state_schema(conn)

    return open_db(path, db_label="state.db (controller)", busy_timeout_ms=10_000,
                   wal=False, row_factory=sqlite3.Row, initialize=initialize)


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Tests pass an explicit temp path (raw SCHEMA_SQL from hermes_state_common)."""
    if db_path is not None:
        from hermes_state_common import SCHEMA_SQL
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA_SQL)
        return conn
    return _connect()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ---- tasks ----------------------------------------------------------------

def create_task(conn, *, owner: str, workdir: str, goal: str, project_id: str = "",
                kanban_ref: str | None = None, position: str = "tail") -> str:
    """position='head' jumps the queue (priority = max+1 among ready/running in this
    workdir), 'tail' (default) queues FIFO at the back. Priority lives in the contract,
    equal priorities keep strict FIFO (queue.py sorts by -priority, updated_at)."""
    if position not in ("head", "tail"):
        raise ValueError(f"unknown queue position: {position}")
    tid = new_id("task")
    now = time.time()
    conn.execute("INSERT INTO ctl_task VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (tid, owner, project_id, str(Path(workdir).resolve()), goal, "draft", 1,
                  kanban_ref, now, now))
    conn.execute(
        "INSERT INTO ctl_contract_revision (task_id,version,change_class,fields,supersedes,agreed_at)"
        " VALUES (?,?,?,?,?,?)",
        (tid, 1, "MATERIAL",
         json.dumps(_empty_contract(goal, priority=_position_priority(conn, workdir, position)),
                    ensure_ascii=False), None, None))
    conn.commit()
    return tid


def _position_priority(conn, workdir: str, position: str) -> float:
    """head = one above every live task in the scope; tail = one below (keeps FIFO at 0
    for the common case since default priority is 0)."""
    row = conn.execute(
        "SELECT MIN(CAST(json_extract(fields,'$.priority') AS REAL)) lo,"
        " MAX(CAST(json_extract(fields,'$.priority') AS REAL)) hi"
        " FROM ctl_task t JOIN ctl_contract_revision c"
        "   ON c.task_id=t.task_id AND c.version=t.contract_version"
        " WHERE t.workdir=? AND t.state IN ('ready','running')",
        (str(Path(workdir).resolve()),)).fetchone()
    if position == "head":
        return (row["hi"] or 0) + 1
    lo = row["lo"]
    return (lo - 1) if lo is not None and lo < 0 else 0.0


def _empty_contract(goal: str, priority: float = 0.0) -> dict:
    return {"goal": goal, "priority": priority, "acceptance": [], "decisions": [], "allowed_actions": [],
            "approval_required": [], "restrictions": [], "facts": [], "hypotheses": [],
            "proposals": [], "open_questions": [], "input_artifacts": [], "dependencies": [],
            "start_conditions": [], "budgets": {}, "stop_conditions": [],
            "reclassification_rules": [], "notify_channel": ""}


def set_priority(conn, task_id: str, *, head: bool) -> None:
    """Move a task in its scope's queue via contract priority (editorial revision —
    no re-agreement). head=above all live tasks, tail=min-1 (keeps FIFO below)."""
    row = conn.execute("SELECT t.workdir, t.state FROM ctl_task t WHERE t.task_id=?",
                       (task_id,)).fetchone()
    if row is None:
        raise LookupError(f"no such task: {task_id}")
    if row["state"] not in ("draft", "ready"):
        raise ValueError(f"cannot requeue a {row['state']} task")
    p = _position_priority(conn, row["workdir"], "head" if head else "tail")
    cur = conn.execute("SELECT fields FROM ctl_contract_revision WHERE task_id=? AND version=("
                       "SELECT MAX(version) FROM ctl_contract_revision WHERE task_id=?)",
                       (task_id, task_id)).fetchone()
    fields = json.loads(cur["fields"] or "{}")
    fields["priority"] = p
    revise_contract(conn, task_id, fields, "EDITORIAL")


def get_task(conn, task_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ctl_task WHERE task_id=?", (task_id,)).fetchone()


def set_task_state(conn, task_id: str, state: str) -> None:
    conn.execute("UPDATE ctl_task SET state=?, updated_at=? WHERE task_id=?",
                 (state, time.time(), task_id))
    conn.commit()


# ---- contracts --------------------------------------------------------------

def revise_contract(conn, task_id: str, fields: dict, change_class: str) -> int:
    """MATERIAL needs agreement before it authorises work; editorial/detail chain silently."""
    cur = conn.execute("SELECT MAX(version) v FROM ctl_contract_revision WHERE task_id=?",
                       (task_id,)).fetchone()
    nxt = (cur["v"] or 0) + 1
    conn.execute(
        "INSERT INTO ctl_contract_revision (task_id,version,change_class,fields,supersedes,agreed_at)"
        " VALUES (?,?,?,?,?,?)",
        (task_id, nxt, change_class, json.dumps(fields, ensure_ascii=False),
         cur["v"], time.time() if change_class != "MATERIAL" else None))
    conn.execute("UPDATE ctl_task SET contract_version=?, updated_at=? WHERE task_id=?",
                 (nxt, time.time(), task_id))
    conn.commit()
    return nxt


def current_contract(conn, task_id: str) -> dict:
    row = conn.execute(
        "SELECT * FROM ctl_contract_revision WHERE task_id=? ORDER BY version DESC LIMIT 1",
        (task_id,)).fetchone()
    return json.loads(row["fields"]) if row else {}


def material_agreed(conn, task_id: str) -> bool:
    row = conn.execute(
        "SELECT agreed_at FROM ctl_contract_revision WHERE task_id=? ORDER BY version DESC LIMIT 1",
        (task_id,)).fetchone()
    return bool(row and row["agreed_at"])


def agree_contract(conn, task_id: str) -> None:
    conn.execute("UPDATE ctl_contract_revision SET agreed_at=? WHERE task_id=? AND version=("
                 "SELECT MAX(version) FROM ctl_contract_revision WHERE task_id=?)",
                 (time.time(), task_id, task_id))
    conn.commit()


# ---- autonomy permission (spec §7, §36) ------------------------------------

def get_permission(conn, scope: str) -> dict:
    row = conn.execute("SELECT * FROM ctl_autonomy_permission WHERE scope=?", (scope,)).fetchone()
    if not row:
        return {"scope": scope, "enabled": 0, "execution_epoch": 0, "granted_via": None}
    return dict(row)


def set_permission(conn, scope: str, *, enabled: bool, granted_via: str) -> dict:
    """Enable/resume bumps the epoch: grants issued under an older epoch are dead (§36).
    Grant source is closed-world: doc text ('doc') can never appear here (§45)."""
    if granted_via not in {"user-command", "recovered-autonomous", "reconcile"}:
        raise ValueError(f"illegal autonomy grant source: {granted_via}")
    cur = get_permission(conn, scope)
    epoch = cur["execution_epoch"] + 1
    conn.execute(
        "INSERT INTO ctl_autonomy_permission VALUES (?,?,?,?,?)"
        " ON CONFLICT(scope) DO UPDATE SET enabled=excluded.enabled,"
        " execution_epoch=excluded.execution_epoch, granted_via=excluded.granted_via,"
        " updated_at=excluded.updated_at",
        (scope, 1 if enabled else 0, epoch, granted_via, time.time()))
    conn.commit()
    return get_permission(conn, scope)


# ---- events -----------------------------------------------------------------

def emit(conn, *, kind: str, origin: str, task_id: str | None = None,
         payload: dict | None = None, idempotency_key: str | None = None) -> str | None:
    """Returns event_id, or None when a duplicate (same idempotency_key) was absorbed."""
    eid = new_id("evt")
    try:
        conn.execute("INSERT INTO ctl_event VALUES (?,?,?,?,?,?,?)",
                     (eid, time.time(), kind, task_id, origin,
                      json.dumps(payload or {}, ensure_ascii=False), idempotency_key))
        conn.commit()
        return eid
    except sqlite3.IntegrityError:
        return None  # duplicate delivery — side effect must not repeat


# ---- runs -------------------------------------------------------------------

def start_run(conn, *, task_id: str, step_id: str, contract_version: int,
              execution_epoch: int, worker_id: str = "",
              input_artifacts: list | None = None, authority: list | None = None,
              attempt: int = 1) -> str:
    rid = new_id("run")
    conn.execute(
        "INSERT INTO ctl_run (run_id,task_id,step_id,attempt,worker_id,contract_version,"
        "execution_epoch,input_artifacts,authority,run_status,started_at,ended_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
        (rid, task_id, step_id, attempt, worker_id, contract_version,
         execution_epoch, json.dumps(input_artifacts or []),
         json.dumps(authority or []), "starting", time.time()))
    conn.commit()
    return rid


RUN_STATUSES = {"starting", "running", "interrupt_requested", "interrupted",
                "finished", "failed", "unknown"}


def set_run_status(conn, run_id: str, status: str) -> None:
    assert status in RUN_STATUSES, status
    ended = time.time() if status in {"finished", "failed", "interrupted", "unknown"} else None
    conn.execute("UPDATE ctl_run SET run_status=?, ended_at=COALESCE(?, ended_at) WHERE run_id=?",
                 (status, ended, run_id))
    conn.commit()


# ---- budgets (spec §41) ------------------------------------------------------

def add_budget(conn, task_id: str, metric: str, amount: float = 1) -> dict:
    conn.execute("INSERT INTO ctl_budget_counter(task_id, metric, used) VALUES (?,?,?)"
                 " ON CONFLICT(task_id, metric) DO UPDATE SET used = used + excluded.used",
                 (task_id, metric, amount))
    conn.commit()
    return dict(conn.execute("SELECT * FROM ctl_budget_counter WHERE task_id=? AND metric=?",
                             (task_id, metric)).fetchone())


def budget_exhausted(conn, task_id: str, metric: str) -> bool:
    row = conn.execute("SELECT used, limit_value FROM ctl_budget_counter WHERE task_id=? AND metric=?",
                       (task_id, metric)).fetchone()
    return bool(row and row["limit_value"] is not None and row["used"] >= row["limit_value"])


# ---- steps / artifacts / reviews --------------------------------------------

def add_step(conn, task_id: str, kind: str, title: str, *, checkpoint: bool = True,
             depends_on: list | None = None) -> str:
    sid = new_id("step")
    conn.execute("INSERT INTO ctl_step VALUES (?,?,?,?,?,?,?)",
                 (sid, task_id, kind, title, "pending", 1 if checkpoint else 0,
                  json.dumps(depends_on or [])))
    conn.commit()
    return sid


def finish_step(conn, step_id: str, status: str = "done") -> None:
    conn.execute("UPDATE ctl_step SET status=? WHERE step_id=?", (status, step_id))
    conn.commit()


# ---- step preemption (PR-A: paused/resume for ctl steps) --------------------
#
# A running worker marks its step 'in_progress'; an out-of-band preempt (scheduler via
# control socket) flips in_progress -> paused WITHOUT letting the turn finish, storing the
# handoff note ("чем занимался, на какой тулзе встал") in a step_paused event so the next
# dispatch resumes cheaply instead of re-narrating. resume_step puts it back to pending —
# queue.next_step picks only 'pending', so paused steps are invisible to dispatch until
# resumed. Each flip is a compare-and-set under BEGIN IMMEDIATE with a post-write
# read-back: the preempt never trusts the worker's exit status for what the step's real
# state became underneath it.

STEP_STATUSES = frozenset({"pending", "in_progress", "paused", "done", "blocked", "failed"})


def get_step(conn, step_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM ctl_step WHERE step_id=?", (step_id,)).fetchone()


def _set_step_status_cas(conn, step_id: str, *, expect: str, new: str,
                         task_id: str | None = None, note: str = "",
                         event_kind: str | None = None) -> dict:
    assert new in STEP_STATUSES, new
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT status, task_id FROM ctl_step WHERE step_id=?",
                           (step_id,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return {"ok": False, "step_id": step_id, "stored": None,
                    "error": f"unknown step {step_id!r}"}
        stored = row["status"]
        if stored != expect:
            conn.execute("ROLLBACK")
            return {"ok": False, "step_id": step_id, "stored": stored, "expected": expect}
        conn.execute("UPDATE ctl_step SET status=? WHERE step_id=?", (new, step_id))
        if note and event_kind:
            conn.execute(
                "INSERT INTO ctl_event VALUES (?,?,?,?,?,?,?)",
                (new_id("evt"), time.time(), event_kind,
                 task_id or row["task_id"], "controller",
                 json.dumps({"step_id": step_id, "handoff": note}, ensure_ascii=False),
                 None))
        actual = conn.execute("SELECT status FROM ctl_step WHERE step_id=?",
                              (step_id,)).fetchone()
        if actual is None or actual["status"] != new:
            conn.execute("ROLLBACK")
            return {"ok": False, "step_id": step_id,
                    "stored": actual["status"] if actual else None,
                    "error": "read-back mismatch after CAS"}
        conn.commit()
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return {"ok": True, "step_id": step_id, "stored": new, "previous": stored,
            "task_id": row["task_id"]}


def mark_step_in_progress(conn, step_id: str) -> dict:
    """Worker-side dispatch claim: pending -> in_progress (CAS)."""
    return _set_step_status_cas(conn, step_id, expect="pending", new="in_progress")


def pause_step(conn, step_id: str, *, note: str = "", task_id: str | None = None) -> dict:
    """Out-of-band preempt: in_progress -> paused + handoff note (CAS). A step already
    paused externally (or finished) reports ok:false with the stored status — the
    scheduler acts on the READ-BACK state, never on the worker's exit status."""
    return _set_step_status_cas(conn, step_id, expect="in_progress", new="paused",
                                task_id=task_id, note=note, event_kind="step_paused")


def resume_step(conn, step_id: str) -> dict:
    """Return a paused step to dispatch: paused -> pending (CAS). Re-anchoring (fresh
    contract text + the stored handoff note) happens at the next dispatch, see
    loop.run_cycle / paused_handoff_note."""
    return _set_step_status_cas(conn, step_id, expect="paused", new="pending")


def unstick_step(conn, step_id: str) -> dict:
    """Worker ended normally (fail/revise path) — release an in_progress claim back to
    pending so the step stays dispatchable (CAS)."""
    return _set_step_status_cas(conn, step_id, expect="in_progress", new="pending")


def paused_handoff_note(conn, step_id: str) -> str:
    """The newest step_paused handoff for a step ('' when none) — the cheap-resume text
    the re-anchored worker receives instead of re-narrating its interrupted turn."""
    row = conn.execute(
        "SELECT payload FROM ctl_event WHERE kind='step_paused'"
        " AND json_extract(payload,'$.step_id')=? ORDER BY ts DESC LIMIT 1",
        (step_id,)).fetchone()
    if row is None:
        return ""
    try:
        return str(json.loads(row["payload"]).get("handoff") or "")
    except (ValueError, TypeError):
        return ""


def add_artifact(conn, task_id: str, run_id: str, kind: str, payload: dict) -> str:
    aid = new_id("art")
    conn.execute("INSERT INTO ctl_artifact_revision VALUES (?,?,?,?,?,?)",
                 (aid, task_id, run_id, kind, json.dumps(payload, ensure_ascii=False),
                  time.time()))
    conn.commit()
    return aid


def add_review(conn, step_id: str, run_id: str, artifact_hash: str, verdict: str,
               reviewer: str, detail: str = "") -> str:
    rid = new_id("rev")
    conn.execute("INSERT INTO ctl_review VALUES (?,?,?,?,?,?,?,?)",
                 (rid, step_id, run_id, artifact_hash, verdict, reviewer, detail, time.time()))
    conn.commit()
    return rid


def grant_approval(conn, task_id: str, action_class: str, contract_version: int,
                   execution_epoch: int, granted_by: str) -> str:
    aid = new_id("apr")
    conn.execute("INSERT INTO ctl_approval VALUES (?,?,?,?,?,?,?,?)",
                 (aid, task_id, contract_version, action_class, execution_epoch,
                  granted_by, time.time(), None))
    conn.commit()
    return aid


# ---- restart marker (Q5: durable cause-of-restart) ---------------------------

def mark_restart_intent(conn, reason: str = "operator") -> None:
    conn.execute("INSERT INTO ctl_kv VALUES ('restart_intent', ?)"
                 " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (reason,))
    conn.commit()


def consume_restart_intent(conn) -> str | None:
    row = conn.execute("SELECT value FROM ctl_kv WHERE key='restart_intent'").fetchone()
    if row:
        conn.execute("DELETE FROM ctl_kv WHERE key='restart_intent'")
        conn.commit()
        return row["value"]
    return None
