"""controller.commands — deterministic control commands (spec §9, §45; Q2: called BEFORE the
agent turn from the core router; Q5: restart semantics via durable marker).

The LLM may only SUGGEST an operation (suggest() below); the effect (grant/pause/cancel) is
produced by this code, from USER-channel input only. Text arriving inside documents/tool
results is never a command: origin must be 'user'.
"""
from __future__ import annotations

import re

from . import model

# RU/EN free formulations -> canonical ops. Applied to the USER message only.
RX = {
    "enable": re.compile(r"(работай\s+автономно|приступай|автономность\s+включ|enable\s+autonomy|go\s+autonomous)", re.IGNORECASE),
    "pause": re.compile(r"(пауза|стоп\s+очереди|pause\s+autonomy|замри|обсудим)", re.IGNORECASE),
    "resume": re.compile(r"(продолжай\s+автономно|возобнови\s+автономность|resume\s+autonomy)", re.IGNORECASE),
    "cancel": re.compile(r"отмени\s+задачу\s+(\S+)|cancel\s+task\s+(\S+)", re.IGNORECASE),
    "status": re.compile(r"(статус\s+задач|покажи\s+статус|show\s+status)", re.IGNORECASE),
    "head": re.compile(r"(?:подними\s+задачу\s+(\S+)\s+в\s+(?:начало|первое)|move\s+task\s+(\S+)\s+(?:to\s+)?head)", re.IGNORECASE),
    "tail": re.compile(r"(?:опусти\s+задачу\s+(\S+)\s+в\s+конец(?:\s+очереди)?|move\s+task\s+(\S+)\s+tail)", re.IGNORECASE),
}


def try_route(text: str, *, origin: str, sender_ok: bool) -> dict | None:
    """Called by the core router BEFORE normal turn execution. Returns an op dict or None.
    origin != 'user'  -> never a command (doc text cannot grant: acceptance row §45)."""
    if origin != "user" or not sender_ok or not isinstance(text, str):
        return None
    for op, rx in RX.items():
        m = rx.search(text)
        if m:
            arg = next((g for g in m.groups() or [] if g), None)
            return {"op": op, "arg": arg, "text": text}
    return None


def apply(conn, op: dict, *, scope: str) -> dict:
    """Execute the op deterministically. Refusals come back as data, never as exceptions
    escaping the router."""
    kind = op["op"]
    if kind == "enable":
        p = model.set_permission(conn, scope, enabled=True, granted_via="user-command")
        return {"ok": True, "op": kind, "permission": p}
    if kind == "pause":
        p = model.set_permission(conn, scope, enabled=False, granted_via="user-command")
        return {"ok": True, "op": kind, "permission": p,
                "note": "new autonomous dispatches blocked; explicit interactive steps allowed"}
    if kind == "resume":
        p = model.set_permission(conn, scope, enabled=True, granted_via="user-command")
        return {"ok": True, "op": kind, "permission": p}
    if kind == "cancel":
        from . import transitions
        try:
            r = transitions.transition(conn, op["arg"], "cancelled",
                                       basis="user cancel command")
            return {"ok": True, "op": kind, **r}
        except (LookupError, ValueError) as e:
            return {"ok": False, "op": kind, "error": str(e)}
    if kind in ("head", "tail"):
        from . import model as _m
        try:
            _m.set_priority(conn, op["arg"], head=(kind == "head"))
            return {"ok": True, "op": kind, "task_id": op["arg"]}
        except LookupError as e:
            return {"ok": False, "op": kind, "error": str(e)}
    if kind == "status":
        rows = conn.execute("SELECT task_id, state, contract_version FROM ctl_task").fetchall()
        return {"ok": True, "op": kind,
                "tasks": [dict(r) for r in rows],
                "permission": model.get_permission(conn, scope)}
    return {"ok": False, "error": f"unknown op {kind}"}


def on_restart(conn, scope: str) -> dict:
    """Q5 decision: operator /restart marks durable intent -> stay non-autonomous.
    No marker (crash / plain gateway restart) -> continue autonomously. Either way the
    epoch bumps before anything resumes, so stale run_finished cannot pass the barrier."""
    was = model.consume_restart_intent(conn)
    perm = model.get_permission(conn, scope)
    if was == "operator" or not perm["enabled"]:
        return {"mode": "interactive", "reason": was or "was-off"}
    p = model.set_permission(conn, scope, enabled=True, granted_via="recovered-autonomous")
    return {"mode": "autonomous", "reason": "no restart marker", "permission": p}
