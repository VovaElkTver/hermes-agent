"""Agent-facing controller tools: enqueue-is-proposal, step-done-with-read-back,
blocked-via-audited-transition, status shape, registration contract."""
import json

import pytest


@pytest.fixture()
def tools(tmp_path, monkeypatch):
    from controller import model
    conn = model.connect(tmp_path / "state.db")
    import plugins.controller.agent_tools as at
    monkeypatch.setattr(at, "_conn", lambda: conn)
    wd = tmp_path / "work"
    wd.mkdir()
    yield at, conn, model, wd
    conn.close()


def _call(fn, **args):
    return json.loads(fn(args))


def test_enqueue_lands_as_unagreed_draft(tools):
    at, conn, _model, wd = tools
    res = _call(at.ctl_enqueue, goal="ship X", workdir=str(wd),
                steps=[{"kind": "implementation", "title": "code"}, {"title": "test"}],
                acceptance=["tests green"], dependencies=[])
    assert res["ok"] and res["state"] == "draft"
    row = conn.execute("SELECT state FROM ctl_task WHERE task_id=?",
                       (res["task_id"],)).fetchone()
    assert row["state"] == "draft"
    agree = conn.execute(
        "SELECT change_class, agreed_at FROM ctl_contract_revision WHERE task_id=?"
        " ORDER BY version DESC LIMIT 1", (res["task_id"],)).fetchone()
    assert agree["change_class"] == "MATERIAL" and agree["agreed_at"] is None
    contract = conn.execute(
        "SELECT fields FROM ctl_contract_revision WHERE task_id=? ORDER BY version DESC"
        " LIMIT 1", (res["task_id"],)).fetchone()
    assert json.loads(contract["fields"])["acceptance"] == ["tests green"]
    assert conn.execute("SELECT COUNT(*) c FROM ctl_step WHERE task_id=?",
                        (res["task_id"],)).fetchone()["c"] == 2


def test_enqueue_rejects_unknown_dependency_without_orphan(tools):
    at, conn, _model, wd = tools
    before = conn.execute("SELECT COUNT(*) c FROM ctl_task").fetchone()["c"]
    res = _call(at.ctl_enqueue, goal="x", workdir=str(wd),
                dependencies=["task_nope"])
    assert not res["ok"] and "task_nope" in res["error"]
    after = conn.execute("SELECT COUNT(*) c FROM ctl_task").fetchone()["c"]
    assert after == before


def test_enqueue_requires_existing_workdir(tools):
    at, _conn, _model, wd = tools
    res = _call(at.ctl_enqueue, goal="x", workdir=str(wd / "nope"))
    assert not res["ok"] and "does not exist" in res["error"]


def test_status_lists_and_drills(tools):
    at, _conn, _model, wd = tools
    tid = _call(at.ctl_enqueue, goal="g", workdir=str(wd),
                steps=[{"title": "s1"}])["task_id"]
    lst = _call(at.ctl_status)
    assert lst["ok"] and any(t["task_id"] == tid for t in lst["tasks"])
    one = _call(at.ctl_status, task_id=tid)
    assert one["task"]["state"] == "draft" and len(one["steps"]) == 1
    assert "permission" in one
    missing = _call(at.ctl_status, task_id="task_absent")
    assert not missing["ok"]


def test_step_done_read_back(tools):
    at, conn, model, wd = tools
    tid = model.create_task(conn, owner="v", workdir=str(wd), goal="g")
    sid = model.add_step(conn, tid, "implementation", "code")
    res = _call(at.ctl_step_done, step_id=sid)
    assert res["ok"] and res["status"] == "done" and res["task_id"] == tid
    stored = conn.execute("SELECT status FROM ctl_step WHERE step_id=?",
                          (sid,)).fetchone()["status"]
    assert stored == "done"
    assert not _call(at.ctl_step_done, step_id="step_absent")["ok"]
    assert not _call(at.ctl_step_done, step_id=sid, status="bogus")["ok"]


def test_block_uses_audited_transition(tools):
    at, conn, model, wd = tools
    tid = model.create_task(conn, owner="v", workdir=str(wd), goal="g")
    model.agree_contract(conn, tid)
    transitions_to_ready = _call(at.ctl_block, task_id=tid, reason="no docs")
    assert not transitions_to_ready["ok"]  # draft -> blocked is illegal
    p = model.set_permission(conn, str(wd), enabled=True, granted_via="user-command")
    from controller import transitions
    transitions.transition(conn, tid, "ready", basis="go")
    transitions.transition(conn, tid, "running", basis="go", permission=p)
    res = _call(at.ctl_block, task_id=tid, reason="missing spec")
    assert res["ok"] and res["to"] == "blocked"
    audit = conn.execute(
        "SELECT from_state, to_state FROM ctl_state_transition WHERE task_id=?",
        (tid,)).fetchall()
    assert [dict(r) for r in audit][-1] == {"from_state": "running",
                                            "to_state": "blocked"}
    ev = conn.execute("SELECT origin, payload FROM ctl_event WHERE task_id=?"
                      " AND kind='agent_block'", (tid,)).fetchone()
    assert json.loads(ev["payload"])["reason"] == "missing spec"


def test_tool_defs_shape():
    import plugins.controller.agent_tools as at
    defs = at.tool_defs()
    assert {name for name, _s, _h in defs} == {
        "ctl_status", "ctl_enqueue", "ctl_step_done", "ctl_block"}
    for name, schema, handler in defs:
        assert schema["name"] == name and callable(handler)
        assert schema["parameters"]["type"] == "object"
        if name == "ctl_enqueue":
            assert set(schema["parameters"]["required"]) == {"goal", "workdir"}


def test_status_snapshot_truncation_and_as_of(tools):
    at, _conn, _model, wd = tools
    for i in range(52):
        _call(at.ctl_enqueue, goal=f"g{i}", workdir=str(wd))
    snap = _call(at.ctl_status)
    assert snap["ok"] and len(snap["tasks"]) == 50 and snap["has_more"] is True
    assert snap["as_of"].endswith("Z")
    # instruction tool: no page cursors in the surface at all
    props = next(s["parameters"]["properties"]
                 for n, s, _h in at.tool_defs() if n == "ctl_status")
    assert set(props) == {"task_id", "workdir"}
    few = _call(at.ctl_status, workdir=str(wd) + "/other")
    assert few["ok"] and few["tasks"] == [] and few["has_more"] is False


def test_step_done_precondition(tools):
    at, conn, model, wd = tools
    tid = model.create_task(conn, owner="v", workdir=str(wd), goal="g")
    sid = model.add_step(conn, tid, "implementation", "code")
    assert _call(at.ctl_step_done, step_id=sid)["ok"]
    again = _call(at.ctl_step_done, step_id=sid)
    assert not again["ok"] and again["stored"] == "done" and again["expected"] == "pending"
    assert "precondition failed" in again["error"] and "ctl_status" in again["error"]
    assert conn.execute("SELECT status FROM ctl_step WHERE step_id=?",
                        (sid,)).fetchone()["status"] == "done"
    bad = _call(at.ctl_step_done, step_id=sid, expected_status="pendig")
    assert not bad["ok"] and "pending, done, blocked, failed" in bad["error"]
    forced = _call(at.ctl_step_done, step_id=sid, expected_status="done")
    assert forced["ok"] and forced["status"] == "done"


def test_read_only_declaration_semantics():
    import plugins.controller.agent_tools as at
    assert at.is_read_only("ctl_status") is True
    assert at.is_read_only("ctl_enqueue") is False
    assert at.is_read_only("some_other_tool") is False


def test_barrier_read_only_passthrough(tools, monkeypatch):
    """When the dispatch barrier blocks, declared read-only tools still pass —
    TOOL_META has a real consumer (controller hook), not just tests."""
    import importlib.util
    _at, conn, _model, _wd = tools
    spec = importlib.util.spec_from_file_location(
        "ctl_plugin_under_test", "plugins/controller/__init__.py",
        submodule_search_locations=["plugins/controller"])
    plug = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = plug
    spec.loader.exec_module(plug)
    from controller import barrier
    monkeypatch.setattr(plug, "_enabled", lambda: True)
    monkeypatch.setattr(plug, "_origin_of", lambda *a, **k: "autonomous")
    monkeypatch.setattr(plug, "_db", lambda: conn)
    monkeypatch.setattr(barrier, "check", lambda *a, **k: (False, "epoch stale"))
    assert plug._on_pre_tool_call("ctl_status", task_id="", session_id="s1") is None
    blocked = plug._on_pre_tool_call("terminal", task_id="", session_id="s1")
    assert blocked["action"] == "block" and "epoch stale" in blocked["message"]
