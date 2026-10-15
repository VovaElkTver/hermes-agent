"""Integration tests for the tmux-session plugin (real tmux, no mocks).

Run:  pytest tests/plugins/test_tmux_session.py -q
Covers the acceptance matrix of the plugin spec: state persistence across
calls, REPL multi-call flows, timeout-not-kill semantics, Ctrl-C recovery,
exit-code fidelity, unicode input, adapter re-attach, pane lock serialization,
clear errors, marker/staleness discipline.
"""

from __future__ import annotations

import importlib.util
import json
import os
import threading
import time
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "tmux-session"
TMP_DIR = Path("/tmp/tmux-session-tests")
# Keep the tmux socket off the pytest sandbox path: AF_UNIX sun_path caps at
# 108 bytes and CI basetemps (which the plugin derives HERMES_HOME from) can
# exceed that. Production keeps the socket under HERMES_HOME.
os.environ.setdefault("HERMES_TMUX_SOCK_ROOT", str(TMP_DIR / "sock"))


def _load_core():
    spec = importlib.util.spec_from_file_location("tmux_core_test", _PLUGIN / "tmux_core.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def core():
    ok, detail = _load_core().check_tmux_available()
    if not ok:
        pytest.fail(f"tmux unavailable: {detail}")
    return _load_core()


def _session(request, core, name="t"):
    sname = f"tmuxsess-{name}-{request.node.name[:30]}"
    r = core.ensure_session(sname, str(TMP_DIR))
    assert r["ok"], r
    pane = core.list_panes(sname)[0]["pane_id"]
    return sname, pane


@pytest.fixture()
def sess(request, core):
    TMP_DIR.mkdir(exist_ok=True)
    sname, pane = _session(request, core)
    yield sname, pane
    core.kill_session(sname)


def test_cwd_persists_between_calls(core, sess):
    _, pane = sess
    r1 = core.execute_shell(pane, "cd /tmp", wait_timeout=15)
    assert r1["completed"] and r1["exit_code"] == 0
    r2 = core.execute_shell(pane, "pwd", wait_timeout=15)
    assert "/tmp" in r2["tail_snapshot"]


def test_export_persists_between_calls(core, sess):
    _, pane = sess
    val = f"v{int(time.time())}"
    r = core.execute_shell(pane, f"export HERMES_TMUX_TEST={val}", wait_timeout=15)
    assert r["completed"]
    r = core.execute_shell(pane, "echo $HERMES_TMUX_TEST", wait_timeout=15)
    assert val in r["tail_snapshot"]


def test_venv_activation_persists(core, sess):
    _, pane = sess
    venv_activate = "/home/qwen/hermes-agent/venv/bin/activate"
    if not Path(venv_activate).exists():
        pytest.skip("no venv on this host")
    r = core.execute_shell(pane, f"source {venv_activate}", wait_timeout=15)
    assert r["completed"]
    r = core.execute_shell(pane, "which python", wait_timeout=10)
    assert "venv/bin/python" in r["tail_snapshot"]


def test_repl_multi_call_flow(core, sess):
    _, pane = sess
    assert core.send_text(pane, "python3")["ok"]
    time.sleep(1.5)
    assert core.send_text(pane, "x = 21 * 2")["ok"]
    time.sleep(0.7)
    assert core.send_text(pane, "print(x)")["ok"]
    time.sleep(0.8)
    snap = core.inspect(pane, 15)["snapshot"]
    assert "42" in snap
    assert core.send_keys(pane, ["C-d"])["ok"]
    time.sleep(0.8)
    # shell must accept a command again after the REPL exits
    r = core.execute_shell(pane, "echo back-to-shell", wait_timeout=10)
    assert r["completed"] and "back-to-shell" in r["tail_snapshot"]


def test_long_command_survives_wait_timeout(core, sess):
    _, pane = sess
    r = core.execute_shell(pane, "sleep 120", wait_timeout=2)
    assert r["state"] == "running" and r.get("marker")
    r2 = core.wait_pane(pane, r["marker"], timeout=2)
    assert not r2.get("completed")
    # and it is really still running
    snap = core.inspect(pane, 10)["snapshot"]
    assert "sleep 120" in snap


def test_ctrl_c_stops_foreground_and_shell_continues(core, sess):
    _, pane = sess
    r = core.execute_shell(pane, "sleep 60", wait_timeout=2)
    assert r["state"] == "running"
    assert core.send_keys(pane, ["C-c"])["ok"]
    time.sleep(1.0)
    r = core.execute_shell(pane, "echo recovered", wait_timeout=10)
    assert r["completed"] and "recovered" in r["tail_snapshot"]


def test_nonzero_exit_code_reported(core, sess):
    _, pane = sess
    r = core.execute_shell(pane, "sh -c 'exit 3'", wait_timeout=15)
    assert r["completed"] and r["exit_code"] == 3


def test_unicode_quotes_multiline(core, sess):
    _, pane = sess
    payload = "ü—yoū «quotes» $HOME `tick` ; ;;"
    cmd = f"printf '%s\\n' '{payload}'"
    r = core.execute_shell(pane, cmd, wait_timeout=15)
    assert r["completed"]
    assert "ü—yoū «quotes»" in r["tail_snapshot"]


def test_reattach_no_duplicate_panes(core, sess):
    sname, pane = sess
    panes_before = len(core.list_panes(sname))
    # re-running ensure_session must not create anything new
    r = core.ensure_session(sname, str(TMP_DIR))
    assert r["ok"] and not r.get("created")
    assert len(core.list_panes(sname)) == panes_before
    assert core.resolve_target(pane)["ok"]


def test_parallel_calls_serialized_by_lock(core, sess):
    _, pane = sess
    marker_file = TMP_DIR / "lock-order.txt"
    if marker_file.exists():
        marker_file.unlink()
    results = []

    def job(n):
        results.append(json.loads(json.dumps(
            core.execute_shell(pane, f"echo job{n} >> {marker_file}", wait_timeout=25))))

    threads = [threading.Thread(target=job, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.get("completed") for r in results), results
    lines = sorted(marker_file.read_text().splitlines())
    assert lines == [f"job{i}" for i in range(5)]


def test_clear_errors_for_missing_things(core, sess):
    rc_missing = core.execute_shell("%9999", "true", wait_timeout=5)
    assert "error" in rc_missing or rc_missing.get("ok") is False or rc_missing.get("state") == "unknown"
    r_bad = core.ensure_session("tmuxsess-bad-cwd-test", "/nonexistent-dir-xyz")
    assert not r_bad["ok"] and "cwd" in r_bad["error"]


def test_stale_snapshot_not_new_output(core, sess):
    _, pane = sess
    token = f"SNAPE{int(time.time())}"
    core.send_text(pane, f"echo {token}")
    time.sleep(0.8)
    before = core.inspect(pane, 40)["snapshot"]
    count_before = before.count(token)
    time.sleep(0.6)
    after = core.inspect(pane, 40)["snapshot"]
    assert after.count(token) == count_before  # no new phantom output appears
