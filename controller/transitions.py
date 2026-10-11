"""controller.transitions — the task state machine (spec §16).

allowed(from)->to table, guard per transition, every edge records a basis row.
Terminal states (completed, cancelled) cannot re-enter ready/anything by a random
event; only reopen_new_task/reopen_revision — never an event — moves them back
(acceptance: "не возвращай completed/cancelled в ready по случайному событию").
"""
from __future__ import annotations

import time

from . import model

ALLOWED: dict[str, set[str]] = {
    "draft": {"needs_clarification", "deferred", "ready"},
    "needs_clarification": {"draft", "deferred", "ready"},
    "deferred": {"ready"},
    "ready": {"running"},
    "running": {"verifying", "blocked", "paused", "needs_human", "failed", "cancelled"},
    "verifying": {"completed", "ready", "needs_human", "failed", "cancelled"},
    "blocked": {"ready", "cancelled"},
    "paused": {"ready", "cancelled"},
    "needs_human": {"ready", "cancelled"},
    "completed": set(),
    "cancelled": set(),
    "failed": {"ready", "cancelled"},   # retry after failed -> ready is allowed with basis
}

TERMINAL = {"completed", "cancelled"}

# Guards: callable(conn, task, **kw) -> str | None; None = ok, str = denial reason.
GUARDS: dict[tuple[str, str], list] = {
    # entering running requires: autonomy enabled for scope + material contract agreed
    ("ready", "running"): [
        lambda conn, t, **kw: None if model.material_agreed(conn, t["task_id"])
        else "contract MATERIAL revision not agreed",
        lambda conn, t, **kw: None if (
            (perm := kw.get("permission")) and perm["enabled"]
            and (kw.get("expected_epoch") is None
                 or perm["execution_epoch"] == kw["expected_epoch"]))
        else "autonomy permission absent/stale-epoch",
        lambda conn, t, **kw: None if not model.budget_exhausted(conn, t["task_id"], "tool_calls")
        else "budget exhausted: tool_calls",
    ],
    # verifying -> completed requires a current passing review for every checkpoint step (§46)
    ("verifying", "completed"): [
        lambda conn, t, **kw: _reviews_current(conn, t, kw),
    ],
}


def _reviews_current(conn, task, kw) -> str | None:
    rows = conn.execute(
        "SELECT r.verdict, r.artifact_hash, a.payload, s.step_id FROM ctl_review r"
        " JOIN ctl_step s ON s.step_id=r.step_id"
        " LEFT JOIN ctl_artifact_revision a ON a.artifact_rev_id=("
        "   SELECT artifact_rev_id FROM ctl_artifact_revision a2"
        "   WHERE a2.run_id=r.run_id ORDER BY a2.created_at DESC LIMIT 1)"
        " WHERE s.task_id=? AND s.checkpoint=1 AND s.status='done'",
        (task["task_id"],)).fetchall()
    steps = conn.execute("SELECT COUNT(*) c FROM ctl_step WHERE task_id=? AND checkpoint=1",
                         (task["task_id"],)).fetchone()["c"]
    if len(rows) < steps:
        return f"missing reviews: {len(rows)}/{steps} checkpoint steps"
    for r in rows:
        if r["verdict"] != "pass":
            return f"review verdict {r['verdict']} on {r['step_id']}"
        import json, hashlib
        cur = json.loads(r["payload"] or "{}")
        cur_hash = cur.get("hash") or hashlib.sha256(
            json.dumps(cur, sort_keys=True).encode()).hexdigest()
        if r["artifact_hash"] != cur_hash:
            return "review stale: artifact changed after review"
    return None


def transition(conn, task_id: str, to_state: str, *, basis: str,
               permission: dict | None = None, expected_epoch: int | None = None) -> dict:
    task = model.get_task(conn, task_id)
    if task is None:
        raise LookupError(task_id)
    frm = task["state"]
    if to_state not in ALLOWED.get(frm, set()):
        raise ValueError(f"illegal transition {frm} -> {to_state}")
    kw = {"permission": permission, "expected_epoch": expected_epoch}
    for guard in GUARDS.get((frm, to_state), []):
        deny = guard(conn, dict(task), **kw)
        if deny:
            raise ValueError(f"guard {frm}->{to_state}: {deny}")
    conn.execute("INSERT INTO ctl_state_transition(ts,task_id,from_state,to_state,basis)"
                 " VALUES (?,?,?,?,?)", (time.time(), task_id, frm, to_state, basis))
    model.set_task_state(conn, task_id, to_state)
    return {"task_id": task_id, "from": frm, "to": to_state}
