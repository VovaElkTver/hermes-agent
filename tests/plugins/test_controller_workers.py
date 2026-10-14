"""Increment-3 tests: worker/review bindings with a fake hermes binary (offline)."""
import stat
import textwrap

import pytest

from controller import model
from controller.workers import make_review, make_worker


@pytest.fixture()
def env(tmp_path):
    c = model.connect(tmp_path / "c.db")
    wd = tmp_path / "work"; wd.mkdir()
    fake = tmp_path / "hermes"
    fake.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "WORKED $*"
        exit 0
        """))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    yield c, str(wd), str(fake)
    c.close()


def _task(c, wd):
    tid = model.create_task(c, owner="v", workdir=wd, goal="X")
    model.agree_contract(c, tid)
    st = model.add_step(c, tid, "impl", "code")
    c.execute("UPDATE ctl_task SET state='running' WHERE task_id=?", (tid,)); c.commit()
    return tid, st


def test_worker_runs_fake_bin_and_reports_artifacts(env):
    c, wd, fake = env
    tid, st = _task(c, wd)
    w = make_worker(c, hermes_bin=fake, timeout_s=60)
    res = w({"run_id": "r"}, {"task_id": tid, "workdir": wd, "goal": "X"},
            {"step_id": st, "kind": "impl", "title": "code"})
    assert res["status"] == "finished"
    assert "WORKED" in res["artifacts"][0]["stdout_tail"]
    assert res["artifacts"][0]["returncode"] == 0


def test_worker_passes_in_workdir_regression(env):
    """PR#13 review finding: bug 1 regressed silently — the fake bin must SEE --in <wd>."""
    c, wd, fake = env
    tid, st = _task(c, wd)
    res = make_worker(c, hermes_bin=fake, timeout_s=60)(
        {"run_id": "r"}, {"task_id": tid, "workdir": wd, "goal": "X"},
        {"step_id": st, "kind": "impl", "title": "code"})
    tail = res["artifacts"][0]["stdout_tail"]
    toks = tail.split()
    assert "--in" in toks, "--in flag missing from worker argv"
    assert wd in toks, "--in must receive the task workdir"


def test_workers_history_shape_reaches_http(env, monkeypatch):
    """PR#13 follow-up: make_review's history items lacked 'tool' -> build_messages
    KeyError failed every LIVE judge call to needs_human regardless of endpoint state."""
    import plugins.supervisor.judge as j
    seen = {}
    def fake(cfg, goal, history):
        seen['history'] = history
        return {"verdict": "pass", "reason": "fine", "instruction": "", "degraded": False}
    monkeypatch.setattr(j, "review", fake)
    c, _wd, _ = env
    rv = make_review(c, judge_cfg={"model": "x"})
    out = rv({"run_id": "r"}, {"step_id": "s", "title": "do"},
             [{"stdout_tail": "tail", "returncode": 0}])
    assert out["verdict"] == "pass"
    assert seen["history"][0]["tool"] == "worker" and "tail" in seen["history"][0]["result"]


def test_review_fail_closed_on_reasoning_truncation(env):
    """Dogfood 12.10: reasoning judge on 8012 spent max_tokens on hidden reasoning ->
    finish_reason='length', content='' -> empty-completion degraded must fire (was the
    silent needs_human loop that stalled both live tasks)."""
    import json, urllib.request
    _c, _wd, _ = env

    class Resp:
        def read(self): return json.dumps(
            {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: Resp()
    try:
        from plugins.supervisor.judge import review as jr
        v = jr({"base_url": "http://x", "model": "m"}, "g",
               [{"tool": "w", "status": "0", "args": "a", "result": "r"}])
        assert v["verdict"] == "pass" and v["degraded"] is True
    finally:
        urllib.request.urlopen = orig


def test_worker_nonzero_exit_is_failed(env, tmp_path):
    c, wd, _ = env
    bad = tmp_path / "hermes-bad"
    bad.write_text("#!/bin/sh\nexit 3\n")
    bad.chmod(bad.stat().st_mode | stat.S_IEXEC)
    tid, st = _task(c, wd)
    res = make_worker(c, hermes_bin=str(bad), timeout_s=60)(
        {"run_id": "r"}, {"task_id": tid, "workdir": wd, "goal": "X"},
        {"step_id": st, "kind": "impl", "title": "x"})
    assert res["status"] == "failed"


def test_review_without_judge_is_needs_human(env):
    c, _wd, _ = env
    rv = make_review(c, judge_cfg=None)
    assert rv({"run_id": "r"}, {"step_id": "s"}, [])["verdict"] == "needs_human"


def test_review_fail_closed_on_judge_error(env, monkeypatch):
    c, _wd, _ = env
    import plugins.supervisor.judge as j
    def boom(*a, **k):
        raise RuntimeError("judge down")
    monkeypatch.setattr(j, "review", boom)
    rv = make_review(c, judge_cfg={"model": "x"})
    out = rv({"run_id": "r"}, {"step_id": "s"}, [{"stdout_tail": "ok", "returncode": 0}])
    assert out["verdict"] == "needs_human" and "unavailable" in out["detail"]


def test_review_fail_closed_on_failopen_judge_pass(env, monkeypatch):
    """judge.review fails OPEN for checkpoints; completion must not inherit that."""
    c, _wd, _ = env
    import plugins.supervisor.judge as j
    monkeypatch.setattr(j, "review", lambda *a, **k: {
        "verdict": "pass", "reason": "judge unavailable: KeyError", "instruction": ""})
    rv = make_review(c, judge_cfg={"model": "x"})
    out = rv({"run_id": "r"}, {"step_id": "s"}, [{"stdout_tail": "ok", "returncode": 0}])
    assert out["verdict"] == "needs_human"


def test_review_fail_closed_on_empty_completion(env):
    """Review finding hole A: HTTP alive but completion unparsable -> judge.py defaults
    to pass; degraded flag must catch it (empty/garbage content, missing field)."""
    import json, urllib.request
    c, _wd, _ = env

    class Resp:
        def __init__(self, payload): self._p = payload
        def read(self): return json.dumps(self._p).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    calls = iter([{"choices": [{"message": {"content": ""}}]},
                  {"choices": [{"message": {"content": ""}}]}])
    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: Resp(next(calls))
    try:
        from plugins.supervisor.judge import review as jr
        v = jr({"base_url": "http://x", "model": "m"}, "g", [])
        assert v["verdict"] == "pass" and v["degraded"] is True
        rv = make_review(c, judge_cfg={"base_url": "http://x"})
        out = rv({"run_id": "r"}, {"step_id": "s", "title": "t"},
                 [{"stdout_tail": "x", "returncode": 0}])
        # must be degraded-flag path, NOT the exception path (KeyError regression)
        assert out["verdict"] == "needs_human" and "judge unavailable" not in out["detail"]
    finally:
        urllib.request.urlopen = orig


def test_review_gate_rejects_pass_when_worker_failed(env, monkeypatch):
    c, _wd, _ = env
    import plugins.supervisor.judge as j
    monkeypatch.setattr(j, "review", lambda *a, **k: {"verdict": "pass", "reason": "lgtm"})
    rv = make_review(c, judge_cfg={"model": "x"})
    out = rv({"run_id": "r"}, {"step_id": "s"}, [{"stdout_tail": "", "returncode": 2}])
    assert out["verdict"] == "revise"          # code gate beats judge


def test_pump_cli_permission_off_noop(env, monkeypatch):
    from controller import pump_cli
    # pump_cli.main uses model.connect() -> point it at our temp db
    monkeypatch.setattr(model, "connect", lambda db_path=None: env[0])
    out = pump_cli.main(["--scope", "/nonexistent-scope"])
    assert out == 0


# --- Windows-native timeout kill (audit of PR8-16) -------------------------
# os.killpg/getpgid do not exist on Windows (AttributeError escaped the except)
# and start_new_session is silently ignored by CPython there, so a timed-out
# worker left orphaned grandchildren writing into the task workdir.

def test_spawn_kwargs_posix_uses_start_new_session(monkeypatch):
    import os as _os
    from controller import workers
    monkeypatch.setattr(_os, "name", "posix")
    kw = workers._spawn_kwargs()
    assert kw == {"start_new_session": True}


def test_spawn_kwargs_nt_uses_creationflags(monkeypatch):
    import os as _os
    from controller import workers
    monkeypatch.setattr(_os, "name", "nt")
    kw = workers._spawn_kwargs()
    assert "start_new_session" not in kw, "CPython ignores it on Windows"
    assert kw["creationflags"] == (workers.CREATE_NEW_PROCESS_GROUP
                                   | workers.DETACHED_PROCESS)


def test_kill_tree_nt_uses_taskkill_and_never_touches_killpg(monkeypatch):
    import os as _os
    from controller import workers

    class FakeProc:
        pid = 4242

    runs = []
    def fake_run(cmd, **kw):
        runs.append((cmd, kw))
    monkeypatch.setattr(_os, "name", "nt")
    monkeypatch.setattr(workers.subprocess, "run", fake_run)
    # If the nt branch reached for killpg/getpgid, these recorders fire.
    pg = []
    monkeypatch.setattr(_os, "killpg", lambda *a: pg.append(a), raising=False)
    monkeypatch.setattr(_os, "getpgid", lambda p: pg.append(("getpgid", p)), raising=False)

    workers._kill_tree(FakeProc())
    assert pg == [], "nt path must not reference os.killpg/getpgid"
    cmd, kw = runs[0]
    assert cmd == ["taskkill", "/T", "/F", "/PID", "4242"]
    assert kw.get("capture_output") is True and kw.get("timeout") == 15


def test_kill_tree_nt_survives_taskkill_failure(monkeypatch):
    import os as _os
    from controller import workers

    class FakeProc:
        pid = 1

    def boom(*a, **k):
        raise OSError("taskkill missing")
    monkeypatch.setattr(_os, "name", "nt")
    monkeypatch.setattr(workers.subprocess, "run", boom)
    workers._kill_tree(FakeProc())  # must not raise


def test_kill_tree_posix_uses_killpg(monkeypatch):
    import os as _os
    import signal as _signal
    from controller import workers

    class FakeProc:
        pid = 4242

    calls = []
    monkeypatch.setattr(_os, "name", "posix")
    monkeypatch.setattr(_os, "getpgid", lambda pid: pid * 10)
    monkeypatch.setattr(_os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    workers._kill_tree(FakeProc())
    assert calls == [(42420, _signal.SIGKILL)]


def test_kill_tree_posix_missing_killpg_is_swallowed(monkeypatch):
    """Regression: AttributeError (Windows-style missing primitive) must not
    escape the defensive except."""
    import os as _os
    from controller import workers

    class FakeProc:
        pid = 4242

    monkeypatch.setattr(_os, "name", "posix")
    monkeypatch.delattr(_os, "getpgid", raising=False)
    monkeypatch.delattr(_os, "killpg", raising=False)
    workers._kill_tree(FakeProc())  # must not raise


def test_worker_timeout_path_routes_through_kill_tree(env, monkeypatch):
    """Offline: Popen mock whose communicate() raises TimeoutExpired proves the
    timeout arm returns 'failed' AND tree-kills the detached group."""
    import subprocess as _sp
    from controller import workers

    class FakeProc:
        pid = 9999
        returncode = None
        def communicate(self, timeout=None):
            if timeout is not None:
                raise _sp.TimeoutExpired("hermes", timeout)
            return ("", "")

    killed = []
    monkeypatch.setattr(workers, "_kill_tree", lambda proc: killed.append(proc.pid))
    monkeypatch.setattr(workers.subprocess, "Popen", lambda *a, **k: FakeProc())
    c, wd, fake = env
    tid, st = _task(c, wd)
    res = workers.make_worker(c, hermes_bin=fake, timeout_s=1)(
        {"run_id": "r"}, {"task_id": tid, "workdir": wd, "goal": "X"},
        {"step_id": st, "kind": "impl", "title": "x"})
    assert res["status"] == "failed" and res["error"] == "worker timeout"
    assert killed == [9999]


def test_worker_spawn_receives_os_appropriate_flags(env, monkeypatch):
    """The Popen call in worker_fn must get creationflags on nt (not the
    ignored start_new_session) and start_new_session on posix. make_worker
    resolves the branch at binding time, so patch os.name only around it."""
    import os as _os
    from controller import workers

    seen = {}
    class FakeProc:
        pid = 1
        returncode = 0
        def communicate(self, timeout=None):
            return ("ok", "")
    def fake_popen(*a, **k):
        seen.update(k)
        return FakeProc()
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)
    c, wd, fake = env
    tid, st = _task(c, wd)
    run, task, step = ({"run_id": "r"}, {"task_id": tid, "workdir": wd, "goal": "X"},
                       {"step_id": st, "kind": "impl", "title": "x"})

    monkeypatch.setattr(_os, "name", "nt")
    w_nt = workers.make_worker(c, hermes_bin=fake, timeout_s=60)
    monkeypatch.undo()
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)
    seen.clear()
    w_nt(run, task, step)
    assert seen["creationflags"] == (workers.CREATE_NEW_PROCESS_GROUP
                                     | workers.DETACHED_PROCESS)
    assert "start_new_session" not in seen

    seen.clear()
    w_posix = workers.make_worker(c, hermes_bin=fake, timeout_s=60)
    w_posix(run, task, step)
    assert seen["start_new_session"] is True
    assert "creationflags" not in seen


def test_maybe_pump_spawn_receives_os_appropriate_flags(monkeypatch):
    """plugins/controller pump spawn: branch selection via _pump_spawn_kwargs
    (same flags as the worker), and _maybe_pump must forward the resolved
    kwargs to Popen."""
    import os as _os
    import plugins.controller as pc
    from controller import workers

    # branch selection (fake os.name, call the pure resolver)
    monkeypatch.setattr(_os, "name", "nt")
    kw_nt = pc._pump_spawn_kwargs()
    assert kw_nt == {"creationflags": workers.CREATE_NEW_PROCESS_GROUP
                               | workers.DETACHED_PROCESS}
    monkeypatch.setattr(_os, "name", "posix")
    assert pc._pump_spawn_kwargs() == {"start_new_session": True}
    monkeypatch.undo()

    # wiring: real Popen spy sees the resolved detached flags
    seen = {}
    class FakeProc:
        pid = 1
    def fake_popen(*a, **k):
        seen.update(k)
        return FakeProc()
    monkeypatch.setattr(pc._sp, "Popen", fake_popen)
    monkeypatch.setattr(pc, "_last_pump", 0.0)
    monkeypatch.setattr(pc, "_PUMP_SPAWN_KWARGS",
                        {"creationflags": workers.CREATE_NEW_PROCESS_GROUP
                                         | workers.DETACHED_PROCESS})
    pc._maybe_pump("default")
    assert seen["creationflags"] == (workers.CREATE_NEW_PROCESS_GROUP
                                     | workers.DETACHED_PROCESS)
    assert "start_new_session" not in seen

    seen.clear()
    monkeypatch.setattr(pc, "_last_pump", 0.0)
    monkeypatch.setattr(pc, "_PUMP_SPAWN_KWARGS", {"start_new_session": True})
    pc._maybe_pump("default")
    assert seen["start_new_session"] is True
    assert "creationflags" not in seen
