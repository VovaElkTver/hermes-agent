"""Increment-2 tests: queue selection + autonomous loop (spec §17-20, §46)."""
import time

import pytest

from controller import loop, model, queue as Q


@pytest.fixture()
def env(tmp_path):
    c = model.connect(tmp_path / "c.db")
    wd = tmp_path / "work"; wd.mkdir()
    yield c, str(wd)
    c.close()


def mk(c, wd, goal="X", deps=None, priority=0, not_before=0):
    tid = model.create_task(c, owner="v", workdir=wd, goal=goal)
    import json
    row = c.execute("SELECT fields FROM ctl_contract_revision WHERE task_id=? AND version=1",
                    (tid,)).fetchone()
    f = json.loads(row["fields"])
    f["priority"] = priority
    f["dependencies"] = list(deps or [])
    if not_before:
        f["start_conditions"] = {"not_before": not_before}
    c.execute("UPDATE ctl_contract_revision SET fields=? WHERE task_id=? AND version=1",
              (json.dumps(f), tid))
    c.commit()
    model.agree_contract(c, tid)
    c.execute("UPDATE ctl_task SET state='ready' WHERE task_id=?", (tid,))
    c.commit()
    return tid


def ok_worker(run, task, step):
    return {"status": "finished", "artifacts": [{"file": step["title"], "v": 1}]}


def pass_review(run, step, arts):
    return {"verdict": "pass"}


def test_queue_order_deps_and_notbefore(env):
    c, wd = env
    low = mk(c, wd, "low", priority=0)
    high = mk(c, wd, "high", priority=5)
    blocked = mk(c, wd, "later", deps=[low])
    later = mk(c, wd, "wait", not_before=time.time() + 3600)
    ids = [t["task_id"] for t in Q.eligible(c, wd)]
    assert ids == [high, low]           # priority first; 'later' waiting on dep; 'wait' on time
    model.add_step(c, low, "impl", "s")
    model.finish_step(c, conn_steps := c.execute(
        "SELECT step_id FROM ctl_step WHERE task_id=?", (low,)).fetchone()["step_id"])
    # dependency is 'completed' only — done steps don't count
    c.execute("UPDATE ctl_task SET state='completed' WHERE task_id=?", (low,)); c.commit()
    assert blocked in [t["task_id"] for t in Q.eligible(c, wd)]


def test_loop_happy_path_completes(env):
    c, wd = env
    tid = mk(c, wd)
    model.add_step(c, tid, "impl", "code")
    model.add_step(c, tid, "tests", "test")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    log = loop.run_cycle(c, scope=wd, worker_fn=ok_worker, review_fn=pass_review)
    assert model.get_task(c, tid)["state"] == "completed"
    assert "completed" in [x.get("to") for x in log]
    assert c.execute("SELECT run_status FROM ctl_run").fetchall()


def test_loop_stops_when_permission_off(env):
    c, wd = env
    tid = mk(c, wd)
    model.add_step(c, tid, "impl", "code")
    # no permission at all -> cycle must stop immediately, task untouched
    log = loop.run_cycle(c, scope=wd, worker_fn=ok_worker, review_fn=pass_review)
    assert log == [{"stop": "permission off"}]
    assert model.get_task(c, tid)["state"] == "ready"


def test_pause_between_selection_and_dispatch_pauses_task(env, monkeypatch):
    c, wd = env
    tid = mk(c, wd)
    model.add_step(c, tid, "impl", "code")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    orig = Q.eligible
    def eligible_once(*a, **k):
        rows = orig(*a, **k)
        if rows:
            model.set_permission(c, wd, enabled=False, granted_via="user-command")  # pause!
        return rows
    monkeypatch.setattr(Q, "eligible", eligible_once)
    monkeypatch.setattr(loop.Q, "eligible", eligible_once)
    log = loop.run_cycle(c, scope=wd, worker_fn=ok_worker, review_fn=pass_review)
    assert model.get_task(c, tid)["state"] == "paused"
    assert any(x.get("to") == "paused" for x in log)


def test_worker_done_without_passing_review_never_completes(env):
    c, wd = env
    tid = mk(c, wd)
    model.add_step(c, tid, "impl", "code")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    fails = iter([{"verdict": "revise", "detail": "no tests"}])
    def review(run, step, arts):
        try:
            return next(fails)
        except StopIteration:
            return {"verdict": "revise", "detail": "still bad"}
    log = loop.run_cycle(c, scope=wd, worker_fn=ok_worker, review_fn=review, max_steps=4)
    assert model.get_task(c, tid)["state"] != "completed"
    assert any("revise" in str(x) for x in log)


def test_needs_human_review_stops_loop(env):
    c, wd = env
    tid = mk(c, wd)
    model.add_step(c, tid, "impl", "code")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    log = loop.run_cycle(c, scope=wd, worker_fn=ok_worker,
                         review_fn=lambda r, s, a: {"verdict": "needs_human", "detail": "??"})
    assert model.get_task(c, tid)["state"] == "needs_human"


def test_failed_run_marks_task_failed(env):
    c, wd = env
    tid = mk(c, wd)
    model.add_step(c, tid, "impl", "code")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    loop.run_cycle(c, scope=wd, worker_fn=lambda r, t, s: {"status": "failed", "artifacts": []},
                   review_fn=pass_review)
    assert model.get_task(c, tid)["state"] == "failed"


def test_step_budget_exhaustion_blocks(env):
    c, wd = env
    tid = mk(c, wd)
    model.add_step(c, tid, "impl", "code")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    c.execute("INSERT INTO ctl_budget_counter VALUES (?, 'steps', 0, 2)", (tid,))
    c.commit()
    loop.run_cycle(c, scope=wd, worker_fn=ok_worker,
                   review_fn=lambda r, s, a: {"verdict": "revise"}, max_steps=10)
    assert model.get_task(c, tid)["state"] == "blocked"


def test_stale_run_finished_does_not_advance(env):
    """§24: epoch-dead events are recorded but the queue must not move on them."""
    from controller import reconcile
    c, wd = env
    tid = mk(c, wd)
    p = model.set_permission(c, wd, enabled=True, granted_via="user-command")
    rid = model.start_run(c, task_id=tid, step_id="s", contract_version=1,
                          execution_epoch=p["execution_epoch"])
    p2 = model.set_permission(c, wd, enabled=True, granted_via="user-command")  # resume bumps
    assert reconcile.stale_event(c, task_id=tid, run_id=rid, perm=p2)
    model.emit(c, kind="run_finished", origin="system", task_id=tid,
               idempotency_key=f"{rid}:f")
    assert model.get_task(c, tid)["state"] == "ready"   # nothing advanced
