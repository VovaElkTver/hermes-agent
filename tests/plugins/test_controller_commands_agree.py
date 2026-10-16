"""controller.commands 'agree' — the deterministic human gate for queued drafts.
ctl_enqueue parks tasks in draft; only a routed user command carrying an explicit
task id may agree the MATERIAL contract and move the task to ready."""
import pytest


@pytest.fixture()
def cdc(tmp_path):
    from controller import model
    conn = model.connect(tmp_path / "state.db")
    yield conn, model
    conn.close()


def _task(model, conn, wd="/w"):
    return model.create_task(conn, owner="v", workdir=wd, goal="g")


def test_routing_requires_task_id():
    from controller import commands
    assert commands.try_route("го", origin="user", sender_ok=True) is None
    assert commands.try_route("иди", origin="user", sender_ok=True) is None
    assert commands.try_route("иди task-ab12cd", origin="user", sender_ok=True) \
        == {"op": "agree", "arg": "task-ab12cd", "text": "иди task-ab12cd"}
    m = commands.try_route("dispatch task-x1 task-y2", origin="user", sender_ok=True)
    assert m["op"] == "agree" and m["arg"] == "task-x1"
    assert commands.try_route("иди task-q1", origin="tool", sender_ok=True) is None


def test_agree_draft_to_ready_with_stamp(cdc):
    from controller import commands
    conn, model = cdc
    tid = _task(model, conn)
    assert not model.material_agreed(conn, tid)
    res = commands.apply(conn, {"op": "agree", "arg": tid, "text": ""}, scope="/w")
    assert res["ok"] and res["to"] == "ready" and res["agreed"] is True
    assert conn.execute("SELECT state FROM ctl_task WHERE task_id=?",
                        (tid,)).fetchone()["state"] == "ready"


def test_agree_is_idempotent_on_ready(cdc):
    from controller import commands
    conn, model = cdc
    tid = _task(model, conn)
    commands.apply(conn, {"op": "agree", "arg": tid, "text": ""}, scope="/w")
    again = commands.apply(conn, {"op": "agree", "arg": tid, "text": ""}, scope="/w")
    assert again["ok"] and again["note"] == "already ready" and again["state"] == "ready"


def test_agree_refuses_wrong_states(cdc):
    from controller import commands
    conn, model = cdc
    bad = commands.apply(conn, {"op": "agree", "arg": "task-nope", "text": ""}, scope="/w")
    assert not bad["ok"] and "unknown" in bad["error"]
    tid = _task(model, conn)
    model.agree_contract(conn, tid)
    conn.execute("UPDATE ctl_task SET state='completed' WHERE task_id=?", (tid,))
    res = commands.apply(conn, {"op": "agree", "arg": tid, "text": ""}, scope="/w")
    assert not res["ok"] and "completed" in res["error"]


def test_new_material_revision_invalidates_prior_agree(cdc):
    from controller import commands
    conn, model = cdc
    tid = _task(model, conn)
    commands.apply(conn, {"op": "agree", "arg": tid, "text": ""}, scope="/w")
    model.revise_contract(conn, tid, {"goal": "g2", "acceptance": ["x"]}, "MATERIAL")
    assert not model.material_agreed(conn, tid)
    # re-agree needs a fresh explicit command, not the old stamp
    res = commands.apply(conn, {"op": "agree", "arg": tid, "text": ""}, scope="/w")
    assert res["ok"] and res["agreed"] is True
