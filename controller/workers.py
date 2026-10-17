"""controller.workers — real worker/review bindings (increment 3).

worker_fn: a headless hermes one-shot subprocess inside the task's workdir.
  Why not delegate_task children: dispatch needs a live parent agent turn; the
  pump runs between/without turns. The worker session itself is a full session,
  so fork-guardrails apply to it, and any delegation it does is supervisor-gated.
  The task/step brief is written to a query file (nothing shell-interpreted);
  HERMES_CONTROLLER_TASK_ID marks the process so budgets can be attributed.

review_fn: supervisor judge, but FAIL CLOSED (judge.review fails open for the
  checkpoint path; for task completion an unreachable judge must block, not pass).
  If the contract carries a validation command, it must have passed in the worker
  transcript file first — code, not vibes (spec §29-30).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time

logger = logging.getLogger(__name__)

# Windows-only spawn flags (subprocess exposes them on nt only).
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008


def _spawn_kwargs() -> dict:
    """Detached-spawn kwargs per OS. On POSIX start_new_session gives the worker
    its own session so killpg can tree-kill it. On Windows start_new_session is
    silently IGNORED by CPython, so the equivalent isolation is
    CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS via creationflags."""
    if os.name == "nt":
        return {"creationflags": CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS}
    return {"start_new_session": True}


def _kill_tree(proc) -> None:
    """Kill the timed-out worker and every grandchild still writing into the
    workdir. POSIX: the spawn put it in its own session -> killpg. Windows: no
    killpg/getpgid exist at all (AttributeError would escape the except) ->
    taskkill /T /F walks the process tree."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=15, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):
        pass


def _preempt_signal(conn, *, run_id: str, step_id: str | None) -> bool:
    """True when an out-of-band preempt landed on this run/step since dispatch:
    the scheduler either flagged the run row ``interrupt_requested`` (the durable
    fallback path — used when the gateway control socket has no session verbs, old
    gateway, or the worker is a subprocess with no socket at all), or flipped the
    claimed step ``in_progress -> paused`` via the CAS pause (PR-A model). Either is
    a command to cut the worker short WITHOUT finishing the turn. DB hiccups fail
    open (keep the worker running) — a missed poll is retried next interval."""
    try:
        row = conn.execute(
            "SELECT (SELECT run_status FROM ctl_run WHERE run_id=?) rs,"
            " (SELECT status FROM ctl_step WHERE step_id=?) ss",
            (run_id, step_id or "")).fetchone()
    except sqlite3.Error:
        logger.debug("preempt signal read failed (fail-open, retried next poll)",
                     exc_info=True)
        return False
    return bool(row and (row["rs"] == "interrupt_requested"
                         or (step_id and row["ss"] == "paused")))


def _await_worker(proc, *, conn, run_id: str, step_id: str | None,
                  timeout_s: int, poll_s: float, grace_s: float = 60.0):
    """Bounded wait for the worker with out-of-band preempt polling.

    Returns ``(stdout, stderr, preempted)``. Repeated ``communicate()`` calls are
    safe since bpo-39531: partial pipe data is kept in the Popen buffers between
    calls, so the final call still returns everything the worker printed. Raises
    ``subprocess.TimeoutExpired`` when the hard budget (timeout_s + grace) is
    spent with no preempt — the caller keeps its legacy tree-kill/failed path."""
    deadline = time.monotonic() + timeout_s + grace_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(getattr(proc, "args", ""),
                                            timeout_s + grace_s)
        try:
            out, err = proc.communicate(timeout=min(poll_s, remaining))
            return out, err, False
        except subprocess.TimeoutExpired:
            if _preempt_signal(conn, run_id=run_id, step_id=step_id):
                _kill_tree(proc)
                # After the kill every pipe reaches EOF; this collects the buffered
                # partial output too (see bpo-39531 note above).
                out, err = proc.communicate()
                return out, err, True


def make_worker(conn, *, timeout_s: int = 1800,
                hermes_bin: str | None = None, extra_env: dict | None = None,
                preempt_poll_s: float = 2.0, grace_s: float = 60.0):
    # Resolve the OS branch once at binding time: the host OS cannot change
    # mid-run, and the tests that fake os.name='nt' must not have it leak into
    # stdlib internals (tempfile checks os.name too).
    spawn_kwargs = _spawn_kwargs()
    def worker_fn(run, task, step):
        brief = _brief(conn, task, step)
        qf = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                         encoding="utf-8")
        try:
            qf.write(brief)
            qf.close()
            exe = hermes_bin or shutil.which("hermes") or "hermes"
            env = {**os.environ, "HERMES_CONTROLLER_TASK_ID": task["task_id"],
                   "HERMES_CONTROLLER_RUN_ID": run["run_id"]}
            if extra_env:
                env.update(extra_env)
            # start_new_session: the worker's own grandchildren (terminal/browser tools)
            # must die WITH it — SIGKILL of the direct child alone left orphans writing
            # into the workdir after the pump marked the step failed (review finding 4).
            proc = subprocess.Popen(
                [exe, "chat", "--query-file", qf.name, "--oneshot", "-Q",
                 "--in", task["workdir"],
                 "--run-budget", str(timeout_s)],
                cwd=task["workdir"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=env,
                **spawn_kwargs)
            try:
                # Bounded wait with preempt polling: communicate() may be re-entered on
                # timeout (CPython keeps partial pipe data in internal buffers and the
                # final call returns everything collected). Each poll costs one indexed
                # read of the run/step rows; the worker is cut short the moment an
                # out-of-band preempt lands, WITHOUT letting the turn finish (acceptance:
                # the pump removes the in_progress step mid-flight).
                out, err, preempted = _await_worker(
                    proc, conn=conn, run_id=run["run_id"],
                    step_id=step.get("step_id"), timeout_s=timeout_s,
                    poll_s=preempt_poll_s, grace_s=grace_s)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                # direct child + every tool grandchild share this detached group:
                # tree-kill guarantees nothing keeps writing into the workdir after
                # failure (killpg on POSIX, taskkill /T /F on Windows)
                _kill_tree(proc)
                proc.communicate()
                return {"status": "failed", "artifacts": [], "error": "worker timeout"}
            if preempted:
                from controller import model
                handoff = ""
                if step.get("step_id"):
                    handoff = model.paused_handoff_note(conn, step["step_id"])
                return {"status": "interrupted",
                        "artifacts": [{"stdout_tail": (out or "")[-2000:],
                                       "returncode": returncode}],
                        "handoff": handoff or "preempted out-of-band (no note stored)",
                        "detail": (err or "")[-500:]}
            artifacts = [{"stdout_tail": (out or "")[-2000:],
                          "returncode": returncode}]
            status = "finished" if returncode == 0 else "failed"
            return {"status": status, "artifacts": artifacts,
                    "detail": (err or "")[-500:]}
        finally:
            try:
                os.unlink(qf.name)
            except OSError:
                pass
    return worker_fn


def _brief(conn, task, step) -> str:
    from controller import model
    c = model.current_contract(conn, task["task_id"])
    lines = [
        f"# Controlled step ({step['kind']}): {step['title']}",
        f"Task: {task['goal']}",
        "",
        "Acceptance criteria:", *[f"- {a}" for a in c.get("acceptance", []) or ["(none listed)"]],
        "Confirmed facts:", *[f"- {a}" for a in (c.get("facts") or [])[:20]],
        "Restrictions:", *[f"- {a}" for a in (c.get("restrictions") or [])[:20]],
        "",
        "Work ONLY on this step. When done, state exactly how each acceptance",
        "criterion is verified (command output). This run is checkpointed.",
    ]
    # Cheap resume (acceptance #4): a step that was preempted re-dispatches with the
    # handoff line the preempter stored ("чем занимался, на какой тулзе встал").
    # The contract fields above are re-read NOW, so a contract edit that landed during
    # the pause re-anchors the resumed run instead of replaying stale context.
    note = str(step.get("handoff") or "").strip()
    if note:
        lines += [
            "",
            "## RESUME — this step was preempted mid-turn before",
            f"Handoff from the interrupted run: {note}",
            "Do NOT redo finished work: continue from the handoff point, and",
            "re-anchor first on the CURRENT acceptance criteria/facts above.",
        ]
    return "\n".join(lines)


def make_review(conn, *, judge_cfg: dict | None = None):
    from controller import model
    def review_fn(run, step, artifacts):
        tail = ""
        if artifacts and isinstance(artifacts[0], dict):
            tail = artifacts[0].get("stdout_tail", "")
        if judge_cfg is None:
            return {"verdict": "needs_human", "detail": "judge not configured"}
        try:
            from plugins.supervisor.judge import review as _j
            hist = [{"tool": "worker", "status": str(artifacts[0].get("returncode")),
                     "args": step.get("title", ""), "result": tail[:8000]}] if artifacts else []
            v = _j(judge_cfg, step.get("title", "step"), hist)
            verdict = v.get("verdict", "revise")
            # judge.review is fail-OPEN by design (advisory checkpoint); completion needs
            # fail-closed. degraded is judge.py's machine flag (HTTP down, unparsable/empty
            # completion, missing verdict field, off-whitelist verdict coerced to pass);
            # the wording check covers judges older than the degraded flag; a pass with no
            # reason sentence violates JUDGE_SYSTEM's contract (a real pass always explains).
            reason = v.get("reason") or ""
            if verdict == "pass" and (v.get("degraded") or "judge unavailable" in reason
                                      or not reason.strip()):
                return {"verdict": "needs_human",
                        "detail": reason or "judge pass without reason (degraded)"}
            # code-side gate: worker must have exited clean for a pass
            if verdict == "pass" and artifacts and artifacts[0].get("returncode") not in (0, None):
                return {"verdict": "revise", "detail": "judge pass but worker exit != 0"}
            return {"verdict": verdict, "detail": v.get("reason", "")}
        except Exception as exc:  # FAIL CLOSED unlike checkpoint judge
            return {"verdict": "needs_human", "detail": f"judge unavailable: {exc}"}
    return review_fn


def run_validation(conn, task_id: str, workdir: str) -> tuple[bool, str]:
    """Optional contract-level gate: acceptance.validation_cmd must pass (code, not prose)."""
    from controller import model
    c = model.current_contract(conn, task_id)
    cmd = (c.get("acceptance") or {}).get("validation_cmd") if isinstance(c.get("acceptance"), dict) else None
    if not cmd:
        return True, "no validation_cmd in contract"
    p = subprocess.run(cmd, shell=True, cwd=workdir, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600, check=False)
    return p.returncode == 0, (p.stdout + p.stderr)[-1000:]
