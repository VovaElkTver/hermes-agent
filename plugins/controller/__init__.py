"""controller plugin hooks — the hook surface of the managed-autonomy controller.

Off unless config `controller.enabled: true` (plugins.entries.controller.settings.enabled
or top-level `controller.enabled`). Feature-flag discipline per the spec's task:
implementation lands under a disabled-by-default flag.

Wiring:
  pre_tool_call    -> dispatch barrier for autonomous-origin actions (epoch re-read at
                      dispatch time closes the pause race; supervisor tags children as
                      autonomous by session registry).
  post_tool_call   -> budget accounting per task_id (survives restart, not per-worker).
  register()       -> restart recovery (Q5): operator /restart marker -> non-autonomous;
                      no marker (crash) -> continue with epoch bump; unfinished runs to
                      'unknown' awaiting verify (no blind retry).
"""
from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger("controller")

_ctx = None
_lock_obj = threading.RLock()
_conn = None
_scope_by_session: dict[str, str] = {}


def _enabled() -> bool:
    try:
        if _ctx is not None and _ctx.get_config("enabled", False):
            return True
    except Exception:
        pass
    # top-level config.yaml: controller.enabled (same source the core router uses)
    try:
        from gateway.run_inbound import _ctl_flag_on
        return _ctl_flag_on()
    except Exception:
        return False


def _db():
    global _conn
    with _lock_obj:
        if _conn is None:
            from controller import model
            _conn = model.connect()
        return _conn


# ---- dispatch barrier --------------------------------------------------------

def _origin_of(task_id: str, session_id: str) -> str:
    """Children spawned through delegate_task are the autonomous origin; operator
    sessions are interactive. supervisor's registry is the tag source (Q4 decision)."""
    try:
        from tools.delegate_tool_registry import _active_subagents
        with _lock_obj:
            return "autonomous" if task_id in _active_subagents else "user"
    except Exception:
        return "user"


def _on_pre_tool_call(tool_name: str = "", args: dict | None = None, task_id: str = "",
                      session_id: str = "", **kw):
    if not _enabled():
        return None
    try:
        from controller import barrier
        origin = _origin_of(task_id, session_id)
        if origin != "autonomous":
            return None
        scope = _scope_by_session.get(session_id) or _default_scope()
        task_id_ctl = kw.get("controller_task_id") or _task_for_scope(_db(), scope)
        ok, why = barrier.check(_db(), scope=scope, origin=origin, task_id=task_id_ctl)
        if not ok:
            return {"action": "block",
                    "message": f"[controller] autonomous dispatch blocked: {why}"}
    except Exception as exc:
        logger.warning("controller barrier error (fail-closed): %s", exc)
        return {"action": "block", "message": f"[controller] barrier error: {exc}"}
    return None


def _task_for_scope(conn, scope: str):
    row = conn.execute("SELECT task_id FROM ctl_task WHERE workdir=? AND state='running'"
                       " ORDER BY updated_at DESC LIMIT 1", (scope,)).fetchone()
    return row["task_id"] if row else None


# ---- budget accounting -------------------------------------------------------

def _on_post_tool_call(tool_name: str = "", args: dict | None = None, task_id: str = "",
                       session_id: str = "", **kw):
    if not _enabled() or kw.get("status") not in {"ok", "error"}:
        return
    try:
        from controller import model
        tid = kw.get("controller_task_id")
        if not tid:
            return
        model.add_budget(_db(), tid, "tool_calls", 1)
    except Exception as exc:
        logger.debug("controller budget accounting failed: %s", exc)
    return



def pump(scope: str, *, worker_fn=None, review_fn=None, max_steps: int = 20) -> list[dict]:
    """Drive the autonomous cycle for one scope (queue + loop). The engine is pure and
    tested; worker_fn is host-provided (headless executor or delegate dispatch) and
    review_fn defaults to the supervisor checkpoint. Safe to call from cron or a
    completion watcher: permission is re-checked at dispatch, so a paused queue no-ops."""
    from controller import loop as _loop
    if worker_fn is None:
        def worker_fn(run, task, step):
            return {"status": "failed", "artifacts": [],
                    "error": "no worker bound (increment-2 binding PR pending)"}
    if review_fn is None:
        review_fn = lambda run, step, arts: {"verdict": "needs_human",
                                             "detail": "no reviewer bound"}
    return _loop.run_cycle(_db(), scope=scope, worker_fn=worker_fn,
                           review_fn=review_fn, max_steps=max_steps)



import subprocess as _sp
import time as _time
_last_pump = 0.0


def _pump_spawn_kwargs() -> dict:
    """Same OS branch as the worker spawn (controller.workers._spawn_kwargs).
    Resolved once at plugin-import time so a missing/failing import can't ride
    inside _maybe_pump's broad except and silently kill the pump spawn."""
    try:
        from controller.workers import _spawn_kwargs
        return _spawn_kwargs()
    except Exception:
        import os as _os
        if _os.name == "nt":
            return {"creationflags": 0x00000200 | 0x00000008}  # NEW_PROCESS_GROUP|DETACHED
        return {"start_new_session": True}


_PUMP_SPAWN_KWARGS = _pump_spawn_kwargs()


def _maybe_pump(scope: str) -> None:
    """Fire-and-forget detached pump after a turn (spec §20 loop runner). Cooldown
    prevents spawn thrash; the pump process itself no-ops when permission is off or
    the queue is empty, so worker-session endings re-pumping is self-limiting."""
    global _last_pump
    with _lock_obj:
        if _time.time() - _last_pump < 30:
            return
        _last_pump = _time.time()
    try:
        import sys
        _sp.Popen([sys.executable, "-m", "controller.pump_cli", "--scope", scope],
                  cwd=str(Path(__file__).resolve().parents[2]),
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  **_PUMP_SPAWN_KWARGS)
    except Exception as exc:
        logger.debug("controller pump spawn failed: %s", exc)


def _on_post_llm_call(session_id: str = "", **kw):
    if not _enabled():
        return
    try:
        scope = _scope_by_session.get(session_id) or _default_scope()
        _maybe_pump(scope)
    except Exception as exc:
        logger.debug("controller post-llm pump check failed: %s", exc)
    return


def register(ctx):
    global _ctx
    _ctx = ctx
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    try:
        from . import agent_tools
        for _name, _schema, _handler in agent_tools.tool_defs():
            ctx.register_tool(name=_name, toolset="controller", schema=_schema,
                              handler=_handler, emoji=agent_tools.EMOJI,
                              check_fn=_enabled)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("controller: agent tools registration failed: %s", exc,
                       exc_info=True)
    if _enabled():
        logger.info("controller: managed autonomy enabled (flag on)")
        try:
            from controller import commands as _cmds, reconcile as _rec
            mode = _cmds.on_restart(_db(), scope="default")
            rep = _rec.recover(_db(), verify_fn=lambda r: "not_started")
            logger.info("controller recovery: mode=%s unresolved_runs=%s",
                        mode["mode"], [x["run_id"] for x in rep])
        except Exception as exc:
            logger.warning("controller recovery failed: %s", exc)
