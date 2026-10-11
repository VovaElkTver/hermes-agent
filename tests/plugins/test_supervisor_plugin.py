"""Tests for the bundled supervisor plugin (plugins/supervisor/).

Policy engine + plugin hook flow with a stubbed delegate registry; runs offline
(no gateway, no LLM). mirrors how the security-guidance plugin tests load their
subject from the repo tree.
"""

import sys
import time
import types
from pathlib import Path

import pytest

import hermes_yaml as yaml

ROOT = Path(__file__).resolve().parents[2]

CFG = yaml.safe_load((ROOT / "plugins" / "supervisor" / "config.example.yaml").read_text())
SETTINGS = CFG["plugins"]["entries"]["supervisor"]["settings"]


# ── stub tools.delegate_tool_registry ────────────────────────────────────────────────────────
calls = {"steer": [], "interrupt": []}
reg = types.ModuleType("tools.delegate_tool_registry")
reg._active_subagents, reg._active_subagents_lock = {}, __import__("threading").Lock()
reg.steer_subagent = lambda sid, text, **k: calls["steer"].append((sid, text)) or True
reg.interrupt_subagent = lambda sid: calls["interrupt"].append(sid) or True
sys.modules.setdefault("tools", types.ModuleType("tools"))
sys.modules["tools.delegate_tool_registry"] = reg

def _load_plugin():
    """Import plugins/supervisor/ standalone (repo plugins/__init__ shadows plain import)."""
    import importlib.util
    plugin_dir = ROOT / "plugins" / "supervisor"
    spec = importlib.util.spec_from_file_location(
        "supervisor_plugin_under_test", plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)])
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "supervisor_plugin_under_test"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["supervisor_plugin_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod

supervisor = _load_plugin()
ChildState = supervisor.ChildState
evaluate = supervisor.evaluate
verify_gate = supervisor.verify_gate


class Ctx:
    def __init__(self, settings):
        self.settings, self.hooks = settings, {}

    def get_config(self, key, default=None):
        node = self.settings
        for seg in key.split("."):
            if not isinstance(node, dict) or seg not in node:
                return default
            node = node[seg]
        return node

    def register_hook(self, name, fn):
        self.hooks[name] = fn


@pytest.fixture()
def h(tmp_path):
    s = dict(SETTINGS, log_path=str(tmp_path / "audit.jsonl"))
    ctx = Ctx(s)
    supervisor.register(ctx)
    supervisor._children.clear()
    calls["steer"].clear(); calls["interrupt"].clear()
    ctx.hooks["subagent_start"](child_session_id="child-1", child_subagent_id="sa-0-abc",
                                parent_session_id="parent", parent_subagent_id="sa-orch",
                                child_role="leaf", child_goal="refactor utils")
    return ctx.hooks, tmp_path


def pre(h, tool, args, tcid="t"):
    return h["pre_tool_call"](tool_name=tool, args=args, session_id="child-1", tool_call_id=tcid)


def post(h, tool, args, result="ok", status="ok", tcid="t"):
    return h["post_tool_call"](tool_name=tool, args=args, result=result, status=status,
                               session_id="child-1", tool_call_id=tcid)


def test_parent_session_is_never_touched(h):
    hooks, _ = h
    assert hooks["pre_tool_call"](tool_name="terminal", args={"command": "git push --force"},
                                  session_id="parent", tool_call_id="x") is None


def test_block_rule(h):
    hooks, _ = h
    d = pre(hooks, "terminal", {"command": "git push origin main --force"})
    assert d["action"] == "block" and "Force-push" in d["message"]


def test_verify_after_write_gate(h):
    hooks, _ = h
    for i, p in enumerate(["a.py", "b.py"]):
        assert pre(hooks, "write_file", {"path": p}) is None
        post(hooks, "write_file", {"path": p})
    blocked = pre(hooks, "patch", {"path": "c.py"})
    assert blocked["action"] == "block" and "a.py" in blocked["message"]
    # A PASSING run clears the backlog (post half of the gate judges status+result)...
    assert pre(hooks, "terminal", {"command": "python -m pytest -q"}) is None
    post(hooks, "terminal", {"command": "python -m pytest -q"}, result="9 passed in 1.2s")
    assert pre(hooks, "patch", {"path": "c.py"}) is None


def test_failing_verification_does_not_clear_gate(h):
    """Finding 1 regression: a pytest that RUNS but FAILS is not verification."""
    hooks, _ = h
    post(hooks, "write_file", {"path": "a.py"})
    post(hooks, "write_file", {"path": "b.py"})
    assert pre(hooks, "patch", {"path": "c.py"})["action"] == "block"
    # failing run: status ok but FAILED in the output
    post(hooks, "terminal", {"command": "pytest -q"}, result="2 failed, 3 passed in 0.9s")
    still = pre(hooks, "patch", {"path": "c.py"})
    assert still["action"] == "block" and "a.py" in still["message"]
    # errored/timeout runs likewise do not clear
    post(hooks, "terminal", {"command": "pytest -q"}, result="killed", status="timeout")
    assert pre(hooks, "patch", {"path": "c.py"})["action"] == "block"
    # ...and only the passing run clears it
    post(hooks, "terminal", {"command": "pytest -q"}, result="5 passed in 0.4s")
    assert pre(hooks, "patch", {"path": "c.py"}) is None


def test_annotate_on_test_failure(h):
    hooks, _ = h
    post(hooks, "terminal", {"command": "pytest -q"}, result="2 failed, 3 passed", tcid="t9")
    out = hooks["transform_tool_result"](tool_name="terminal", result="2 failed, 3 passed",
                                         session_id="child-1", tool_call_id="t9")
    assert out.endswith("rerun the same command, and do not start new edits elsewhere.")
    assert "[supervisor]" in out


def test_annotate_survives_non_string_result(h):
    """Finding 3 regression: the hook receives the RAW result object — annotations must not be
    silently dropped for dict/list results (dispatcher takes the first str return)."""
    hooks, _ = h
    result = {"output": "1 failed, 2 passed", "exit_code": 1}
    post(hooks, "terminal", {"command": "pytest -q"}, result=result, tcid="td")
    out = hooks["transform_tool_result"](tool_name="terminal", result=result,
                                         session_id="child-1", tool_call_id="td")
    assert isinstance(out, str) and "1 failed" in out and "[supervisor]" in out


def test_steer_then_escalate_on_error_streak(h):
    hooks, _ = h
    for _ in range(6):
        post(hooks, "terminal", {"command": "make"}, result='{"error":"boom"}', status="error")
    assert [s for s, _ in calls["steer"]] == ["sa-0-abc", "sa-orch"]  # steer worker, then escalate to parent


def test_interrupt_and_halt(h):
    hooks, _ = h
    d = pre(hooks, "terminal", {"command": "rm -rf / "})
    assert calls["interrupt"] == ["sa-0-abc"] and d["action"] == "block"
    assert "halted" in pre(hooks, "read_file", {"path": "x"})["message"]


def test_judge_gate_blocks_on_revise(h, monkeypatch):
    hooks, _ = h
    supervisor._ctx.settings["judge"] = dict(SETTINGS["judge"], enabled=True, gate_wait_seconds=2)
    monkeypatch.setattr(supervisor._judge, "review",
                        lambda cfg, goal, hist: {"verdict": "revise", "reason": "edited without tests",
                                                 "instruction": "run pytest tests/test_utils.py"})
    post(hooks, "write_file", {"path": "utils.py"})
    d = pre(hooks, "read_file", {"path": "utils.py"})
    assert d["action"] == "block" and "run pytest tests/test_utils.py" in d["message"]
    assert pre(hooks, "read_file", {"path": "utils.py"}) is None  # verdict consumed once


def test_stale_judge_verdict_is_dropped(h):
    """Finding 2 regression: a verdict describes the history at SUBMISSION time. If the child
    moved on (e.g. the future timed out at the boundary it was meant for), the verdict must be
    dropped, not applied to an unrelated fresh call."""
    hooks, _ = h
    import concurrent.futures as cf
    st = supervisor._children["child-1"]
    fut = cf.Future()
    fut.set_result({"verdict": "revise", "reason": "stale?", "instruction": "irrelevant"})
    st.pending_review = (fut, st.tool_count)
    st.tool_count += 5  # the child ran five more tools since the review was submitted
    assert pre(hooks, "read_file", {"path": "x"}) is None  # stale verdict dropped, call passes
    assert st.pending_review is None


def test_malformed_triggers_do_not_paralyse(h):
    """Finding 4 regression: register() validates triggers; a bad list means 'no triggers',
    never a failing pre_tool_call (which fails CLOSED and would block every child call)."""
    from importlib import import_module
    validate_triggers = supervisor.__dict__["validate_triggers"]
    assert validate_triggers(SETTINGS["triggers"]) == []
    bad = [True, {"when": "post_tool_call", "action": "block"}, {"when": "on", "action": "steer"},
           {"when": "pre_tool_call", "action": "block", "tool": "(", "message": "x"},
           {"when": "pre_tool_call", "action": "block", "args_regex": {"command": "[unclosed"}}]
    errors = validate_triggers(bad)
    assert len(errors) == 5
    # even without validation, evaluate() must skip the broken entry and keep working
    st = ChildState("sa", "s")
    decs = evaluate(bad, "pre_tool_call", st, "write_file", {"path": "/tmp/x"})
    assert decs == []  # bad regex skipped, no exception


def test_register_with_bad_triggers_disables_them(tmp_path):
    s = dict(SETTINGS, log_path=str(tmp_path / "audit.jsonl"),
             triggers=[{"when": "post_tool_call", "action": "block"}])  # block not allowed on post
    ctx = Ctx(s)
    supervisor.register(ctx)
    assert supervisor._triggers() == []
    s2 = dict(SETTINGS, log_path=str(tmp_path / "audit2.jsonl"))  # good config re-registers clean
    supervisor.register(Ctx(s2))
    assert supervisor._triggers() == SETTINGS["triggers"]


def test_stop_writes_audit(h):
    hooks, tmp = h
    hooks["subagent_stop"](child_session_id="child-1", child_status="completed", child_summary="ok")
    kinds = [__import__("json").loads(l)["kind"] for l in (tmp / "audit.jsonl").read_text().splitlines()]
    assert kinds[0] == "start" and kinds[-1] == "stop"
    assert "child-1" not in supervisor._children


def test_max_fires():
    st = ChildState("sa", "s")
    trig = [{"name": "x", "when": "post_tool_call", "action": "steer", "max_fires": 1}]
    assert len(evaluate(trig, "post_tool_call", st, "terminal", {})) == 1
    assert evaluate(trig, "post_tool_call", st, "terminal", {}) == []
