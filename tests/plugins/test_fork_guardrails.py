"""Tests for the bundled fork-guardrails plugin (plugins/fork-guardrails/).

Offline: the plugin module is loaded standalone and the hooks are driven directly
with the same kwarg shapes the dispatcher uses (kwargs-only, block > approve).
Mirrors how test_supervisor_plugin.py loads its subject from the repo tree.
"""

import importlib.util
import sys
import types

import pytest

ROOT = None  # set below


def _load_plugin():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    reg = types.ModuleType("tools.delegate_tool_registry")
    reg._active_subagents, reg._active_subagents_lock = {}, __import__("threading").Lock()
    sys.modules.setdefault("tools", types.ModuleType("tools"))
    sys.modules["tools.delegate_tool_registry"] = reg
    plugin_dir = root / "plugins" / "fork-guardrails"
    spec = importlib.util.spec_from_file_location(
        "fork_guardrails_under_test", plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "fork_guardrails_under_test"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["fork_guardrails_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


fg = _load_plugin()


@pytest.fixture()
def hooks():
    fg._failures.clear()
    fg._delegation_ok.clear()
    reg = {}

    class Ctx:
        def register_hook(self, name, cb):
            reg.setdefault(name, []).append(cb)

    fg.register(Ctx())
    return reg


def invoke(hooks, name, **kw):
    """Mirror plugins_dispatch: kwargs-only, exceptions isolated per callback."""
    out = []
    for cb in hooks.get(name, []):
        out.append(cb(**kw))
    return out


def directive(hooks, tool, args, session_id="s1", turn_id="t1"):
    """Mirror _get_pre_tool_call_directive_details precedence: block wins, else first approve."""
    res = invoke(hooks, "pre_tool_call", tool_name=tool, args=args, task_id="k",
                 session_id=session_id, tool_call_id="c", turn_id=turn_id,
                 api_request_id="a", middleware_trace=[])
    ap = None
    for r in res:
        if not isinstance(r, dict):
            continue
        if r.get("action") == "block" and r.get("message"):
            return ("block", None)
        if r.get("action") == "approve" and ap is None:
            ap = ("approve", r.get("rule_key"))
    return ap or (None, None)


@pytest.mark.parametrize("cmd,expected", [
    ("ls", (None, None)),
    ("git push origin feature", ("approve", "fork-guardrails:git-push")),
    ("git push -f origin main", ("block", None)),
    ("git push origin +main", ("block", None)),
    ("git push --force-with-lease origin main", ("approve", "fork-guardrails:git-push")),
    ("rm -rf build/", ("approve", "fork-guardrails:rm-recursive")),
    ("gh pr create --fill", ("approve", "fork-guardrails:gh-publish")),
])
def test_shell_policies(hooks, cmd, expected):
    got = directive(hooks, "terminal", {"command": cmd})
    if expected[0] is None:
        assert got == (None, None)
    else:
        assert got[0] == expected[0]
        if expected[1]:
            assert got[1] == expected[1]


def test_gh_and_execute_code_covered(hooks):
    assert directive(hooks, "terminal", {"command": "gh pr create --fill"})[0] == "approve"
    ec = directive(hooks, "execute_code",
                   {"code": "import subprocess; subprocess.run('git push origin x', shell=True)"})
    assert ec == ("approve", "fork-guardrails:git-push")


def test_delegation_requires_ask(hooks):
    assert directive(hooks, "delegate_task", {"goal": "x"}) == ("block", None)
    invoke(hooks, "pre_llm_call", session_id="s1", user_message="please use parallel subagents",
           conversation_history=[], is_first_turn=True, model="m", platform="cli")
    assert directive(hooks, "delegate_task", {"goal": "x"}) == (None, None)


def test_delegation_flag_one_shot_per_round(hooks):
    """Author's fix under review: 'delegate' authorises ONE dispatch, not the session."""
    invoke(hooks, "pre_llm_call", session_id="s1", user_message="delegate this",
           conversation_history=[], is_first_turn=False, model="m", platform="cli")
    assert directive(hooks, "delegate_task", {"goal": "a"}) == (None, None)
    # successful dispatch consumes the flag
    invoke(hooks, "post_tool_call", tool_name="delegate_task", args={"goal": "a"},
           result="dispatched", status="ok", task_id="k", duration_ms=1,
           session_id="s1", turn_id="t1")
    assert directive(hooks, "delegate_task", {"goal": "b"}) == ("block", None)
    # a fresh ask re-enables it
    invoke(hooks, "pre_llm_call", session_id="s1", user_message="again, use subagents",
           conversation_history=[], is_first_turn=False, model="m", platform="cli")
    assert directive(hooks, "delegate_task", {"goal": "c"}) == (None, None)


def test_delegation_negation_revokes(hooks):
    invoke(hooks, "pre_llm_call", session_id="s1", user_message="use subagents",
           conversation_history=[], is_first_turn=True, model="m", platform="cli")
    invoke(hooks, "pre_llm_call", session_id="s1", user_message="do not use subagents anymore",
           conversation_history=[], is_first_turn=False, model="m", platform="cli")
    assert directive(hooks, "delegate_task", {"goal": "x"}) == ("block", None)


def test_identical_failure_streak_blocks(hooks):
    for _ in range(3):
        invoke(hooks, "post_tool_call", tool_name="terminal", args={"command": "make"},
               result="{}", status="error", task_id="k", duration_ms=1,
               session_id="s1", turn_id="t1")
    assert directive(hooks, "terminal", {"command": "make"}) == ("block", None)
    # a plugin block is not a tool failure
    invoke(hooks, "post_tool_call", tool_name="terminal", args={"command": "make"},
           result="{}", status="blocked", session_id="s1", turn_id="t1")
    assert directive(hooks, "terminal", {"command": "make"}) == ("block", None)  # streak persists
    # other turns/sessions unaffected
    assert directive(hooks, "terminal", {"command": "make"}, turn_id="t2") == (None, None)


def test_success_clears_streak(hooks):
    invoke(hooks, "post_tool_call", tool_name="terminal", args={"command": "make"},
           result="x", status="error", session_id="s1", turn_id="t1")
    invoke(hooks, "post_tool_call", tool_name="terminal", args={"command": "make"},
           result="x", status="ok", session_id="s1", turn_id="t1")
    invoke(hooks, "post_tool_call", tool_name="terminal", args={"command": "make"},
           result="x", status="error", session_id="s1", turn_id="t1")
    assert directive(hooks, "terminal", {"command": "make"}) == (None, None)


def test_session_end_is_per_turn_reset(hooks):
    for _ in range(3):
        invoke(hooks, "post_tool_call", tool_name="terminal", args={"command": "make"},
               result="x", status="error", session_id="s1", turn_id="t1")
    invoke(hooks, "on_session_end", session_id="s1", turn_id="t1", completed=True,
           failed=False, interrupted=False)
    assert directive(hooks, "terminal", {"command": "make"}) == (None, None)


def test_finalize_drops_session_state(hooks):
    invoke(hooks, "pre_llm_call", session_id="s1", user_message="use subagents",
           conversation_history=[], is_first_turn=True, model="m", platform="cli")
    invoke(hooks, "on_session_finalize", session_id="s1", platform="cli")
    assert "s1" not in fg._delegation_ok


def test_reduced_payloads_do_not_crash(hooks):
    assert invoke(hooks, "on_session_end") == [None]
    assert invoke(hooks, "on_session_finalize", session_id="s1", platform="cli") == [None]


def test_internal_error_fails_closed(hooks, monkeypatch):
    monkeypatch.setattr(fg, "_failures", None)  # force an exception inside _decide
    assert directive(hooks, "terminal", {"command": "ls"}) == ("block", None)


def test_pre_llm_injects_date(hooks):
    out = invoke(hooks, "pre_llm_call", session_id="s1", user_message="hi",
                 conversation_history=[], is_first_turn=True, model="m", platform="cli")
    assert isinstance(out[0], dict) and "Current date:" in out[0]["context"]
