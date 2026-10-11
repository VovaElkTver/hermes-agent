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
    c, wd, _ = env
    rv = make_review(c, judge_cfg=None)
    assert rv({"run_id": "r"}, {"step_id": "s"}, [])["verdict"] == "needs_human"


def test_review_fail_closed_on_judge_error(env, monkeypatch):
    c, wd, _ = env
    import plugins.supervisor.judge as j
    def boom(*a, **k):
        raise RuntimeError("judge down")
    monkeypatch.setattr(j, "review", boom)
    rv = make_review(c, judge_cfg={"model": "x"})
    out = rv({"run_id": "r"}, {"step_id": "s"}, [{"stdout_tail": "ok", "returncode": 0}])
    assert out["verdict"] == "needs_human" and "unavailable" in out["detail"]


def test_review_fail_closed_on_failopen_judge_pass(env, monkeypatch):
    """judge.review fails OPEN for checkpoints; completion must not inherit that."""
    c, wd, _ = env
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
    c, wd, _ = env

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
        out = rv({"run_id": "r"}, {"step_id": "s"}, [{"stdout_tail": "x", "returncode": 0}])
        assert out["verdict"] == "needs_human" and out["detail"].strip()
    finally:
        urllib.request.urlopen = orig


def test_review_gate_rejects_pass_when_worker_failed(env, monkeypatch):
    c, wd, _ = env
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
