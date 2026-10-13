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
import os
import shutil
import signal
import subprocess
import tempfile
import time

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


def make_worker(conn, *, timeout_s: int = 1800,
                hermes_bin: str | None = None, extra_env: dict | None = None):
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
                out, err = proc.communicate(timeout=timeout_s + 60)
                returncode = proc.returncode
            except subprocess.TimeoutExpired:
                # direct child + every tool grandchild share this detached group:
                # tree-kill guarantees nothing keeps writing into the workdir after
                # failure (killpg on POSIX, taskkill /T /F on Windows)
                _kill_tree(proc)
                proc.communicate()
                return {"status": "failed", "artifacts": [], "error": "worker timeout"}
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
