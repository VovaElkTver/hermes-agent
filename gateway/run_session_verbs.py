"""Control-socket verbs to interrupt/steer a running chat session out-of-band (PR-A).

The scheduler/controller (a separate process — the pump, ``ctl`` tooling, or any local
peer with the gateway-owned control socket) can preempt or redirect a live session turn
without a chat platform: the same plumbing ``/stop`` and ``/steer`` use from chat,
reachable from a machine or process that has no chat client in the loop.

Attribution discipline (#112647 / #130207): preemption is a SYSTEM stop, never a user
stop. Every ``interrupt-session`` publishes ``tool_reason`` (default
``controller_preemption``), so ``interrupt_issuer`` attributes the turn exit to the
scheduler and the busy path does not re-render it as "user sent a new message" — even
when a ``message`` rides along with the preempt.

Handoff discipline (acceptance #4): before dropping the turn the verb snapshots
``agent.get_activity_summary()`` (what it was doing, which tool it stopped on,
iteration/budget) and returns it as a ``handoff`` string; it also stages the note into
the session's pending sidecar notes so the NEXT turn resumes cheaply without a
re-narration. The caller (pump) persists the note into the ctl step via
``controller.model.preempt_step``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# The trusted fixed category for scheduler preempts. ``tool_reason`` is a slug, never
# the sender's free text (same invariant as interrupt_control's user categories).
DEFAULT_PREEMPT_REASON = "controller_preemption"


def _slug_reason(reason: Optional[str]) -> str:
    """tool_reason is a fixed category slug — normalize, never store free text."""
    reason = str(reason or "").strip().lower()
    cleaned = "_".join(reason.split())
    return cleaned or DEFAULT_PREEMPT_REASON


def _resolve_session(
    runner: Any, session: str
) -> tuple[Optional[str], Optional[Any], Optional[str]]:
    """Resolve a session key (exact first, then a unique running-key substring) to
    ``(session_key, agent, error)``. ``agent`` is ``None`` for a known-but-idle session
    and for the pending-turn sentinel (turn starting, no agent attached yet)."""
    from gateway.run import _AGENT_PENDING_SENTINEL  # late import: facade cycle

    peek = getattr(runner, "_peek_session_state", None)
    if not callable(peek) or not session:
        return None, None, "'session' param is required"
    key: Optional[str] = session
    state: Any = peek(session)
    if state is None:
        items_fn = getattr(runner, "_running_agent_items", None)
        items: list[tuple[str, Any]] = (
            list(items_fn()) if callable(items_fn) else []  # pyright: ignore[reportArgumentType]
        )
        matches = [k for k, _a in items if session.lower() in str(k).lower()]
        if len(matches) > 1:
            return None, None, f"ambiguous session {session!r}; running: {matches}"
        if len(matches) == 1:
            key, state = matches[0], peek(matches[0])
    if state is None:
        return key, None, None  # unknown/idle session — caller reports not-running
    agent = state.turn.agent if getattr(state, "turn", None) is not None else None
    if agent is _AGENT_PENDING_SENTINEL:
        return key, None, "turn starting (pending sentinel) — no agent attached yet"
    return key, agent, None


def _activity_snapshot(agent: Any) -> dict[str, Any]:
    """Cheap 'what is this turn doing' read: the agent already maintains
    ``_last_activity_desc`` / ``_current_tool`` for spinners; reading attributes
    directly survives bare/partial agents (no get_activity_summary needed)."""
    summary: dict[str, Any] = {}
    try:
        fn = getattr(agent, "get_activity_summary", None)
        if callable(fn):
            raw = fn()
            if isinstance(raw, dict):
                summary = dict(raw)
    except Exception:
        logger.debug("session-verbs: activity snapshot failed", exc_info=True)
    return {
        "doing": str(getattr(agent, "_last_activity_desc", "") or summary.get("description") or ""),
        "current_tool": str(getattr(agent, "_current_tool", "") or summary.get("current_tool") or ""),
        "api_call": summary.get("api_call_count"),
        "budget_used": summary.get("budget_used"),
        # get_activity_summary's real key is budget_max; budget_total kept as the
        # alias a hand-rolled summary may use.
        "budget_total": summary.get("budget_total") or summary.get("budget_max"),
    }


def _handoff_note(agent: Any, reason: str) -> str:
    """The one-liner a resumer needs: чем занимался, на какой тулзе встал."""
    snap = _activity_snapshot(agent)
    doing = snap["doing"] or "unknown activity"
    tool = snap["current_tool"]
    where = f"stopped on tool: {tool}" if tool and tool != "none" else "between tools"
    iter_part = ""
    if snap.get("api_call") is not None:
        iter_part = f" (iteration {snap['api_call']}"
        if snap.get("budget_total"):
            iter_part += f"/{snap['budget_total']}"
        iter_part += ")"
    return (
        f"[preempted: {reason}] was doing: {doing}; {where}{iter_part}. "
        "Resume from this point — do not redo finished work; re-anchor on the current contract."
    )


def _stage_sidecar_note(runner: Any, session_key: str, note: str) -> bool:
    """Stage the handoff note into the session's pending sidecar notes so the next turn
    sees it (consumed once at turn start). Defensive: never fail the verb over it."""
    try:
        state = runner._peek_session_state(session_key)
        conv = getattr(state, "conversation", None) if state is not None else None
        if conv is not None:
            existing = list(getattr(conv, "sidecar_notes", None) or [])
            setter = getattr(runner, "_set_pending_turn_sidecar_notes", None)
            if callable(setter):
                setter(session_key, existing + [note])
                return True
    except Exception:
        logger.debug("session-verbs: sidecar handoff staging failed", exc_info=True)
    return False


def interrupt_session_verb(runner: Any) -> Callable[..., dict[str, Any]]:
    """Factory for the ``interrupt-session`` handler (runner-bound).

    params: ``{"session": "<key or unique substring>", "reason": "<slug, default
    controller_preemption>", "message": "<optional text delivered with the preempt>",
    "hard": <bool, default false — cooperative pause by default>}``.
    """

    def handler(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        params = params or {}
        session = str(params.get("session") or "").strip()
        key, agent, error = _resolve_session(runner, session)
        if error is not None:
            return {"interrupted": False, "session": key or session, "error": error}
        if agent is None:
            return {
                "interrupted": False,
                "session": key,
                "reason_for_none": error or "no running turn",
            }

        from agent.interrupt_control import interrupt_issuer  # late import: safe, no cycle

        # Default tool_reason names the sender when one is attributed (the wire path
        # always is); unattributed in-process callers keep the legacy default.
        _origin = str(params.get("origin") or "").strip()
        reason = _slug_reason(params.get("reason")
                              or (f"preempted_by_{_origin.replace(':', '_')}" if _origin else ""))
        message = str(params.get("message") or "").strip() or None
        hard = bool(params.get("hard"))
        handoff = _handoff_note(agent, reason)
        staged = _stage_sidecar_note(runner, key, handoff) if key else False

        try:
            ok = bool(agent.interrupt(message, hard_cancel=hard, tool_reason=reason))
        except TypeError:
            # Third-party agent whose interrupt() predates the tool_reason ABI — the
            # preempt still lands, only the attribution degrades.
            logger.debug("session-verbs: agent.interrupt lacks the tool_reason ABI", exc_info=True)
            try:
                ok = bool(agent.interrupt(message) if message else agent.interrupt())
            except Exception as exc:
                logger.debug("session-verbs: legacy interrupt failed", exc_info=True)
                return {"interrupted": False, "session": key, "error": f"interrupt failed: {exc}"}

        issuer = interrupt_issuer(agent)
        logger.info(
            "control: interrupt-session key=%s ok=%s issuer=%s hard=%s origin=%s (out-of-band preempt)",
            key, ok, issuer, hard, params.get("origin"),
        )
        return {
            "interrupted": ok,
            "session": key,
            "origin": params.get("origin"),
            "tool_reason": reason,
            "issuer": issuer,
            "hard": hard,
            "handoff": handoff,
            "handoff_staged": staged,
            "activity": _activity_snapshot(agent),
        }

    return handler


def steer_session_verb(runner: Any) -> Callable[..., dict[str, Any]]:
    """Factory for the ``steer-session`` handler (runner-bound).

    params: ``{"session": "<key or unique substring>", "message": "<mid-turn steer text>"}``.
    Unlike chat's ``/steer`` (rejected when no agent is live), this returns
    ``accepted: false`` with ``reason: "no-running-turn"`` so the scheduler can fall back
    to queuing its own follow-up. Fan-out to running children mirrors
    ``_steer_running_agent`` (the scheduler steers the whole subtree).
    """

    def handler(params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        params = params or {}
        session = str(params.get("session") or "").strip()
        text = str(params.get("message") or "").strip()
        key, agent, error = _resolve_session(runner, session)
        if error is not None:
            return {"accepted": False, "session": key or session, "error": error}
        if agent is None:
            return {"accepted": False, "session": key, "reason": "no-running-turn"}
        if not text:
            return {"accepted": False, "session": key, "error": "empty 'message'"}
        # Machine-sent steer carries a visible origin label into the same pending-steer
        # inbox humans use (PR-A canon: one inbox, attributed senders).
        origin = str(params.get("origin") or "").strip()
        if origin and not text.startswith(f"[from {origin}"):
            text = f"[from {origin}] {text}"

        accepted = False
        children: list[Any] = []
        try:
            accepted = bool(agent.steer(text))
        except Exception as exc:
            logger.debug("session-verbs: steer failed", exc_info=True)
            return {"accepted": False, "session": key, "error": f"steer failed: {exc}"}
        # Fan-out to running children — best-effort, mirrors the chat /steer path and
        # interrupt()'s own child walk (same lock, same swallow-with-log per child).
        from agent.interrupt_control import _ic_lock
        with _ic_lock(agent, "_active_children_lock"):
            children = list(getattr(agent, "_active_children", None) or [])
        for child in children:
            try:
                child.steer(text)
            except Exception:
                logger.debug("session-verbs: child steer failed", exc_info=True)

        logger.info(
            "control: steer-session key=%s accepted=%s children=%d origin=%s (out-of-band steer)",
            key, accepted, len(children), params.get("origin"),
        )
        return {"accepted": accepted, "session": key, "children": len(children),
                "origin": origin or None}

    return handler
