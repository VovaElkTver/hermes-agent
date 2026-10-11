"""fork-guardrails: policy hooks for Hermes fork work (manifest v2, tested on main@97a9ae8)."""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import re
import threading
from collections import defaultdict

log = logging.getLogger(__name__)

MAX_IDENTICAL_FAILURES = 3
DELEGATION_TOOL = "delegate_task"          # tools/delegate_tool.py
SHELL_TOOLS = {"terminal", "execute_code"}  # execute_code can spawn subprocesses too

# Patterns are a speed bump for the model, not a sandbox (see #73338 discussion).
FORCE_PUSH_PROTECTED = re.compile(
    r"git\s+push\b(?=.*(?:\s--force(?!-with-lease)\b|\s-[a-zA-Z]*f\b|\s\+))(?=.*\b(?:main|master)\b)", re.S)
OUTBOUND = {
    "git-push": re.compile(r"\bgit\s+push\b"),
    "gh-publish": re.compile(r"\bgh\s+(?:pr\s+(?:create|merge)|release\s+create)\b"),
    "registry-push": re.compile(r"\b(?:docker|podman)\s+push\b|\btwine\s+upload\b|\bnpm\s+publish\b"),
    "rm-recursive": re.compile(r"\brm\s+(?:-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r|-r\s+-f|-f\s+-r|--recursive)\b"),
}
DELEGATION_ASK = re.compile(r"\b(?:sub-?agents?|parallel agents?|delegate|delegation)\b", re.I)
DELEGATION_NEG = re.compile(r"\b(?:don'?t|do not|no|never|without)\s+(?:\w+\s+){0,2}(?:sub-?agents?|delegat\w*)", re.I)

_lock = threading.Lock()                     # tool batches run on a thread pool
_failures: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
_delegation_ok: set[str] = set()


def _sid(kw: dict) -> str:
    return kw.get("session_id") or kw.get("task_id") or "_global"


def _sig(tool: str, args: dict) -> str:
    return hashlib.sha1(f"{tool}:{json.dumps(args, sort_keys=True, default=str)}".encode()).hexdigest()


def _shell_text(tool_name: str, args: dict) -> str:
    if tool_name == "terminal":
        return str(args.get("command") or args.get("code") or "")
    if tool_name == "execute_code":
        return str(args.get("code") or "")
    return ""


def pre_llm_call(**kw):
    sid, msg = _sid(kw), kw.get("user_message") or ""
    with _lock:
        if DELEGATION_NEG.search(msg):
            _delegation_ok.discard(sid)
        elif DELEGATION_ASK.search(msg):
            _delegation_ok.add(sid)          # consumed by the next successful delegation (post_tool_call)
    now = datetime.datetime.now().astimezone()
    return {"context": f"Current date: {now:%Y-%m-%d %H:%M %Z}. Verify post-cutoff facts with tools."}


def _decide(tool_name: str, args: dict, kw: dict):
    sid, turn = _sid(kw), kw.get("turn_id") or ""
    with _lock:
        if _failures[(sid, turn)][_sig(tool_name, args)] >= MAX_IDENTICAL_FAILURES:
            return {"action": "block",
                    "message": f"Identical {tool_name} call failed {MAX_IDENTICAL_FAILURES}x this turn; "
                               "change approach or ask the user."}
        if tool_name == DELEGATION_TOOL and sid not in _delegation_ok:
            return {"action": "block", "message": "Delegation is only allowed when the user asks for subagents."}
    text = _shell_text(tool_name, args)
    if not text:
        return None
    if FORCE_PUSH_PROTECTED.search(text):
        return {"action": "block", "message": "Force-push to main/master is forbidden in this fork."}
    for key, pat in OUTBOUND.items():
        if pat.search(text):
            return {"action": "approve", "message": f"{tool_name}: {text[:300]}",
                    "rule_key": f"fork-guardrails:{key}"}
    return None


def pre_tool_call(tool_name: str = "", args: dict | None = None, **kw):
    try:
        return _decide(tool_name, args if isinstance(args, dict) else {}, kw)
    except Exception as exc:                 # host fails OPEN on exceptions, so fail closed here
        log.exception("fork-guardrails pre_tool_call failed")
        return {"action": "block", "message": f"fork-guardrails internal error: {exc}"}


def post_tool_call(tool_name: str = "", args: dict | None = None, **kw):
    sig = _sig(tool_name, args if isinstance(args, dict) else {})
    key = (_sid(kw), kw.get("turn_id") or "")
    status = kw.get("status")
    with _lock:
        if status == "error":
            _failures[key][sig] += 1
        elif status == "ok":
            _failures[key].pop(sig, None)
            if tool_name == DELEGATION_TOOL:
                # One-shot: the user's "use subagents" authorises THAT delegation, not the
                # whole session. After a successful dispatch the flag is consumed; a new
                # round needs a fresh ask (negation in pre_llm_call remains an early revoke).
                _delegation_ok.discard(_sid(kw))
        # status == "blocked": a plugin veto, not a tool failure — don't count it


def on_session_end(**kw):
    """Fires after EVERY turn (despite the name): drop that turn's retry counters."""
    key = (_sid(kw), kw.get("turn_id") or "")
    with _lock:
        _failures.pop(key, None)


def on_session_finalize(**kw):
    """Real session teardown: drop all per-session state."""
    sid = _sid(kw)
    with _lock:
        _delegation_ok.discard(sid)
        for k in [k for k in _failures if k[0] == sid]:
            _failures.pop(k, None)


def register(ctx):
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("on_session_finalize", on_session_finalize)
