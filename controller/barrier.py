"""controller.barrier — the pre-dispatch check (spec §7 'race', §20; MVP §3).

Checked at DISPATCH time (when the action is about to run), not at turn start: the
acceptance race 'pause arrived between choosing and starting' is closed because the
epoch is re-read from the DB on every autonomous dispatch.

Two jobs, deliberately separate (spec §33):
  * admit()/check(): stop NEW autonomous actions           -> this module
  * stop a RUNNING process                                 -> core interrupt_control
    (controller marks task paused and issues interrupt; run_status carries the truth)
"""
from __future__ import annotations

from . import model


def check(conn, *, scope: str, origin: str, task_id: str | None = None,
          action_class: str = "ordinary") -> tuple[bool, str]:
    """Called from pre_tool_call for origin='autonomous' actions only (supervisor tags
    children; the router tags the queue loop). Interactive/user-requested steps bypass
    the permission check but never resume the queue (spec: 'после паузы отдельный шаг')."""
    if origin != "autonomous":
        return True, "interactive"
    perm = model.get_permission(conn, scope)
    if not perm["enabled"]:
        return False, "autonomy paused/off — queue not advancing"
    if task_id:
        task = model.get_task(conn, task_id)
        if task is None:
            return False, "unknown task"
        if task["state"] not in {"running"}:
            return False, f"task state {task['state']} does not authorize dispatch"
        if task["contract_version"] != _run_contract(conn, task_id):
            return False, "contract changed under the active run"
        if model.budget_exhausted(conn, task_id, "tool_calls"):
            return False, "budget exhausted"
        if action_class != "ordinary":
            row = conn.execute(
                "SELECT 1 FROM ctl_approval WHERE task_id=? AND action_class=? AND revoked_at IS NULL"
                " AND contract_version=? AND execution_epoch=?",
                (task_id, action_class, task["contract_version"],
                 perm["execution_epoch"])).fetchone()
            if not row:
                return False, f"action class '{action_class}' requires approval at this epoch"
    return True, "ok"


def _run_contract(conn, task_id: str) -> int | None:
    row = conn.execute("SELECT contract_version FROM ctl_run WHERE task_id=? AND ended_at IS NULL"
                       " ORDER BY started_at DESC LIMIT 1", (task_id,)).fetchone()
    return row["contract_version"] if row else None


def on_pause(conn, *, scope: str, interrupt_fn) -> dict:
    """Pause = epoch bump (no new dispatches) + safe stop of what's already running.
    interrupt_fn: injectable core interrupt (agent.interrupt_control). Confirms via
    run_status=interrupted->interrupted (spec §34)."""
    p = model.set_permission(conn, scope, enabled=False, granted_via="user-command")
    rows = conn.execute("SELECT run_id FROM ctl_run WHERE run_status IN ('starting','running')"
                        " AND execution_epoch < ?", (p["execution_epoch"],)).fetchall()
    for r in rows:
        model.set_run_status(conn, r["run_id"], "interrupt_requested")
        interrupt_fn(r["run_id"])
    return {"epoch": p["execution_epoch"], "interrupting": [r["run_id"] for r in rows]}


def confirm_interrupted(conn, run_id: str) -> None:
    """Worker/core reports the process actually stopped -> §34 confirmation recorded."""
    model.set_run_status(conn, run_id, "interrupted")
