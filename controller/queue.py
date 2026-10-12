"""controller.queue — task selection (spec §17–19) for one scope.

Selection is deterministic code: state='ready', deps satisfied, not_before reached,
priority order. The LLM never picks the next task (a proposal can reorder via a
user-approved contract edit only).

Reselect-after-step (Architect-approved, variant A): order is pure
(-priority, updated_at) over ready+running — a running task holds NO sticky claim on
the queue. After each completed step the scheduler reselects, so a task continues
only while nothing outranks it; a head-requeued ready task runs its next step before
another task's following step. The in-flight run is never cut short: loop.py skips
dispatch for any task with an open run, so a step always finishes fully and a task
can never get a second concurrent run.
"""
from __future__ import annotations

import time

from . import model

OPEN_RUN_STATUSES = ("starting", "running", "interrupt_requested")


def has_open_run(conn, task_id: str) -> bool:
    """True while the task has a run that never reached a terminal status — the same
    non-terminal set reconcile.py scans. Dispatch must skip such a task: one run at a
    time per task, no mid-run kill (defense in depth for reselect-after-step)."""
    q = ",".join("?" * len(OPEN_RUN_STATUSES))
    row = conn.execute(
        f"SELECT 1 FROM ctl_run WHERE task_id=? AND run_status IN ({q}) LIMIT 1",
        (task_id, *OPEN_RUN_STATUSES)).fetchone()
    return row is not None


def eligible(conn, scope: str, now: float | None = None) -> list[dict]:
    now = now or time.time()
    rows = conn.execute(
        "SELECT t.*, c.fields AS cfields FROM ctl_task t"
        " JOIN ctl_contract_revision c ON c.task_id=t.task_id AND c.version=t.contract_version"
        " WHERE t.workdir=? AND t.state IN ('ready','running')",
        (scope,)).fetchall()
    import json
    out = []
    for r in rows:
        fields = json.loads(r["cfields"] or "{}")
        sc = fields.get("start_conditions") or {}
        not_before = sc.get("not_before", 0) if isinstance(sc, dict) else 0
        if not_before > now:
            continue
        deps = fields.get("dependencies") or []
        if isinstance(deps, str):
            deps = json.loads(deps or "[]")
        if deps and not all(_dep_done(conn, d) for d in deps):
            continue
        pr = fields.get("priority", 0)
        try:
            pr = float(pr)
        except (TypeError, ValueError):
            pr = 0.0
        out.append({**dict(r), "priority": pr})
    out.sort(key=lambda t: (-t["priority"], t["updated_at"]))
    return out


def _dep_done(conn, dep_task_id: str) -> bool:
    row = conn.execute("SELECT state FROM ctl_task WHERE task_id=?", (dep_task_id,)).fetchone()
    return bool(row and row["state"] == "completed")


def next_step(conn, task_id: str) -> dict | None:
    """First pending step whose depends_on are all done."""
    rows = conn.execute("SELECT * FROM ctl_step WHERE task_id=? ORDER BY rowid",
                        (task_id,)).fetchall()
    done = {r["step_id"] for r in rows if r["status"] == "done"}
    for r in rows:
        if r["status"] != "pending":
            continue
        import json
        if all(d in done for d in json.loads(r["depends_on"] or "[]")):
            return dict(r)
    return None
