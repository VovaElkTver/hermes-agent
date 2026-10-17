"""controller.preempt — out-of-band preempt request for one running task (PR-B).

The scheduler decides that the currently-running step must stop NOW (a higher-
priority task arrived, an operator redirected, a lease was lost). Two delivery
paths, in preference order:

1. ``control socket``  — a gateway-hosted session's live turn is cut via the
   ``interrupt-session`` verb; the preempt is attributed to the SYSTEM producer
   (``tool_reason`` slug, default ``controller_preemption``), never a user stop
   (#112647 attribution discipline), and the verb returns the activity handoff.
2. ``db flag``         — the durable fallback: the run row is flagged
   ``interrupt_requested`` and the pump's worker poll (controller.workers.
   _preempt_signal) tree-kills the subprocess WITHOUT letting the turn finish.
   Used when no session/home is given, when no gateway answers, and when the
   gateway predates the verbs (client returns None).

Regardless of path, the in_progress step is UNSTAGED synchronously here —
model.pause_step, a CAS under BEGIN IMMEDIATE with read-back — so the queue sees
the step as 'paused' the moment the call returns, with the handoff note stored
for a cheap re-anchored resume (workers._brief injects it on re-dispatch). The
worker's own exit status never overwrites this truth (loop trusts the read-back).
"""
from __future__ import annotations

import logging

from . import model

logger = logging.getLogger(__name__)

DEFAULT_PREEMPT_REASON = "controller_preemption"

OPEN_RUN_STATUSES = ("starting", "running")


def request_preempt(conn, task_id: str, *, reason: str = DEFAULT_PREEMPT_REASON,
                    handoff: str = "", session: str | None = None,
                    home=None) -> dict:
    """Preempt the task's running step out-of-band. Returns an audit dict; never
    raises for a benign miss (idle task, lost CAS) — callers act on ``ok``/``via``."""
    task = model.get_task(conn, task_id)
    if task is None:
        return {"ok": False, "task_id": task_id, "error": f"unknown task {task_id!r}"}
    reason = str(reason or DEFAULT_PREEMPT_REASON).strip() or DEFAULT_PREEMPT_REASON
    note = str(handoff or "").strip() or f"[preempted: {reason}] activity not captured"

    # 1) live path: gateway session verbs (None answer = old gateway / no socket).
    gateway_result = None
    via = "db_flag"
    if session and home is not None:
        try:
            from gateway.control_socket import interrupt_gateway_session
            gateway_result = interrupt_gateway_session(
                home, session, reason=reason,
                message=note if handoff else None)
        except Exception:  # deliberate boundary — any client/import failure means
            # "no live path"; the durable flag below still delivers the preempt.
            logger.debug("interrupt-session verb unavailable; falling back to "
                         "durable flag", exc_info=True)
            gateway_result = None
        if gateway_result is not None:
            via = "control_socket"

    # 2) durable flag: the pump-side worker poll acts on this within one poll
    #    interval even if the live path never lands (subprocess workers have no
    #    socket at all).
    run = conn.execute(
        "SELECT run_id FROM ctl_run WHERE task_id=? AND run_status IN (?,?)"
        " ORDER BY started_at DESC LIMIT 1",
        (task_id, *OPEN_RUN_STATUSES)).fetchone()
    if run is not None:
        model.set_run_status(conn, run["run_id"], "interrupt_requested")

    # 3) unstage synchronously: every in_progress step -> paused + handoff (CAS).
    steps = conn.execute(
        "SELECT step_id FROM ctl_step WHERE task_id=? AND status='in_progress'",
        (task_id,)).fetchall()
    results = [model.pause_step(conn, r["step_id"], task_id=task_id, note=note)
               for r in steps]
    paused = [r for r in results if r.get("ok")]

    model.emit(conn, kind="preempt_requested", origin="controller", task_id=task_id,
               payload={"reason": reason, "via": via, "handoff": note,
                        "session": session or "",
                        "steps_paused": [r["step_id"] for r in paused],
                        "gateway": gateway_result})
    return {"ok": bool(paused) or via == "control_socket",
            "task_id": task_id, "reason": reason, "via": via,
            "run_id": run["run_id"] if run else None,
            "steps": results, "gateway": gateway_result}
