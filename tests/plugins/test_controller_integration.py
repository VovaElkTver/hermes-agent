"""Integration tests for the controller MVP in the fork context.

Covers what the standalone repo proved (transitions/barrier/commands/reconcile) plus
fork-specific wiring: DDL comes from the canonical SCHEMA_SQL authority, no second shape.
"""
import importlib

import pytest


@pytest.fixture()
def ctl(tmp_path):
    from controller import model, transitions  # package importable from repo root
    conn = model.connect(tmp_path / "state.db")
    yield conn, model, transitions
    conn.close()


def test_schema_authority_is_hermes_state_common(ctl):
    """controller.connect(temp) must build ctl_* from hermes_state_common.SCHEMA_SQL only."""
    conn, model, _ = ctl
    names = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'ctl_%'")}
    assert {"ctl_task", "ctl_run", "ctl_event", "ctl_autonomy_permission"} <= names
    import controller.model as m
    import inspect
    assert "SCHEMA_SQL" in inspect.getsource(m.connect)


def test_full_lifecycle_and_race(ctl, tmp_path):
    conn, model, transitions = ctl
    wd = tmp_path / "work"; wd.mkdir()
    tid = model.create_task(conn, owner="v", workdir=str(wd), goal="X")
    st = model.add_step(conn, tid, "implementation", "code")
    model.agree_contract(conn, tid)
    transitions.transition(conn, tid, "needs_clarification", basis="d")
    transitions.transition(conn, tid, "ready", basis="r")
    p = model.set_permission(conn, str(wd), enabled=True, granted_via="user-command")
    transitions.transition(conn, tid, "running", basis="go", permission=p)
    rid = model.start_run(conn, task_id=tid, step_id=st, contract_version=1,
                          execution_epoch=p["execution_epoch"])
    # pause between choice and dispatch
    model.set_permission(conn, str(wd), enabled=False, granted_via="user-command")
    from controller import barrier
    ok, why = barrier.check(conn, scope=str(wd), origin="autonomous", task_id=tid)
    assert not ok
    # duplicate event absorbed
    assert model.emit(conn, kind="run_finished", origin="system", task_id=tid,
                      idempotency_key=f"{rid}:f")
    assert model.emit(conn, kind="run_finished", origin="system", task_id=tid,
                      idempotency_key=f"{rid}:f") is None
    # terminal lock
    transitions.transition(conn, tid, "cancelled", basis="cancel")
    with pytest.raises(ValueError):
        transitions.transition(conn, tid, "ready", basis="stray")


def test_commands_doc_and_restart(ctl, tmp_path):
    conn, model, _ = ctl
    from controller import commands
    assert commands.try_route("возобнови автономность", origin="doc", sender_ok=True) is None
    op = commands.try_route("работай автономно", origin="user", sender_ok=True)
    assert op["op"] == "enable"
    r = commands.apply(conn, op, scope="/w")
    assert r["ok"] and r["permission"]["enabled"]
    model.set_permission(conn, "/w", enabled=True, granted_via="user-command")
    model.mark_restart_intent(conn, "operator")
    assert commands.on_restart(conn, "/w")["mode"] == "interactive"
    # crash path: no marker -> autonomous with epoch bump
    p1 = model.get_permission(conn, "/w")
    out = commands.on_restart(conn, "/w")
    assert out["mode"] == "autonomous"
    assert out["permission"]["execution_epoch"] > p1["execution_epoch"]


def test_reconcile_no_blind_retry(ctl):
    conn, model, transitions = ctl
    from controller import reconcile
    rid = model.start_run(conn, task_id="t", step_id="s", contract_version=1, execution_epoch=1)
    conn.execute("INSERT INTO ctl_task VALUES ('t','o','','/w','g','running',1,NULL,0,0)")
    rep = reconcile.recover(conn, verify_fn=lambda r: "finished")
    assert rep[0]["action"] == "reconciled"
    assert conn.execute("SELECT run_status FROM ctl_run WHERE run_id=?",
                        (rid,)).fetchone()["run_status"] == "finished"


def test_plugin_registers_off_and_flag_gates(tmp_path, monkeypatch):
    """plugins/controller loads; with flag off the hooks are inert (no DB touch)."""
    import sys, types
    mod = importlib.import_module("plugins.controller") if False else None
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "controller_plugin_under_test",
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "plugins" / "controller" / "__init__.py")
    plug = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plug
    spec.loader.exec_module(plug)

    reg = {}

    class Ctx:
        def register_hook(self, name, cb):
            reg.setdefault(name, []).append(cb)
        def get_config(self, key, default=None):
            return False  # flag OFF

    plug.register(Ctx())
    assert "pre_tool_call" in reg and "post_tool_call" in reg
    assert "pre_llm_call" not in reg  # command routing lives in the core router, not the plugin
    out2 = [cb(tool_name="terminal", args={"command": "ls"}, task_id="k",
               session_id="s") for cb in reg["pre_tool_call"]]
    assert out2 == [None]
