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


def test_position_head_tail_and_requeue(env):
    """put-first/put-last: create_task(position=) + set_priority via editorial revision."""
    c, wd = env
    a = model.create_task(c, owner="o", workdir=wd, goal="a")
    b = model.create_task(c, owner="o", workdir=wd, goal="b")
    for x in (a, b):
        c.execute("UPDATE ctl_task SET state='ready' WHERE task_id=?", (x,))
    c.commit()
    assert [t["task_id"] for t in Q.eligible(c, wd)] == [a, b]  # FIFO at equal priority
    j = model.create_task(c, owner="o", workdir=wd, goal="j", position="head")
    c.execute("UPDATE ctl_task SET state='ready' WHERE task_id=?", (j,))
    c.commit()
    assert Q.eligible(c, wd)[0]["task_id"] == j
    model.agree_contract(c, b)
    model.set_priority(c, j, head=False)
    assert Q.eligible(c, wd)[-1]["task_id"] == j
    # requeue only touches ready/draft; a running task cannot jump its own queue
    c.execute("UPDATE ctl_task SET state='running' WHERE task_id=?", (a,))
    c.commit()
    try:
        model.set_priority(c, a, head=True); assert False
    except ValueError:
        pass
    # editorial revision over an agreed MATERIAL stays agreed (no re-agreement needed)
    assert model.material_agreed(c, b)


def test_requeue_commands_route_from_user_only():
    from controller import commands
    r = commands.try_route("подними задачу task-x1 в начало", origin="user", sender_ok=True)
    assert r and r["op"] == "head" and r["arg"] == "task-x1"
    r = commands.try_route("опусти задачу task-y2 в конец очереди", origin="user", sender_ok=True)
    assert r and r["op"] == "tail" and r["arg"] == "task-y2"
    assert commands.try_route("подними задачу task-x1 в начало",
                              origin="tool", sender_ok=True) is None  # docs can't requeue


def test_empty_plan_blocks_instead_of_vacuous_complete(env):
    """PR dogfood: task with ZERO steps used to hit completed in 11ms (0<0 False in
    _reviews_current). §46 needs worker-done + review; an empty plan has neither."""
    from controller import loop, model
    c, wd = env
    tid = model.create_task(conn=c, owner="o", workdir=wd, goal="g")
    c.execute("UPDATE ctl_task SET state='ready' WHERE task_id=?", (tid,)); c.commit()
    model.agree_contract(c, tid)
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    log = loop.run_cycle(c, scope=wd, worker_fn=lambda *a: {"status": "finished"},
                         review_fn=lambda *a: {"verdict": "pass"}, max_steps=3)
    task = c.execute("SELECT state FROM ctl_task WHERE task_id=?", (tid,)).fetchone()
    assert task["state"] == "blocked", log
    assert not c.execute("SELECT 1 FROM ctl_run WHERE task_id=?", (tid,)).fetchone()


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


# ---- reselect-after-step (variant A of the queue-preemption decision) --------

def test_reselect_interleaves_by_priority(env):
    """Two fresh equal-priority ready tasks interleave per-step: every selection is
    pure (-priority, updated_at) over ready+running, and each dispatch refreshes the
    winner's claim (ready->running transition), so the next selection turns to the
    other task — a task continues only while nothing outranks it."""
    c, wd = env
    a = mk(c, wd, "A", priority=0)
    b = mk(c, wd, "B", priority=0)
    model.add_step(c, a, "impl", "a1")
    model.add_step(c, a, "impl", "a2")
    model.add_step(c, b, "impl", "b1")
    model.add_step(c, b, "impl", "b2")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    order = []
    loop.run_cycle(c, scope=wd, worker_fn=lambda r, t, s: (order.append(s["title"]),
                                                           ok_worker(r, t, s))[1],
                   review_fn=pass_review)
    # strict round-robin: b never waits for a's whole plan (old code: running-first)
    assert order == ["a1", "b1", "a2", "b2"], order
    for t in (a, b):
        assert model.get_task(c, t)["state"] == "completed"


def test_higher_priority_drains_before_lower(env):
    """With distinct priorities each reselection repeats the -priority winner until its
    plan (and its completion) is exhausted — interleaving only breaks ties."""
    c, wd = env
    a = mk(c, wd, "A", priority=0)
    b = mk(c, wd, "B", priority=5)
    model.add_step(c, a, "impl", "a1")
    model.add_step(c, b, "impl", "b1")
    model.add_step(c, b, "impl", "b2")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    order = []
    loop.run_cycle(c, scope=wd, worker_fn=lambda r, t, s: (order.append(s["title"]),
                                                           ok_worker(r, t, s))[1],
                   review_fn=pass_review)
    assert order == ["b1", "b2", "a1"], order
    assert model.get_task(c, b)["state"] == "completed"
    assert model.get_task(c, a)["state"] == "completed"


def test_head_requeue_overtakes_running_on_next_selection(env):
    """'head' requeue of a READY task beats the RUNNING one on the very next
    selection; the running step itself is never cut short."""
    c, wd = env
    r = mk(c, wd, "R", priority=0)
    model.add_step(c, r, "impl", "r1")
    model.add_step(c, r, "impl", "r2")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    order = []
    worker = lambda run, t, s: (order.append(("run", s["title"])), ok_worker(run, t, s))[1]
    loop.run_cycle(c, scope=wd, worker_fn=worker, review_fn=pass_review, max_steps=1)
    assert order == [("run", "r1")]
    assert model.get_task(c, r)["state"] == "running"
    # operator puts a fresh task at the head of the queue while r is mid-task
    h = model.create_task(c, owner="o", workdir=wd, goal="H", position="head")
    c.execute("UPDATE ctl_task SET state='ready' WHERE task_id=?", (h,)); c.commit()
    model.add_step(c, h, "impl", "h1")
    model.agree_contract(c, h)
    assert Q.eligible(c, wd)[0]["task_id"] == h          # overtakes the running task
    loop.run_cycle(c, scope=wd, worker_fn=worker, review_fn=pass_review, max_steps=1)
    assert order == [("run", "r1"), ("run", "h1")]       # h1 ran before r2
    # head task has no steps left -> one iteration completes it, then r resumes
    loop.run_cycle(c, scope=wd, worker_fn=worker, review_fn=pass_review, max_steps=4)
    assert order == [("run", "r1"), ("run", "h1"), ("run", "r2")]
    assert model.get_task(c, h)["state"] == "completed"
    assert model.get_task(c, r)["state"] == "completed"


def test_task_with_open_run_never_dispatched_twice(env):
    """Resume-in-flight safety: a task whose run row is not terminal yet gets no
    second concurrent run — the loop skips it (others proceed) until it closes."""
    c, wd = env
    p = model.set_permission(c, wd, enabled=True, granted_via="user-command")
    stalled = mk(c, wd, "stalled", priority=9)      # highest rank, external worker busy
    model.add_step(c, stalled, "impl", "s1")
    other = mk(c, wd, "other", priority=0)
    model.add_step(c, other, "impl", "o1")
    # simulate an unfinished run left by an earlier process (crash / external worker)
    rid = model.start_run(c, task_id=stalled, step_id="nonexistent", contract_version=1,
                          execution_epoch=p["execution_epoch"])
    c.execute("UPDATE ctl_task SET state='running' WHERE task_id=?", (stalled,)); c.commit()
    assert Q.has_open_run(c, stalled)

    calls = []
    def counting_worker(run, t, s):
        calls.append(s["title"])
        return ok_worker(run, t, s)
    log = loop.run_cycle(c, scope=wd, worker_fn=counting_worker, review_fn=pass_review)
    # 'stalled' was skipped, 'other' proceeded, and the open run row is untouched
    assert calls == ["o1"], log
    n = c.execute("SELECT COUNT(*) n FROM ctl_run WHERE task_id=?", (stalled,)).fetchone()["n"]
    assert n == 1                                   # no second concurrent run
    assert c.execute("SELECT run_status FROM ctl_run WHERE run_id=?",
                     (rid,)).fetchone()["run_status"] == "starting"
    # when nothing else is serviceable the loop stops cleanly on the open-run filter
    assert log[-1] == {"stop": "all candidates have an open run"}
    # once the run closes, the task is serviceable again
    model.set_run_status(c, rid, "finished")
    assert not Q.has_open_run(c, stalled)
    calls.clear()
    loop.run_cycle(c, scope=wd, worker_fn=counting_worker, review_fn=pass_review)
    assert calls == ["s1"], calls

