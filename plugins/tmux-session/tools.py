"""Tool schemas + handlers for the tmux-session plugin.

Every handler returns a JSON string (the agent-facing contract).
Ops follow the spec: list_sessions, ensure_session, list_panes,
create_window, inspect, send_text, send_keys, execute_shell, wait,
resize, close_window/close_session.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import tmux_core as core


def _str(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _schema(name: str, description: str, properties: dict[str, Any],
            required=None) -> dict:
    params = {"type": "object", "properties": properties}
    if required:
        params["required"] = required
    params["additionalProperties"] = False
    return {"name": name, "description": description, "parameters": params}


def _out(**kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


def _err(msg: str) -> str:
    return _out(ok=False, error=msg)


def _workspace_cwd() -> str:
    """Default cwd for ensure_session when the model omits it."""
    return os.path.expanduser("~")


# ------------------------------------------------------------------ tools ----

def handle_list_sessions(_args, **_kw) -> str:
    sessions = core.list_sessions()
    return _out(ok=True, server_running=core.has_server(), sessions=sessions)


ENSURE_SCHEMA = _schema(
    "tmux_ensure_session",
    "Idempotently create this workspace's persistent tmux session. Returns the "
    "session name to use in later calls. cwd is applied only at creation.",
    {"name": _str("session name; default 'ws'"), "cwd": _str("working directory")},
)

LIST_PANES_SCHEMA = _schema(
    "tmux_list_panes",
    "List panes of a session with stable %N pane ids, current command and cwd.",
    {"session": _str("tmux session name")}, ["session"])

CREATE_WINDOW_SCHEMA = _schema(
    "tmux_create_window",
    "Open a new window (logs / tests / server / ...) inside the session; "
    "returns its fresh pane id.",
    {"session": _str("tmux session name"), "name": _str("window name"),
     "cwd": _str("working directory")}, ["session", "name", "cwd"])

INSPECT_SCHEMA = _schema(
    "tmux_inspect",
    "Capture a pane's visible screen + scrollback (-lines back). Use FIRST "
    "when context is unknown. Snapshot ≠ stdout stream.",
    {"pane_id": _str("%N id from list/ensure/create"),
     "lines": {"type": "integer", "description": "scrollback lines, 1..2000"}},
    ["pane_id"])

SEND_TEXT_SCHEMA = _schema(
    "tmux_send_text",
    "Type literal text into the FRONTMOST interactive program (python REPL, "
    "ssh, psql) and press Enter unless enter=false. Never use on a shell for "
    "commands needing exit codes — use tmux_execute_shell there. Inspect first "
    "when unsure what's running.",
    {"pane_id": _str("%N pane id"), "text": _str("literal text to type"),
     "enter": {"type": "boolean", "description": "press Enter after typing (default true)"}},
    ["pane_id", "text"])

SEND_KEYS_SCHEMA = _schema(
    "tmux_send_keys",
    "Send special keys to the frontmost program: C-c (interrupt), C-d (EOF), "
    "Enter, Escape, Tab, Up/Down (history), etc. This is how you stop a "
    "foreground process without killing the session.",
    {"pane_id": _str("%N pane id"),
     "keys": {"type": "array", "items": {"type": "string"},
              "description": "e.g. ['C-c'] or ['Up','Enter']"}},
    ["pane_id", "keys"])

EXECUTE_SHELL_SCHEMA = _schema(
    "tmux_execute_shell",
    "Run a shell command in the pane's INTERACTIVE SHELL (state persists: cd, "
    "export, venv activate). Returns real exit code; on timeout the process "
    "keeps running — poll with tmux_wait(marker=...). Do NOT run while an "
    "interactive program owns the pane; inspect first.",
    {"pane_id": _str("%N pane id"), "command": _str("shell command"),
     "wait_timeout": {"type": "number",
                      "description": "seconds to wait before returning 'running' (default 60)"},
     "cwd": _str("optional starting dir; persists after cd")},
    ["pane_id", "command"])

WAIT_SCHEMA = _schema(
    "tmux_wait",
    "Wait until either the marked execute_shell call completes (pass marker) "
    "or the pane output goes quiet. Nothing is killed by waiting.",
    {"pane_id": _str("%N pane id"), "marker": _str("marker from a timed-out execute_shell"),
     "timeout": {"type": "number"},
     "quiet_seconds": {"type": "number", "description": "no-change period to treat as idle"}},
    ["pane_id"])

RESIZE_SCHEMA = _schema(
    "tmux_resize",
    "Resize the pane's window (affects TUI programs that read terminal size).",
    {"pane_id": _str("%N pane id"), "cols": {"type": "integer"},
     "rows": {"type": "integer"}}, ["pane_id", "cols", "rows"])

CLOSE_SCHEMA = _schema(
    "tmux_close",
    "Destructive: close a window or whole session. Requires the user's explicit "
    "approval for anything not created by this toolset in this conversation.",
    {"session": _str("session name"),
     "window": _str("window name; omit to kill the whole session")},
    ["session"])


def handle_ensure_session(args, **_kw) -> str:
    name = (args.get("name") or "ws").strip() or "ws"
    cwd = args.get("cwd") or os.path.expanduser("~")
    res = core.ensure_session(name, cwd)
    if not res.get("ok"):
        return _out(**res)
    panes = core.list_panes(res["session"])
    ws = next((p for p in panes if p["window"] == "workspace"), panes[0] if panes else None)
    return _out(ok=True, session=res["session"], created=res.get("created", False),
                workspace_pane=ws)


def handle_list_panes(args, **_kw) -> str:
    panes = core.list_panes(args.get("session") or "")
    return _out(ok=bool(panes), panes=panes) if panes else _err(
        f"no panes/session found for {args.get('session')!r}")


def handle_create_window(args, **_kw) -> str:
    return _out(**core.create_window(args.get("session") or "",
                                     args.get("name") or "win",
                                     args.get("cwd") or os.path.expanduser("~")))


def handle_inspect(args, **_kw) -> str:
    return _out(**core.inspect(args.get("pane_id") or "", int(args.get("lines") or 150)))


def handle_send_text(args, **_kw) -> str:
    return _out(**core.send_text(args.get("pane_id") or "", args.get("text") or "",
                                 bool(args.get("enter", True))))


def handle_send_keys(args, **_kw) -> str:
    keys = args.get("keys") or []
    if isinstance(keys, str):
        keys = [keys]
    return _out(**core.send_keys(args.get("pane_id") or "", keys))


def handle_execute_shell(args, **_kw) -> str:
    try:
        timeout = float(args.get("wait_timeout") or 60.0)
    except (TypeError, ValueError):
        timeout = 60.0
    res = core.execute_shell(args.get("pane_id") or "", args.get("command") or "",
                             wait_timeout=timeout, cwd=args.get("cwd"))
    return _out(**res)


def handle_wait(args, **_kw) -> str:
    try:
        timeout = float(args.get("timeout") or 30.0)
        quiet = float(args.get("quiet_seconds") or 2.0)
    except (TypeError, ValueError):
        timeout, quiet = 30.0, 2.0
    res = core.wait_pane(args.get("pane_id") or "", args.get("marker"),
                         timeout=timeout, quiet_seconds=quiet)
    return _out(**res)


def handle_resize(args, **_kw) -> str:
    try:
        cols, rows = int(args.get("cols") or 120), int(args.get("rows") or 40)
    except (TypeError, ValueError):
        return _err("cols/rows must be integers")
    return _out(**core.resize(args.get("pane_id") or "", cols, rows))


def handle_close(args, **_kw) -> str:
    sess = args.get("session") or ""
    window = args.get("window")
    if window:
        return _out(**core.close_window(sess, window))
    return _out(**core.kill_session(sess))


HANDLERS = {
    "tmux_list_sessions": handle_list_sessions,
    "tmux_ensure_session": handle_ensure_session,
    "tmux_list_panes": handle_list_panes,
    "tmux_create_window": handle_create_window,
    "tmux_inspect": handle_inspect,
    "tmux_send_text": handle_send_text,
    "tmux_send_keys": handle_send_keys,
    "tmux_execute_shell": handle_execute_shell,
    "tmux_wait": handle_wait,
    "tmux_resize": handle_resize,
    "tmux_close": handle_close,
}

LIST_SESSIONS_SCHEMA = _schema(
    "tmux_list_sessions",
    "List sessions on the Hermes private tmux server (and whether it is running).",
    {})

TOOLS = (
    ("tmux_list_sessions", LIST_SESSIONS_SCHEMA, handle_list_sessions, "🗄️"),
    ("tmux_ensure_session", ENSURE_SCHEMA, handle_ensure_session, "🪟"),
    ("tmux_list_panes", LIST_PANES_SCHEMA, handle_list_panes, "🗂️"),
    ("tmux_create_window", CREATE_WINDOW_SCHEMA, handle_create_window, "🪟"),
    ("tmux_inspect", INSPECT_SCHEMA, handle_inspect, "👀"),
    ("tmux_send_text", SEND_TEXT_SCHEMA, handle_send_text, "⌨️"),
    ("tmux_send_keys", SEND_KEYS_SCHEMA, handle_send_keys, "🔑"),
    ("tmux_execute_shell", EXECUTE_SHELL_SCHEMA, handle_execute_shell, "🚀"),
    ("tmux_wait", WAIT_SCHEMA, handle_wait, "⏳"),
    ("tmux_resize", RESIZE_SCHEMA, handle_resize, "↔️"),
    ("tmux_close", CLOSE_SCHEMA, handle_close, "🗑️"),
)
