"""controller.reconcile — post-restart / post-crash truth check (spec §43, acceptance rows
'процесс завершился, но результат не записан' and 'после паузы старое run_finished').

Blind retry is forbidden: unfinished runs become 'unknown' first; the caller must pass a
verify_fn that inspects actual side effects (audit log, git state, artifact hashes) before
any attempt>1 is allowed.
"""
from __future__ import annotations

from . import model


def recover(conn, *, verify_fn) -> list[dict]:
    """Run at controller start (after commands.on_restart). Returns a report per run."""
    out = []
    rows = conn.execute("SELECT * FROM ctl_run WHERE run_status IN"
                        " ('starting','running','interrupt_requested')").fetchall()
    for r in rows:
        model.set_run_status(conn, r["run_id"], "unknown")
        verdict = verify_fn(dict(r))            # 'finished'|'interrupted'|'partial'|'not_started'
        if verdict in {"finished", "interrupted"}:
            model.set_run_status(conn, r["run_id"], verdict)
            out.append({"run_id": r["run_id"], "action": "reconciled", "verdict": verdict})
        else:
            out.append({"run_id": r["run_id"], "action": "manual_or_new_attempt",
                        "verdict": verdict})   # new attempt only with basis recorded
    return out


def stale_event(conn, *, task_id: str, run_id: str, perm: dict) -> bool:
    """True when an incoming run_finished belongs to a dead epoch or a task that already
    moved on — the result may be stored, but MUST NOT advance the queue (spec §24)."""
    row = conn.execute("SELECT execution_epoch FROM ctl_run WHERE run_id=?", (run_id,)).fetchone()
    if row is None or row["execution_epoch"] != perm["execution_epoch"]:
        return True
    task = model.get_task(conn, task_id)
    return bool(task and task["state"] not in {"running"})
