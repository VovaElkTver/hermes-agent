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
        try:
            res = worker_fn({"run_id": run_id}, dict(task), step)
        except Exception as exc:
            res = {"status": "failed", "artifacts": [], "error": str(exc)}
        # budget consumed by children is accounted via post_tool_call hook; loop adds its step
        model.add_budget(conn, task["task_id"], "steps", 1)
        status = res.get("status", "failed")
        model.set_run_status(conn, run_id, "finished" if status == "finished" else status)
        if status != "finished":
            transitions.transition(conn, task["task_id"], "failed",
                                   basis=f"run {run_id} {status}")
            log.append({"task": task["task_id"], "run": run_id, "to": "failed"})
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
        # checkpoint failed -> step stays pending; task needs a fix iteration
        log.append({"task": task["task_id"], "run": run_id, "review": verdict})
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
