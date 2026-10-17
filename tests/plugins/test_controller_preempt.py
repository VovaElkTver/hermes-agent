"""PR-B: preempt request path + cooperative worker cut (out-of-band preemption).

Complements PR-A's CAS/loop tests (test_controller_step_pause.py) and verb tests
(tests/gateway/test_control_socket_session_verbs.py) with the scheduler-facing
pieces:

* request_preempt unstages the in_progress step synchronously (paused + handoff,
  CAS read-back) and flags the open run interrupt_requested;
* path selection: control-socket verb when the gateway answers, durable db-flag
  fallback when it doesn't (old gateway -> client returns None);
* attribution: the preempt event carries the SYSTEM reason slug, never a user stop;
* worker cooperation: the real subprocess is tree-killed mid-run when the flag
  lands (no waiting for the turn to finish), and the result carries the handoff;
* brief re-anchoring: a resumed step gets the stored handoff line in its brief,
  a fresh step does not.
"""
import json
import os
import stat
import tempfile
import threading

import pytest

from controller import model, preempt, workers


@pytest.fixture()
def env(tmp_path):
    c = model.connect(tmp_path / "c.db")
    wd = tmp_path / "work"; wd.mkdir()
    yield c, str(wd), tmp_path
    c.close()


def _running_task(c, wd):
    """Task claimed into running with a dispatch that marked its step in_progress."""
    tid = model.create_task(c, owner="v", workdir=wd, goal="G")
    model.agree_contract(c, tid)
    c.execute("UPDATE ctl_task SET state='running' WHERE task_id=?", (tid,))
    c.commit()
    sid = model.add_step(c, tid, "impl", "code")
    assert model.mark_step_in_progress(c, sid)["ok"] is True
    rid = model.start_run(c, task_id=tid, step_id=sid, contract_version=1,
                          execution_epoch=1)
    model.set_run_status(c, rid, "running")
    return tid, sid, rid


def _last_preempt_event(c):
    row = c.execute("SELECT payload FROM ctl_event WHERE kind='preempt_requested'"
                    " ORDER BY ts DESC LIMIT 1").fetchone()
    assert row is not None
    return json.loads(row["payload"])


# ----------------------------------------------------------- request_preempt

def test_preempt_db_flag_unstages_step_and_flags_run(env):
    c, wd, _ = env
    tid, sid, rid = _running_task(c, wd)
    r = preempt.request_preempt(c, tid, reason="controller_preemption",
                                handoff="was running pytest on terminal")
    assert r["ok"] is True and r["via"] == "db_flag"
    assert model.get_step(c, sid)["status"] == "paused"          # read-back state
    assert model.paused_handoff_note(c, sid) == "was running pytest on terminal"
    row = c.execute("SELECT run_status FROM ctl_run WHERE run_id=?", (rid,)).fetchone()
    assert row["run_status"] == "interrupt_requested"
    payload = _last_preempt_event(c)
    assert payload["reason"] == "controller_preemption"          # SYSTEM attribution
    assert payload["via"] == "db_flag" and sid in payload["steps_paused"]


def test_preempt_uses_control_socket_when_gateway_answers(env, monkeypatch):
    c, wd, _ = env
    tid, sid, _rid = _running_task(c, wd)
    seen = {}

    def fake_interrupt(home, session, *, reason="controller_preemption",
                       message=None, hard=False, timeout=5.0):
        seen.update(home=str(home), session=session, reason=reason)
        return {"interrupted": True, "session": session,
                "tool_reason": reason, "issuer": reason,
                "handoff": "was doing: editing x on tool patch"}

    monkeypatch.setattr("gateway.control_socket.interrupt_gateway_session",
                        fake_interrupt)
    r = preempt.request_preempt(c, tid, session="matrix:!room1", home="/tmp/h",
                                handoff="switching to head task")
    assert r["ok"] is True and r["via"] == "control_socket"
    assert seen["session"] == "matrix:!room1"
    assert seen["reason"] == "controller_preemption"
    # the step is still unstaged synchronously on the live path too
    assert model.get_step(c, sid)["status"] == "paused"


def test_preempt_falls_back_when_gateway_lacks_verbs(env, monkeypatch):
    """Old gateway (старый гейтвей): the client answers None (no socket / unknown
    verb) — the preempt degrades to the durable flag path, never an error."""
    c, wd, _ = env
    tid, sid, rid = _running_task(c, wd)
    monkeypatch.setattr("gateway.control_socket.interrupt_gateway_session",
                        lambda *a, **k: None)
    r = preempt.request_preempt(c, tid, session="telegram:1", home="/tmp/h")
    assert r["ok"] is True and r["via"] == "db_flag"
    assert r["gateway"] is None
    assert model.get_step(c, sid)["status"] == "paused"
    row = c.execute("SELECT run_status FROM ctl_run WHERE run_id=?", (rid,)).fetchone()
    assert row["run_status"] == "interrupt_requested"


def test_preempt_idle_task_is_a_noop(env):
    c, wd, _ = env
    tid = model.create_task(c, owner="v", workdir=wd, goal="G")
    r = preempt.request_preempt(c, tid)
    assert r["ok"] is False and r["steps"] == []
    assert preempt.request_preempt(c, "task-nope")["ok"] is False


# ------------------------------------------------------------- worker signal

def test_preempt_signal_reads_both_durable_signals(env):
    c, wd, _ = env
    tid, sid, rid = _running_task(c, wd)
    assert workers._preempt_signal(c, run_id=rid, step_id=sid) is False
    model.set_run_status(c, rid, "interrupt_requested")
    assert workers._preempt_signal(c, run_id=rid, step_id=sid) is True
    model.set_run_status(c, rid, "running")
    model.pause_step(c, sid, task_id=tid, note="x")
    assert workers._preempt_signal(c, run_id=rid, step_id=sid) is True
    # unknown rows: fail open (keep running), never raise
    assert workers._preempt_signal(c, run_id="run-none", step_id=None) is False


def _write_stub(tmp_path):
    """Executable stand-in for `hermes`: prints one line then blocks like a tool
    that never returns. Lives OUTSIDE the workdir so a tree-kill can't orphan it."""
    fd, p = tempfile.mkstemp(prefix="stub-hermes-", suffix=".sh", dir=str(tmp_path))
    with os.fdopen(fd, "w") as fh:
        fh.write("#!/bin/sh\necho partial-output\nsleep 60\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP)
    return p


def test_worker_subprocess_cut_mid_run_without_finishing_turn(env):
    """The acceptance core: a REAL worker subprocess sitting on a tool (sleep) is
    tree-killed when the preempt lands — the pump does NOT wait out the turn — and
    the result says interrupted and carries the stored handoff."""
    c, wd, tmp_path = env
    tid, sid, rid = _running_task(c, wd)
    task_row = model.get_task(c, tid)
    step_row = model.get_step(c, sid)
    assert task_row is not None and step_row is not None
    box = {}
    stub = _write_stub(tmp_path)

    def worker_on_own_conn():
        # Production truth: the pump waits on its OWN connection (the worker runs in
        # a separate process from whoever flips the flag); sqlite conns are thread-bound.
        wconn = model.connect(tmp_path / "c.db")
        try:
            w = workers.make_worker(wconn, timeout_s=120, hermes_bin=stub,
                                    preempt_poll_s=0.2)
            box["res"] = w({"run_id": rid}, dict(task_row), dict(step_row))
        finally:
            wconn.close()

    th = threading.Thread(target=worker_on_own_conn, daemon=True)
    th.start()
    # the worker is now mid-turn; preempt out-of-band:
    r = preempt.request_preempt(c, tid, handoff="was on tool terminal")
    assert r["ok"] is True
    th.join(timeout=30)
    assert not th.is_alive(), "worker never returned after preempt"
    res = box["res"]
    assert res["status"] == "interrupted"
    assert "was on tool terminal" in res["handoff"]
    assert "partial-output" in res["artifacts"][0]["stdout_tail"]
    assert model.get_step(c, sid)["status"] == "paused"


# ---------------------------------------------------------------- brief text

def test_brief_reanchors_resumed_step_with_handoff(env):
    c, wd, _ = env
    tid = model.create_task(c, owner="v", workdir=wd, goal="G")
    sid = model.add_step(c, tid, "impl", "code")
    fresh = workers._brief(c, {"task_id": tid, "goal": "G"},
                           {"kind": "impl", "title": "code"})
    assert "RESUME" not in fresh
    model.mark_step_in_progress(c, sid)
    model.pause_step(c, sid, task_id=tid, note="was on tool patch; schema written")
    resumed = workers._brief(c, {"task_id": tid, "goal": "G"},
                             {"kind": "impl", "title": "code",
                              "handoff": model.paused_handoff_note(c, sid)})
    assert "RESUME" in resumed
    assert "was on tool patch; schema written" in resumed
    assert "re-anchor" in resumed.lower()
