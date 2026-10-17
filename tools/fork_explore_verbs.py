"""Control-socket wire verbs for fork_explore continuation (PR-2b).

API layer only: pure functions over the fork_gate journal state machine
(STITCH-DESIGN-8012 §3.2/§3.3). The gateway wires them into ``verb_handlers=``
later — task-b78849c10d consumes exactly this contract: one JSON request in,
``{"ok": ..., "result"|"error": ...}`` out; exceptions are reserved for damaged
parameters (the control-socket catcher turns those into plain errors anyway).

Semantics fixed here:
- pause/resume/close are soft: they flip fork_gate rows and, same-process,
  the in-memory ContinueFlag cache; an in-flight HTTP step always finishes.
- every transition is a CAS whose rowcount decides the winner, so races
  between two writers (or two processes) resolve deterministically in the DB.
- resume NEVER replays anything itself — a branch thread dies with its
  tool-call, so there may be no executor at all. Resume only queues
  ``resumed_pending``; ``fork_explore_tool._redrive_session_forks`` owns the
  actual replay.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from tools.fork_explore_journal import (
    cas_gate,
    ensure_gate_rows,
    get_gate,
    holder_alive,
    _connect,
    journal_path,
)

# close absorbs everything non-terminal; closed itself cannot be re-closed
CLOSEABLE_STATES = ("running", "paused", "resumed_pending")
_PAUSABLE_STATES = ("running", "resumed_pending")


def _err(code: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "error": f"{code}: {detail}"}


def _known_branch(join_id: str) -> bool:
    """join_id exists if a run row OR any gate row exists."""
    if not journal_path().exists():
        return False
    with _connect(journal_path()) as db:
        found = db.execute(
            "SELECT COALESCE((SELECT 1 FROM fork_run WHERE join_id=?),0)"
            " + COALESCE((SELECT 1 FROM fork_gate WHERE join_id=? LIMIT 1),0)",
            (join_id, join_id)).fetchone()[0]
    return bool(found)


def _gate_rows(join_id: str) -> list[dict[str, Any]]:
    with _connect(journal_path()) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(
            "SELECT * FROM fork_gate WHERE join_id=?", (join_id,)).fetchall()]


def _active_flags() -> dict[str, dict[str, Any]]:
    from tools.fork_explore_tool import _ACTIVE_FORK_FLAGS
    return _ACTIVE_FORK_FLAGS


def _drop_local_flags(join_id: str, ref: str) -> int:
    """Lower ContinueFlags of THIS process after the DB write (cache rule §2:
    DB is slowly-true, cache fast-true, disagreement resolves toward DB).
    Returns how many live flags were actually lowered."""
    flags = _active_flags().get(join_id) or {}
    targets = ([flags[ref]] if ref and ref in flags
               else ([] if ref else list(flags.values())))
    stopped = sum(1 for f in targets if f.get())
    for f in targets:
        f.stop()
    return stopped


def _raise_local_flags(join_id: str, ref: str) -> int:
    """Resume side of the cache rule: re-arm same-process paused flags."""
    flags = _active_flags().get(join_id) or {}
    targets = ([flags[ref]] if ref and ref in flags
               else ([] if ref else list(flags.values())))
    raised = sum(1 for f in targets if not f.get())
    for f in targets:
        f.resume()
    return raised


# ------------------------------------------------------------------ fork-pause

def pause_branch(join_id: str, ref: str = "", variant: str = "soft") -> dict[str, Any]:
    """Pause a branch (ref given) or the whole fork (ref='') at its next step
    boundary. Idempotency by distinction, not collapse: pausing something
    already paused/closed answers ``already_paused`` / ``already_closed``
    instead of pretending a new mark happened. The hard variant degrades to
    soft by design (§VERDICT-2): from outside the owning process an in-flight
    HTTP step is reachable only through the step client's own timeout."""
    if variant not in ("soft", "hard"):
        raise ValueError("variant must be 'soft' or 'hard'")
    if not _known_branch(join_id):
        return _err("unknown_branch",
                    f"{join_id!r} has neither a run nor a gate row")

    requested = len(_active_flags().get(join_id) or {})
    if ref:
        row = get_gate(join_id, ref) or {}
        if row.get("state") == "closed":
            return _err("already_closed", f"{join_id}/{ref} is terminal")
        if row.get("state") == "paused":
            return _err("already_paused", f"{join_id}/{ref} is already paused")
        ensure_gate_rows(join_id, [ref])
        marked = int(cas_gate(join_id, ref, _PAUSABLE_STATES, "paused",
                              reason="human"))
    else:
        # group gate: ONE update sweeps every currently-pausable row of the
        # fork, group row included — exactly the k-in-one-statement property
        # the group row exists for (§5)
        marked = 0
        with _connect(journal_path()) as db:
            cur = db.execute(
                "UPDATE fork_gate SET state='paused', reason='human',"
                " updated_at=? WHERE join_id=?"
                " AND state IN ('running','resumed_pending')",
                (time.time(), join_id))
            marked = int(cur.rowcount)
    stopped_inflight = _drop_local_flags(join_id, ref)
    return {"ok": True, "result": {
        "join_id": join_id, "ref": ref, "requested": requested,
        "stopped_inflight": stopped_inflight, "marked_paused": marked,
        "variant": "soft"}}


# ----------------------------------------------------------------- fork-resume

def resume_branch(join_id: str, ref: str = "") -> dict[str, Any]:
    """Queue paused/dead branches for lazy re-drive.

    - double resume = no-op: rows already ``resumed_pending`` add zero effects;
    - resume-after-crash succeeds: a ``running`` row whose holder process is
      provably dead is healed to ``paused`` first, then queued like any other;
    - a healthy ``running`` row is ``already_running`` (caller error: effects
      would lie — there is nothing to re-drive);
    - a ``closed`` row stays terminal under any verb.
    """
    if not _known_branch(join_id):
        return _err("unknown_branch",
                    f"{join_id!r} has neither a run nor a gate row")
    if ref:
        targets: list[str | None] = [ref]
    else:
        # fork-wide: group row governs the fork marker, each branch row queues
        targets = [None] + [r["branch_ref"] for r in _gate_rows(join_id)
                            if r["branch_ref"]]
    queued = 0
    for t in targets:
        key = "" if t is None else t
        row = get_gate(join_id, key) or {}
        state = row.get("state", "")
        if not state:
            # pre-PR-2b fork without gate rows yet: make the row real and
            # treat it as live-running so the standard flow can queue it
            ensure_gate_rows(join_id, [key])
            state = "running"
            row = {"holder": ""}
        if state == "resumed_pending":
            continue  # idempotent re-resume
        if state == "closed":
            return _err("already_closed", f"{join_id}/{key or '<fork>'} is terminal")
        if state == "running":
            holder = row.get("holder", "")
            if not holder or holder_alive(holder):
                return _err("already_running",
                            f"{join_id}/{key or '<fork>'} is not paused")
            cas_gate(join_id, key, ("running",), "paused", reason="crashed")
            state = "paused"
        if state == "paused":
            if cas_gate(join_id, key, ("paused",), "resumed_pending"):
                queued += 1
    _raise_local_flags(join_id, ref)
    return {"ok": True, "result": {
        "join_id": join_id, "ref": ref, "resumed_inflight": 0,
        "queued_for_redrive": queued, "disposition": "deferred"}}


# ------------------------------------------------------------------ fork-close

def close_branch(join_id: str, ref: str = "", reason: str = "human") -> dict[str, Any]:
    """Soft terminal close (eval-gate verdict or human decision). Terminal
    means terminal: even ``resumed_pending`` work is absorbed. Double close
    changes nothing by construction and answers ``already_closed``."""
    if not _known_branch(join_id):
        return _err("unknown_branch",
                    f"{join_id!r} has neither a run nor a gate row")
    targets = [ref] if ref else [r["branch_ref"] for r in _gate_rows(join_id)
                                 if r["branch_ref"]]
    closed = 0
    for t in targets:
        row = get_gate(join_id, t) or {}
        if row.get("state") == "closed":
            return _err("already_closed", f"{join_id}/{t} is terminal")
        closed += int(cas_gate(join_id, t, CLOSEABLE_STATES, "closed",
                               reason=reason))
    _drop_local_flags(join_id, ref)
    return {"ok": True, "result": {"join_id": join_id, "ref": ref,
                                   "closed": closed, "reason": reason}}
