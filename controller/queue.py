"""controller.queue — task selection (spec §17–19) for one scope.

Selection is deterministic code: state='ready', deps satisfied, not_before reached,
priority order. The LLM never picks the next task (a proposal can reorder via a
user-approved contract edit only).
"""
from __future__ import annotations

import time

from . import model


def eligible(conn, scope: str, now: float | None = None) -> list[dict]:
    now = now or time.time()
    rows = conn.execute(
        "SELECT t.*, c.fields AS cfields FROM ctl_task t"
        " JOIN ctl_contract_revision c ON c.task_id=t.task_id AND c.version=t.contract_version"
        " WHERE t.workdir=? AND t.state IN ('ready','running') ORDER BY"
        " CASE t.state WHEN 'running' THEN 0 ELSE 1 END, t.updated_at",
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
