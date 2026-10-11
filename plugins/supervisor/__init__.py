"""Hermes supervisor plugin — step-by-step verification of delegated subagents.

Wiring (all in-process, same dispatcher as built-in hooks):

  subagent_start         -> register child: child_session_id -> ChildState(subagent_id, goal, role, parent)
  pre_tool_call          -> (children only) halted? -> block
                            pending LLM-judge verdict? wait <= gate_wait_seconds; revise -> block w/ instruction
                            verify-after-write gate -> block
                            rule triggers (when: pre_tool_call) -> block / steer / interrupt / escalate
  post_tool_call         -> update counters + bounded history; record unverified writes
                            rule triggers (when: post_tool_call) -> annotate / steer / interrupt / escalate
                            every N tools (or on listed tools) -> submit LLM judge review (async)
  transform_tool_result  -> append queued [supervisor] annotations to the result the child is about to read
  subagent_stop          -> final audit record, drop state

Delivery channels and their timing:
  block      synchronous; the message becomes the tool result the child reads immediately.
  annotate   synchronous; appended to the CURRENT tool result (same iteration).
  steer      steer_subagent(): queued, lands at the child's next iteration boundary
             (never cuts the in-flight tool; may surface as missed_steer if the child already finished).
  interrupt  interrupt_subagent(): cooperative stop at next boundary; partial result returns to the parent.
  escalate   steer the PARENT orchestrator (if the parent is itself a subagent) with a report.

Settings live in ~/.hermes/config.yaml under plugins.entries.supervisor.settings (read via
ctx.get_config). Requires plugins.isolation: in_process because it calls the delegate registry directly.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any, Optional

from . import judge as _judge
from .policy import (ChildState, Decision, clear_gate_on_verified, evaluate, record_mutation,
                     validate_triggers, verify_gate)

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_children: dict[str, ChildState] = {}  # child session_id -> state
_annotations: dict[tuple[str, str], list[str]] = {}  # (session_id, tool_call_id) -> notes
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="supervisor-judge")
_ctx = None


# ── settings ─────────────────────────────────────────────────────────────────────────────────

def _cfg(key: str, default: Any = None) -> Any:
    try:
        value = _ctx.get_config(key, default) if _ctx is not None else default
    except Exception:
        value = default
    return default if value is None else value


def _enabled() -> bool:
    return bool(_cfg("enabled", True)) and os.environ.get("HERMES_SUPERVISOR_DISABLE", "") not in ("1", "true")


def _audit(kind: str, **fields: Any) -> None:
    path = Path(os.path.expanduser(_cfg("log_path", "~/.hermes/supervisor/audit.jsonl")))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "kind": kind, **fields}
        with _lock, path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        logger.debug("supervisor audit write failed", exc_info=True)


# ── child lookup ─────────────────────────────────────────────────────────────────────────────

def _lookup(session_id: Optional[str]) -> Optional[ChildState]:
    """Map a hook's session_id to a supervised child. Falls back to the live delegate registry
    (covers compression-rotated session ids and children spawned before the plugin loaded)."""
    if not session_id:
        return None
    with _lock:
        st = _children.get(session_id)
    if st is not None:
        return st
    try:
        from tools.delegate_tool_registry import _active_subagents, _active_subagents_lock
        with _active_subagents_lock:
            records = list(_active_subagents.values())
        for r in records:
            agent = r.get("agent")
            if agent is not None and getattr(agent, "session_id", None) == session_id:
                st = ChildState(subagent_id=r.get("subagent_id", ""), session_id=session_id,
                                parent_subagent_id=r.get("parent_id") or "", goal=r.get("goal") or "")
                with _lock:
                    _children[session_id] = st
                return st
    except Exception:
        logger.debug("supervisor registry fallback failed", exc_info=True)
    return None


# ── actions ──────────────────────────────────────────────────────────────────────────────────

def _steer(subagent_id: str, text: str) -> bool:
    try:
        from tools.delegate_tool_registry import steer_subagent
        return bool(steer_subagent(subagent_id, text))  # unscoped in-process contract
    except Exception:
        logger.debug("steer_subagent failed", exc_info=True)
        return False


def _interrupt(subagent_id: str) -> bool:
    try:
        from tools.delegate_tool_registry import interrupt_subagent
        return bool(interrupt_subagent(subagent_id))
    except Exception:
        logger.debug("interrupt_subagent failed", exc_info=True)
        return False


def _apply(st: ChildState, d: Decision, tool_name: str, tool_call_id: str) -> Optional[dict]:
    """Execute a decision. Returns a pre_tool_call directive dict for 'block', else None."""
    _audit("trigger", subagent_id=st.subagent_id, session_id=st.session_id, trigger=d.trigger,
           action=d.action, tool=tool_name, message=d.message)
    text = d.message if d.message.startswith("[supervisor]") else f"[supervisor] {d.message}"
    if d.action == "block":
        return {"action": "block", "message": text}
    if d.action == "annotate":
        with _lock:
            _annotations.setdefault((st.session_id, tool_call_id), []).append(text)
        return None
    if d.action == "steer":
        ok = _steer(st.subagent_id, text)
        _audit("steer", subagent_id=st.subagent_id, queued=ok)
        return None
    if d.action == "interrupt":
        st.halted = True
        ok = _interrupt(st.subagent_id)
        _audit("interrupt", subagent_id=st.subagent_id, found=ok)
        return {"action": "block", "message": text + " (stopping)"}
    if d.action == "escalate":
        report = (f"[supervisor escalation] worker {st.subagent_id} ({st.goal[:120]!r}) "
                  f"trigger={d.trigger} on {tool_name}: {d.message} — review it with "
                  "delegate_task(action='list'), then steer or stop it.")
        delivered = bool(st.parent_subagent_id) and _steer(st.parent_subagent_id, report)
        _audit("escalate", subagent_id=st.subagent_id, parent=st.parent_subagent_id, delivered=delivered)
        if not delivered:  # top-level parent is not a subagent: tell the worker to report it instead
            _steer(st.subagent_id, text + " Include this issue explicitly in your final summary.")
        return None
    return None


# ── hooks ────────────────────────────────────────────────────────────────────────────────────

def _on_subagent_start(child_session_id=None, child_subagent_id=None, parent_session_id=None,
                       parent_subagent_id=None, child_role=None, child_goal=None, **_: Any) -> None:
    if not child_session_id or not child_subagent_id:
        return
    st = ChildState(subagent_id=child_subagent_id, session_id=child_session_id,
                    parent_session_id=parent_session_id or "", parent_subagent_id=parent_subagent_id or "",
                    role=child_role or "leaf", goal=child_goal or "")
    with _lock:
        _children[child_session_id] = st
    _audit("start", subagent_id=child_subagent_id, session_id=child_session_id, role=st.role,
           parent_session_id=st.parent_session_id, parent_subagent_id=st.parent_subagent_id, goal=st.goal[:500])


def _on_subagent_stop(child_session_id=None, child_status=None, child_summary=None,
                      duration_ms=None, tool_call_history=None, **_: Any) -> None:
    with _lock:
        st = _children.pop(child_session_id or "", None)
        for key in [k for k in _annotations if k[0] == child_session_id]:
            _annotations.pop(key, None)
    if st is None:
        return
    _audit("stop", subagent_id=st.subagent_id, status=child_status, duration_ms=duration_ms,
           tool_count=st.tool_count, unverified_writes=st.unverified_writes, fires=st.fires,
           summary=(child_summary or "")[:2000])
    if st.unverified_writes and _cfg("gate.warn_on_unverified_exit", True):
        logger.warning("supervisor: %s finished with unverified writes: %s", st.subagent_id, st.unverified_writes)


def _scope_ok(st: Optional[ChildState]) -> bool:
    return st is not None  # only delegated children are supervised; the top-level parent is untouched


def _on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", tool_call_id: str = "",
                      **_: Any) -> Optional[dict]:
    if not _enabled():
        return None
    st = _lookup(session_id)
    if not _scope_ok(st):
        return None
    if st.halted:
        return {"action": "block", "message": "[supervisor] This worker was halted. Stop and return your summary."}

    # 1) LLM judge gate: consume the verdict of the review submitted after the previous tool.
    # The pending review is (future, tool_count_at_submit): a verdict describes the history up
    # to submission, so if the child has moved on by more than `gate_max_stale_calls` tools
    # (e.g. the future timed out at the boundary it was meant for), the verdict is stale —
    # drop it instead of applying it to an unrelated fresh call.
    pending = st.pending_review
    if pending is not None:
        fut, submitted_at = pending
        wait = float(_cfg("judge.gate_wait_seconds", 15))
        max_stale = int(_cfg("judge.gate_max_stale_calls", 2))
        if st.tool_count - submitted_at > max_stale:
            st.pending_review = None
            _audit("judge_stale", subagent_id=st.subagent_id, submitted_at=submitted_at,
                   tool_count=st.tool_count)
        else:
            try:
                verdict = fut.result(timeout=wait)
            except FutureTimeout:
                verdict = None  # still running: let this call through, check again next boundary
            except Exception:
                verdict, st.pending_review = None, None
            if verdict is not None:
                st.pending_review = None
                _audit("judge", subagent_id=st.subagent_id, **verdict)
                if verdict["verdict"] == "halt":
                    return _apply(st, Decision("llm_judge", "interrupt", verdict["reason"]), tool_name, tool_call_id)
                if verdict["verdict"] == "revise":
                    on_revise = _cfg("judge.on_revise", "block")
                    msg = f"Checkpoint review: {verdict['reason']} Next step: {verdict['instruction']}"
                    d = _apply(st, Decision("llm_judge", on_revise if on_revise in ("block", "steer") else "block", msg),
                               tool_name, tool_call_id)
                    if d is not None:
                        return d

    # 2) Deterministic verify-after-write gate.
    gate = _cfg("gate", {}) or {}
    d = verify_gate(gate, st, tool_name, args)
    if d is not None:
        return _apply(st, d, tool_name, tool_call_id)

    # 3) Rule triggers. First blocking directive wins; non-blocking actions all execute.
    block = None
    for dec in evaluate(_triggers(), "pre_tool_call", st, tool_name, args):
        out = _apply(st, dec, tool_name, tool_call_id)
        block = block or out
    return block


def _on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, session_id: str = "",
                       tool_call_id: str = "", status: str = "", **_: Any) -> None:
    if not _enabled():
        return
    st = _lookup(session_id)
    if not _scope_ok(st):
        return
    st.tool_count += 1
    st.consecutive_errors = st.consecutive_errors + 1 if status in ("error", "timeout") else 0
    gate_cfg = _cfg("gate", {}) or {}
    record_mutation(gate_cfg, st, tool_name, args, status)
    # Verify-after-write, post half: the backlog clears only when the verification command
    # actually passed (status ok + no failure-looking output), not merely because it was run.
    if clear_gate_on_verified(gate_cfg, st, tool_name, args, result, status):
        _audit("gate_cleared", subagent_id=st.subagent_id, tool=tool_name)
    keep = int(_cfg("judge.history_window", 8))
    st.history.append({"tool": tool_name, "args": args, "result": result, "status": status})
    del st.history[:-keep]

    for dec in evaluate(_triggers(), "post_tool_call", st, tool_name, args, result, status):
        _apply(st, dec, tool_name, tool_call_id)

    jcfg = _cfg("judge", {}) or {}
    if jcfg.get("enabled") and st.pending_review is None and not st.halted:
        every = int(jcfg.get("every_n_tools", 5))
        on_tools = jcfg.get("on_tools", ["write_file", "patch"])
        if tool_name in on_tools or (every > 0 and st.tool_count % every == 0):
            st.pending_review = (_executor.submit(_judge.review, jcfg, st.goal, list(st.history)),
                                 st.tool_count)


def _on_transform_tool_result(tool_name: str = "", result: Any = None, session_id: str = "",
                              tool_call_id: str = "", **_: Any) -> Optional[str]:
    with _lock:
        notes = _annotations.pop((session_id, tool_call_id), None)
    if not notes:
        return None
    # The hook receives the raw result object (Any), not the serialized string — a dict/list
    # result used to drop annotations silently. Serialize it so the annotation always reaches
    # the child (the dispatcher takes the first str return).
    base = result if isinstance(result, str) else _serialize(result)
    return base + "\n\n" + "\n".join(notes)


def _serialize(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    # Validate the trigger config once at startup. pre_tool_call fails CLOSED on hook errors,
    # so a malformed triggers list that only blows up inside evaluate() would block EVERY child
    # tool call. Log the exact problems and run with triggers disabled rather than paralysing.
    try:
        errors = validate_triggers(ctx.get_config("triggers", []) or [])
    except Exception as exc:
        errors = [f"triggers unreadable: {exc!r}"]
    if errors:
        logger.error("supervisor: ignoring triggers block, %d config error(s): %s",
                     len(errors), "; ".join(errors))
        _triggers_disabled.update(errors)
    else:
        _triggers_disabled.clear()
    ctx.register_hook("subagent_start", _on_subagent_start)
    ctx.register_hook("subagent_stop", _on_subagent_stop)
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
    logger.info("supervisor plugin registered (enabled=%s)", _enabled())


_triggers_disabled: set[str] = set()


def _triggers() -> list:
    """Config triggers unless register() found them malformed (then: none, logged once)."""
    if _triggers_disabled:
        return []
    return _cfg("triggers", []) or []
