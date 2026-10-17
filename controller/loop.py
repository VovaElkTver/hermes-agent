"""controller.loop — the autonomous cycle (spec §20), one worker, one scope.

Every iteration is a full deterministic pass:
  permission check (fresh epoch read) -> pick task -> pick step -> barrier admit
  -> run step via worker_fn -> on finish: artifact + checkpoint review -> advance.

worker_fn(run, task, step) -> {"status": "finished"|"failed"|"interrupted",
                               "artifacts": [..payloads..]}
review_fn(run, step, artifacts) -> {"verdict": "pass"|"revise"|"needs_human", "detail"}
The loop NEVER trusts worker 'done' without review (acceptance row §46) and stops on
pause/permission loss between iterations and at dispatch (race row §7/§20).
"""
from __future__ import annotations

import hashlib
import json

from . import barrier, model, queue as Q, transitions


def _artifact_hash(payloads: list) -> str:
    return hashlib.sha256(json.dumps(payloads, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _park_task_paused(conn, task_id: str, *, basis: str) -> None:
    try:
        transitions.transition(conn, task_id, "paused", basis=basis)
    except ValueError:
        pass  # already paused/closed under another writer — the step state is the truth


def _resolve_worker_outcome(conn, *, task: dict, step: dict, run_id: str, res: dict):
    """Classify a finished worker against the DB, right after the turn ended.

    Preemption truth is the step READ-BACK, never the worker's exit status: a turn cut
    short can still report "finished" (or die reporting "failed") while the scheduler
    already flipped the step to paused underneath it. Returns ``None`` to proceed to
    artifact/review on a clean finish, else ``{"entry": <log line>, "stop": bool}``.
    """
    sid, tid = step["step_id"], task["task_id"]
    cur = model.get_step(conn, sid)
    if cur is not None and cur["status"] == "paused":
        model.set_run_status(conn, run_id, "interrupted")
        _park_task_paused(conn, tid, basis="out-of-band preempt")
        return {"entry": {"task": tid, "run": run_id, "step": sid,
                          "to": "paused", "preempted": True}, "stop": True}
    status = res.get("status", "failed")
    model.set_run_status(conn, run_id, "finished" if status == "finished" else status)
    if status == "interrupted":
        # Worker cut short with no external pause recorded: park the step paused with
        # the worker's own handoff so a resume re-anchors instead of restarting blind.
        model.pause_step(conn, sid, task_id=tid,
                         note=str(res.get("handoff") or res.get("error")
                                  or "worker reported interrupted"))
        _park_task_paused(conn, tid, basis="worker interrupted (resume pending)")
        return {"entry": {"task": tid, "run": run_id, "step": sid,
                          "to": "paused", "interrupted": True}, "stop": True}
    if status != "finished":
        model.unstick_step(conn, sid)  # release the claim: a ready->running retry must find it dispatchable
        transitions.transition(conn, tid, "failed", basis=f"run {run_id} {status}")
        return {"entry": {"task": tid, "run": run_id, "to": "failed"}, "stop": False}
    return None


def run_cycle(conn, *, scope: str, worker_fn, review_fn, max_steps: int = 50) -> list[dict]:
    log = []
    for _ in range(max_steps):
        perm = model.get_permission(conn, scope)
        if not perm["enabled"]:
            log.append({"stop": "permission off"})
            return log
        candidates = Q.eligible(conn, scope)
        if not candidates:
            log.append({"stop": "queue empty"})
            return log
        # one run at a time per task: a task whose run is still open (an external
        # worker is mid-step, or a crashed run not yet reconciled) is never given a
        # second concurrent run — step over it and take the next candidate. The open
        # run is left untouched (no mid-run kill).
        candidates = [t for t in candidates if not Q.has_open_run(conn, t["task_id"])]
        if not candidates:
            log.append({"stop": "all candidates have an open run"})
            return log
        task = candidates[0]
        if task["state"] != "running":
            # entering running is itself guarded (contract/permission/budgets)
            try:
                transitions.transition(conn, task["task_id"], "running",
                                       basis="autonomous loop", permission=perm)
            except ValueError as e:
                log.append({"skip": task["task_id"], "reason": str(e)})
                return log
        step = Q.next_step(conn, task["task_id"])
        if step is None:
            total = conn.execute("SELECT COUNT(*) c FROM ctl_step WHERE task_id=?",
                                 (task["task_id"],)).fetchone()["c"]
            if total == 0:
                transitions.transition(conn, task["task_id"], "blocked",
                                       basis="empty plan: add steps before running")
                log.append({"task": task["task_id"], "to": "blocked",
                            "reason": "empty plan"})
                continue
            # A preempted (paused) step is NOT "all done": the task parks until the step is
            # resumed, instead of marching into verifying with unfinished work.
            if conn.execute("SELECT 1 FROM ctl_step WHERE task_id=? AND status='paused' LIMIT 1",
                            (task["task_id"],)).fetchone() is not None:
                transitions.transition(conn, task["task_id"], "paused",
                                       basis="preempted step awaiting resume")
                log.append({"task": task["task_id"], "to": "paused",
                            "reason": "preempted step"})
                return log
            transitions.transition(conn, task["task_id"], "verifying",
                                   basis="all steps done")
            outcome = finish_verifying(conn, task["task_id"])
            log.append({"task": task["task_id"], "to": outcome})
            continue
        # dispatch-time barrier: pause arriving after selection is caught here (§7 race)
        ok, why = barrier.check(conn, scope=scope, origin="autonomous",
                                task_id=task["task_id"])
        if not ok:
            transitions.transition(conn, task["task_id"], "paused",
                                   basis=f"loop barrier: {why}")
            log.append({"task": task["task_id"], "to": "paused", "reason": why})
            return log
        run_id = model.start_run(conn, task_id=task["task_id"], step_id=step["step_id"],
                                 contract_version=task["contract_version"],
                                 execution_epoch=perm["execution_epoch"],
                                 worker_id="loop")
        model.add_budget(conn, task["task_id"], "tool_calls", 0)  # ensure counter row
        model.set_run_status(conn, run_id, "running")
        # Claim the step BEFORE dispatch so an out-of-band preempt has something to
        # CAS against (in_progress -> paused) while the worker is mid-turn. A lost claim
        # (already in_progress/paused under another writer) is requeued and rescanned.
        claim = model.mark_step_in_progress(conn, step["step_id"])
        if not claim["ok"]:
            model.set_run_status(conn, run_id, "interrupted")
            log.append({"task": task["task_id"], "run": run_id,
                        "skip_step": step["step_id"], "stored": claim["stored"]})
            continue
        # Re-anchoring: a step that resumes after a preempt gets the stored handoff
        # ("чем занимался, на какой тулзе встал") — a cheap resume, no re-narration.
        handoff = model.paused_handoff_note(conn, step["step_id"])
        step = {**dict(step), "handoff": handoff} if handoff else dict(step)
        try:
            res = worker_fn({"run_id": run_id}, dict(task), step)
        except Exception as exc:
            res = {"status": "failed", "artifacts": [], "error": str(exc)}
        # budget consumed by children is accounted via post_tool_call hook; loop adds its step
        model.add_budget(conn, task["task_id"], "steps", 1)
        stop = _resolve_worker_outcome(conn, task=task, step=step, run_id=run_id, res=res)
        if stop is not None:
            log.append(stop["entry"])
            if stop["stop"]:
                return log
            continue
        ah = _artifact_hash(res.get("artifacts", []))
        model.add_artifact(conn, task["task_id"], run_id, "step_output",
                           {"hash": ah, "items": res.get("artifacts", [])})
        verdict = {"verdict": "pass", "detail": "not checkpoint"}
        if step.get("checkpoint", 1):
            verdict = review_fn({"run_id": run_id}, step, res.get("artifacts", []))
            model.add_review(conn, step["step_id"], run_id, ah,
                             verdict.get("verdict", "revise"), "controller-loop",
                             verdict.get("detail", ""))
        if verdict.get("verdict") == "pass":
            model.finish_step(conn, step["step_id"])
            log.append({"task": task["task_id"], "run": run_id,
                        "step": step["step_id"], "review": "pass"})
            continue
        # checkpoint failed -> step goes back to pending; task needs a fix iteration
        log.append({"task": task["task_id"], "run": run_id, "review": verdict})
        model.unstick_step(conn, step["step_id"])
        if verdict.get("verdict") == "needs_human":
            transitions.transition(conn, task["task_id"], "needs_human",
                                   basis=f"review: {verdict.get('detail')}")
            return log
        # 'revise': give the task one more attempt at the same step (attempt>1 next pick)
        if model.budget_exhausted(conn, task["task_id"], "steps"):
            transitions.transition(conn, task["task_id"], "blocked",
                                   basis="budget: steps exhausted on revise")
            return log
    log.append({"stop": "max_steps"})
    return log


def finish_verifying(conn, task_id: str) -> str:
    """verifying -> completed|ready|needs_human by the review-currency guard outcome."""
    try:
        transitions.transition(conn, task_id, "completed", basis="all reviews current")
        return "completed"
    except ValueError as e:
        msg = str(e)
        if "review" in msg:
            transitions.transition(conn, task_id, "needs_human", basis=f"verify: {msg}")
            return "needs_human"
        raise
