"""PR-A: ctl_step paused/resume (out-of-band preemption, spec §20 race row).

Scheduler-facing invariants:
* every status flip is a compare-and-set under BEGIN IMMEDIATE with read-back —
  a stale preempt lands as ok:false + the STORED status, never a blind overwrite;
* the loop dispatch claims the step in_progress and, after the worker returns,
  trusts the DB READ-BACK (paused) over the worker's exit status;
* a preempt stores a handoff note and the resumed dispatch re-anchors with it;
* paused steps are invisible to dispatch (queue) and keep the task from
  verifying-as-complete; resume_step returns them to the queue.
"""
import pytest

from controller import loop, model, queue as Q


@pytest.fixture()
def env(tmp_path):
    c = model.connect(tmp_path / "c.db")
    wd = tmp_path / "work"; wd.mkdir()
    yield c, str(wd)
    c.close()


def mk(c, wd):
    tid = model.create_task(c, owner="v", workdir=wd, goal="G")
    model.agree_contract(c, tid)
    c.execute("UPDATE ctl_task SET state='ready' WHERE task_id=?", (tid,))
    c.commit()
    return tid


# ------------------------------------------------------------------ CAS model

def test_step_cas_round_trip_and_read_back(env):
    c, wd = env
    sid = model.add_step(c, mk(c, wd), "impl", "code")
    assert model.mark_step_in_progress(c, sid)["ok"] is True
    assert model.get_step(c, sid)["status"] == "in_progress"
    # Stale preempt (already-paused) must NOT overwrite; read-back reports stored.
    r = model.pause_step(c, sid, task_id="t", note="mid file")
    assert r["ok"] is True and r["stored"] == "paused"
    stale = model.pause_step(c, sid, note="again")
    assert stale["ok"] is False and stale["stored"] == "paused"
    bad = model.resume_step(c, sid.replace("step", "stxp"))  # unknown step
    assert bad["ok"] is False and bad["stored"] is None


def test_handoff_note_persisted_and_reloaded(env):
    c, wd = env
    tid = mk(c, wd)
    sid = model.add_step(c, tid, "impl", "code")
    model.mark_step_in_progress(c, sid)
    model.pause_step(c, sid, task_id=tid, note="was editing app.py on tool patch")
    assert model.paused_handoff_note(c, sid) == "was editing app.py on tool patch"
    assert model.resume_step(c, sid)["stored"] == "pending"
    # Resume returns the step to dispatch (queue sees pending only).
    assert Q.next_step(c, tid)["step_id"] == sid


# ------------------------------------------------------------------- loop path

def test_external_preempt_during_worker_lands_paused(env):
    """The scheduler flips in_progress->paused WHILE the worker runs; whatever the
    worker reports afterwards, the loop acts on the read-back: step paused, handoff
    stored, task paused (never done, never failed)."""
    c, wd = env
    tid = mk(c, wd)
    sid = model.add_step(c, tid, "impl", "code")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")

    def worker(run, task, step):
        # out-of-band preempt by another actor, mid-turn:
        r = model.pause_step(c, sid, task_id=tid, note="was running tests")
        assert r["ok"] is True  # claimed in_progress by dispatch -> preempt lands
        return {"status": "finished", "artifacts": [{"v": 1}]}  # worker lies: "finished"

    log = loop.run_cycle(c, scope=wd, worker_fn=worker, review_fn=lambda *a: {"verdict": "pass"})
    assert log[-1]["preempted"] is True
    assert model.get_step(c, sid)["status"] == "paused"
    assert model.get_task(c, tid)["state"] == "paused"
    assert model.paused_handoff_note(c, sid) == "was running tests"
    # A paused step keeps the task out of verifying and out of dispatch:
    assert Q.next_step(c, tid) is None
    # resume returns it to the queue for a later re-anchored dispatch
    assert model.resume_step(c, sid)["stored"] == "pending"


def test_worker_interrupted_status_pauses_step_not_fails_task(env):
    c, wd = env
    tid = mk(c, wd)
    sid = model.add_step(c, tid, "impl", "code")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    worker = lambda r, t, s: {"status": "interrupted", "handoff": "stopped on build"}
    log = loop.run_cycle(c, scope=wd, worker_fn=worker,
                         review_fn=lambda *a: {"verdict": "pass"})
    assert log[-1]["interrupted"] is True
    assert model.get_step(c, sid)["status"] == "paused"
    assert model.get_task(c, tid)["state"] == "paused"
    assert model.paused_handoff_note(c, sid) == "stopped on build"


def test_resume_redispatches_with_handoff_reanchor(env):
    """After resume, the next dispatch re-anchors: worker receives the stored handoff
    in the step payload and the step completes normally."""
    c, wd = env
    tid = mk(c, wd)
    sid = model.add_step(c, tid, "impl", "code")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    model.mark_step_in_progress(c, sid)
    model.pause_step(c, sid, task_id=tid, note="half done: schema written")
    model.resume_step(c, sid)
    c.execute("UPDATE ctl_task SET state='ready' WHERE task_id=?", (tid,))
    c.commit()

    seen = {}

    def worker(run, task, step):
        seen.update(step)
        return {"status": "finished", "artifacts": [{"v": 1}]}

    loop.run_cycle(c, scope=wd, worker_fn=worker, review_fn=lambda *a: {"verdict": "pass"})
    assert seen["handoff"] == "half done: schema written"
    assert model.get_step(c, sid)["status"] == "done"


def test_paused_step_blocks_verifying(env):
    """A task with done+paused steps must park 'paused awaiting resume' rather than
    marching into verifying with unfinished work."""
    c, wd = env
    tid = mk(c, wd)
    s1 = model.add_step(c, tid, "impl", "a")
    s2 = model.add_step(c, tid, "impl", "b")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    loop.run_cycle(c, scope=wd, worker_fn=lambda r, t, s: {"status": "finished", "artifacts": [1]},
                   review_fn=lambda *a: {"verdict": "pass"}, max_steps=1)
    assert model.get_step(c, s1)["status"] == "done"
    model.mark_step_in_progress(c, s2)
    model.pause_step(c, s2, task_id=tid, note="preempted")
    c.execute("UPDATE ctl_task SET state='running' WHERE task_id=?", (tid,))
    c.commit()
    log = loop.run_cycle(c, scope=wd, worker_fn=lambda *a: {"status": "finished"},
                         review_fn=lambda *a: {"verdict": "pass"})
    assert log[-1]["to"] == "paused" and log[-1]["reason"] == "preempted step"
    assert model.get_task(c, tid)["state"] == "paused"


def test_failed_worker_releases_claim(env):
    """Normal worker failure (no preempt) must NOT strand the step in in_progress —
    it is released back to pending so a ready->running retry can pick it."""
    c, wd = env
    tid = mk(c, wd)
    sid = model.add_step(c, tid, "impl", "a")
    model.set_permission(c, wd, enabled=True, granted_via="user-command")
    loop.run_cycle(c, scope=wd, worker_fn=lambda r, t, s: {"status": "failed", "artifacts": []},
                   review_fn=lambda *a: {"verdict": "pass"})
    assert model.get_step(c, sid)["status"] == "pending"
