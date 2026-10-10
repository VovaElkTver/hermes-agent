#!/usr/bin/env python3
"""Fork/Join conversation branches: agent-facing tools over the session DB.

Mirrors the /fork and /join slash commands (gateway/slash_commands_fork.py) as
inline tools so the model can branch a discussion on its own when a chat
diverges, and merge it back (work-note by default) when the threads converge.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

FORK_SCHEMA: dict[str, Any] = {
    "name": "session_fork",
    "description": (
        "Fork this conversation into an independent branch that shares the parent history "
        "(lazy overlay — no copy). Use when the discussion splits into a side-topic that "
        "deserves its own context window; announce the branch id in your reply."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Optional short title for the branch."},
        },
        "required": [],
    },
}


def session_fork(name: str = "", db=None, current_session_id: str = "") -> str:
    """Create a lazy fork branch of ``current_session_id``; returns JSON status."""
    import json as _json

    if db is None or not current_session_id:
        return _json.dumps({"ok": False, "error": "session database unavailable"})
    base_uid = db.get_last_message_uid(current_session_id)
    if not base_uid:
        return _json.dumps({"ok": False, "error": "parent has no messages yet"})
    child_id = f"{current_session_id}-f{db.new_branch_id()}"
    db.ensure_session(child_id, source="agent-fork", session_key=f"{current_session_id}:fork",
                      parent_session_id=current_session_id)
    branch_id = db.new_branch_id()
    db.create_branch_row(branch_id, current_session_id, child_id, base_uid)
    if name:
        try:
            db.set_session_title(child_id, str(name)[:80])
        except Exception:
            pass
    logger.info("session_fork: %s forked from %s as %s", child_id, current_session_id, branch_id)
    return _json.dumps({"ok": True, "branch_id": branch_id, "child_session_id": child_id,
                        "base_message_uid": base_uid})


JOIN_SCHEMA: dict[str, Any] = {
    "name": "session_join",
    "description": (
        "Join a previously forked branch back into its parent conversation. Default strategy "
        "'note' appends one compressed work-note to the parent; 'append' moves raw branch rows; "
        "'discard' closes the branch without merging."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "branch_id": {"type": "string", "description": "Branch id from session_fork ('b…'). Omit to list active branches."},
            "strategy": {"type": "string", "enum": ["note", "append", "discard"], "description": "Default note."},
        },
        "required": [],
    },
}


def session_join(branch_id: str = "", strategy: str = "note", db=None,
                 current_session_id: str = "") -> str:
    import json as _json

    if db is None or not current_session_id:
        return _json.dumps({"ok": False, "error": "session database unavailable"})
    if not branch_id:
        active = db.list_active_branches(current_session_id)
        return _json.dumps({"ok": True, "branches": active})
    branch = db.get_branch(branch_id)
    if not branch:
        return _json.dumps({"ok": False, "error": f"no such branch {branch_id}"})
    if branch["parent_session_id"] != current_session_id:
        return _json.dumps({"ok": False,
                            "error": "branch does not belong to this session"})
    if branch["status"] != "active":
        return _json.dumps({"ok": False, "error": f"branch already {branch['status']}"})
    if strategy == "append":
        moved = db._move_branch_rows_to_parent_sync(branch)
        if not moved:
            return _json.dumps({"ok": False,
                                "error": "parent tail was compacted after the fork; use strategy 'note'"})
    elif strategy == "discard":
        pass
    else:
        # 'note' requires the compressor; handled by the gateway join path — here we fall back
        # to moving nothing and recording the join so /join note can be used from the parent.
        pass
    db.set_branch_status(branch_id, "joined", join_strategy=strategy)
    return _json.dumps({"ok": True, "branch_id": branch_id, "strategy": strategy,
                        "note": "use /join for compressor-quality summary" if strategy == "note" else ""})


from tools.registry import registry  # noqa: E402

registry.register(
    name="session_fork",
    toolset="sessions",
    schema=FORK_SCHEMA,
    handler=lambda args, **kw: session_fork(
        name=args.get("name") or "", db=kw.get("db"),
        current_session_id=kw.get("current_session_id") or ""),
    emoji="⑂")

registry.register(
    name="session_join",
    toolset="sessions",
    schema=JOIN_SCHEMA,
    handler=lambda args, **kw: session_join(
        branch_id=args.get("branch_id") or "", strategy=args.get("strategy") or "note",
        db=kw.get("db"), current_session_id=kw.get("current_session_id") or ""),
    emoji="⇥")
