"""Agent-facing tool surface of the controller (spec: docs/controller.md).

The passive layer (hook gates + skill) stops agents from doing the wrong thing;
these tools let a healthy agent DO controller work itself: see the queue,
propose tasks (which park in draft until a human "иди" agrees the contract),
close its own steps with read-back, and report a block through the authoritative
transition path instead of raw SQL.

All handlers go through controller.model / controller.transitions — the same
functions the pump and the operator commands use. No handler writes ctl_* SQL
directly except read-back SELECTs. Registration is gated by controller.enabled
(check_fn), so the tools are invisible to non-enabled profiles.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("controller.tools")

_lock = threading.RLock()


def _out(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _err(msg: str) -> str:
    return _out(ok=False, error=msg)


def _conn():
    from controller import model
    return model.connect()


def _task_row(conn, task_id: str) -> dict | None:
    row = conn.execute(
        "SELECT task_id, owner, project_id, workdir, goal, state, contract_version,"
        " updated_at FROM ctl_task WHERE task_id=?", (task_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------- handlers ----

def ctl_status(args: dict, **_: Any) -> str:
    """Queue snapshot: tasks (+steps when a task_id is given) and the epoch."""
    task_id = str(args.get("task_id") or "").strip()
    scope = str(args.get("workdir") or "").strip()
    with _lock, _conn() as conn:
        from controller import model
        if task_id:
            task = _task_row(conn, task_id)
            if task is None:
                return _err(f"unknown task_id {task_id!r}")
            steps = conn.execute(
                "SELECT step_id, kind, title, status FROM ctl_step"
                " WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
            events = conn.execute(
                "SELECT ts, kind, origin, payload FROM ctl_event WHERE task_id=?"
                " ORDER BY ts DESC LIMIT 8", (task_id,)).fetchall()
            return _out(ok=True,
                        permission=model.get_permission(conn, task["workdir"]),
                        task=task,
                        contract=model.current_contract(conn, task_id),
                        steps=[dict(s) for s in steps],
                        recent_events=[dict(e) for e in events])
        perms = [dict(r) for r in conn.execute(
            "SELECT scope, enabled, execution_epoch FROM ctl_autonomy_permission")]
        sql = ("SELECT task_id, owner, workdir, goal, state, contract_version,"
               " updated_at FROM ctl_task")
        params: tuple = ()
        if scope:
            sql += " WHERE workdir=?"
            params = (str(Path(scope).resolve()),)
        sql += " ORDER BY updated_at DESC LIMIT 50"
        rows = conn.execute(sql, params).fetchall()
        return _out(ok=True, permissions=perms, tasks=[dict(r) for r in rows])


def ctl_enqueue(args: dict, **_: Any) -> str:
    """Propose a task. Lands as draft with an unagreed MATERIAL contract: the
    pump will NOT dispatch it until a human "иди" agrees — agent enqueue is a
    proposal, never a self-start."""
    goal = str(args.get("goal") or "").strip()
    workdir = str(args.get("workdir") or "").strip()
    if not goal or not workdir:
        return _err("'goal' and 'workdir' are required")
    if not Path(workdir).expanduser().exists():
        return _err(f"workdir does not exist: {workdir}")
    owner = str(args.get("owner") or "agent").strip()
    position = "head" if str(args.get("priority") or "").lower() == "head" else "tail"
    acceptance = args.get("acceptance") or []
    dependencies = args.get("dependencies") or []
    steps = args.get("steps") or []
    if not isinstance(acceptance, list) or not isinstance(steps, list):
        return _err("'acceptance' and 'steps' must be arrays")
    with _lock, _conn() as conn:
        from controller import model
        deps = [str(d) for d in dependencies]
        unknown = [d for d in deps
                   if conn.execute("SELECT 1 FROM ctl_task WHERE task_id=?",
                                   (d,)).fetchone() is None]
        if unknown:
            return _err(f"unknown dependency task_id(s): {unknown}")
        tid = model.create_task(conn, owner=owner, workdir=workdir, goal=goal,
                                position=position)
        if acceptance or deps:
            fields = model.current_contract(conn, tid)
            if acceptance:
                fields["acceptance"] = [str(a) for a in acceptance]
            if deps:
                fields["dependencies"] = deps
            model.revise_contract(conn, tid, fields, "MATERIAL")
        step_ids = []
        for s in steps:
            if not isinstance(s, dict) or not s.get("title"):
                return _err(f"each step needs a 'title' (got {s!r})")
            step_ids.append(model.add_step(conn, tid, str(s.get("kind") or "generic"),
                                           str(s["title"])))
        return _out(ok=True, task_id=tid, state="draft", steps=step_ids,
                    note="queued as draft; dispatch waits for human agreement ('иди')")


def ctl_step_done(args: dict, **_: Any) -> str:
    """Finish one of the caller's steps, then READ BACK the stored status —
    a step is done only when the row says so."""
    step_id = str(args.get("step_id") or "").strip()
    status = str(args.get("status") or "done").strip()
    if not step_id:
        return _err("'step_id' is required")
    if status not in ("done", "blocked"):
        return _err("status must be 'done' or 'blocked'")
    with _lock, _conn() as conn:
        from controller import model
        row = conn.execute("SELECT step_id, task_id, status FROM ctl_step WHERE step_id=?",
                           (step_id,)).fetchone()
        if row is None:
            return _err(f"unknown step_id {step_id!r}")
        model.finish_step(conn, step_id, status)
        after = conn.execute("SELECT status FROM ctl_step WHERE step_id=?",
                             (step_id,)).fetchone()
        actual = after["status"] if after else "?"
        if actual != status:
            return _out(ok=False, step_id=step_id, requested=status, stored=actual,
                        error="finish_step did not stick — read-back mismatch")
        return _out(ok=True, step_id=step_id, status=actual, task_id=row["task_id"])


def ctl_block(args: dict, **_: Any) -> str:
    """Report that a running task is stuck: transitions to 'blocked' through the
    authoritative guard path (audited in ctl_state_transition), with the reason
    in the event log. Use this instead of silently stopping."""
    task_id = str(args.get("task_id") or "").strip()
    reason = str(args.get("reason") or "").strip()
    if not task_id or not reason:
        return _err("'task_id' and 'reason' are required")
    with _lock, _conn() as conn:
        from controller import model, transitions
        task = _task_row(conn, task_id)
        if task is None:
            return _err(f"unknown task_id {task_id!r}")
        try:
            res = transitions.transition(conn, task_id, "blocked", basis=reason)
        except ValueError as exc:
            return _err(str(exc))
        model.emit(conn, kind="agent_block", origin="agent", task_id=task_id,
                   payload={"reason": reason})
        return _out(ok=True, **res)


# ------------------------------------------------------------------ schema ----

def _str(desc: str) -> dict:
    return {"type": "string", "description": desc}


_TOOLS: dict[str, tuple[Any, str, dict, list[str]]] = {
    "ctl_status": (ctl_status,
                   "Show the controller queue: all tasks with their state and contract "
                   "version, plus the autonomy epoch. Pass task_id for one task's steps, "
                   "contract fields and recent events. Read-only.",
                   {"task_id": _str("Optional: drill into one task"),
                    "workdir": _str("Optional: only tasks queued for this workdir scope")},
                   []),
    "ctl_enqueue": (ctl_enqueue,
                    "Propose a new controller task (goal + workdir + optional steps, "
                    "acceptance criteria, dependencies on completed task_ids). The task "
                    "lands as draft with an unagreed contract: it is NOT dispatched until "
                    "a human explicitly agrees it. Enqueueing is proposing, never starting.",
                    {"goal": _str("One-line objective of the task"),
                     "workdir": _str("Absolute queue scope (project directory)"),
                     "steps": {"type": "array",
                               "items": {"type": "object",
                                         "properties": {"kind": _str("step kind"),
                                                        "title": _str("step title")},
                                         "required": ["title"]},
                               "description": "Optional plan: ordered checklist steps"},
                     "acceptance": {"type": "array", "items": {"type": "string"},
                                    "description": "Verifiable acceptance criteria"},
                     "dependencies": {"type": "array", "items": {"type": "string"},
                                      "description": "task_ids that must complete first"},
                     "priority": {"type": "string", "enum": ["tail", "head"],
                                  "description": "Queue position among live tasks (default tail)"}},
                    ["goal", "workdir"]),
    "ctl_step_done": (ctl_step_done,
                      "Mark one of your controller steps done (or blocked) and VERIFY it "
                      "landed: the reply reports the status actually stored. Never assume "
                      "a step closed from the call alone.",
                    {"step_id": _str("Step id to finish"),
                    "status": {"type": "string", "enum": ["done", "blocked"],
                               "description": "Default: done"}},
                    ["step_id"]),
    "ctl_block": (ctl_block,
                  "Report a running task as blocked, with a reason, through the audited "
                  "transition path (illegal transitions are rejected). Use this when you "
                  "are stuck instead of ending the turn silently.",
                  {"task_id": _str("Task to block"),
                   "reason": _str("Why it cannot proceed")},
                  ["task_id", "reason"]),
}

EMOJI = "🎛️"


def tool_defs() -> list[tuple[str, dict, Any]]:
    """(name, schema, handler) triples for ctx.register_tool."""
    out = []
    for name, (handler, description, properties, required) in _TOOLS.items():
        parameters: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            parameters["required"] = required
        out.append((name, {"name": name, "description": description,
                           "parameters": parameters}, handler))
    return out
