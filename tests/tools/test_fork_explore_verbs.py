"""Behavior contracts for the PR-2b continuation gate (fork_gate table),
lazy re-drive, and wire verbs (STITCH-DESIGN-8012 §3/§5).

Invariants under contract:
- every fork_gate transition is a CAS; a racing loser sees rowcount==0,
- the branch boundary believes the DB over any in-memory cache,
- resume only queues (resumed_pending); the driver owns actual replay,
- double resume is a no-op, close is soft and terminal.
"""

import json
import sqlite3
import threading
import time

from tools import fork_explore_journal as fem
from tools import fork_explore_verbs as verbs
from tools.fork_explore_rollout import BranchRoller, ContinueFlag, redrive_from_checkpoint
from tools.fork_explore_tool import (
    _ACTIVE_FORK_FLAGS as _FLAGS,
    _redrive_session_forks as driver,
    run_branch,
)


def base_cfg(**over):
    cfg = {
        "depth_default": 2, "depth_max": 3, "tokens_per_step_max": 200,
        "temperature": 0.7, "bad_json_retry": False, "forbidden_tools": [],
        "read_roots": [], "fork_deadline_s": 30,
    }
    cfg.update(over)
    return cfg


def _script_chat(steps, calls):
    def chat(client, model, messages, **kw):
        calls.append(kw.get("seed"))
        done = sum(1 for m in messages if m["role"] == "assistant")
        step = steps[min(done, len(steps) - 1)]
        return json.dumps(step), 50
    return chat


def _script_chat_by_call(steps, calls):
    """Like _script_chat but keyed by CALL INDEX — a resumed leg carries
    replayed assistant messages that would otherwise shift the cursor."""
    def chat(client, model, messages, **kw):
        i = len(calls)
        calls.append(i)
        return json.dumps(steps[min(i, len(steps) - 1)]), 50
    return chat


STEP_A1 = {"kind": "action", "content": "А-маркер: идём через спецификацию, шаг 1"}
STEP_A2 = {"kind": "stop", "content": "А-маркер: направление зафиксировано"}


class TestForkGateCas:
    """The gate is a state machine with CAS-only transitions."""

    def test_cas_wins_only_from_listed_states(self):
        fem.ensure_gate_rows("fj_g1", ["b1"])
        assert fem.cas_gate("fj_g1", "b1", ("running",), "paused",
                            reason="human", holder="1:1")
        # from 'paused' nobody may re-enter paused via the same CAS list
        assert not fem.cas_gate("fj_g1", "b1", ("running",), "paused")
        row = fem.get_gate("fj_g1", "b1")
        assert row["state"] == "paused" and row["reason"] == "human"

    def test_missing_row_reads_as_running(self):
        assert fem.get_gate("fj_absent") is None
        assert fem.gate_open("fj_absent", "b9")

    def test_group_row_blocks_every_branch(self):
        fem.ensure_gate_rows("fj_g2", ["b1", "b2"])
        assert fem.cas_gate("fj_g2", "", ("running",), "paused")
        assert not fem.gate_open("fj_g2", "b1")
        assert not fem.gate_open("fj_g2", "b2")
        # resuming the GROUP row lands it in resumed_pending — which is STILL
        # blocking (§5 §3.1): only the lazy re-drive flipping rows back to
        # 'running' releases the branches.
        assert fem.cas_gate("fj_g2", "", ("paused",), "resumed_pending")
        assert not fem.gate_open("fj_g2", "b1")
        assert not fem.gate_open("fj_g2", "b2")
        # the driver's handoff (step 2 mechanics) releases them
        assert fem.cas_gate("fj_g2", "", ("resumed_pending",), "running")
        assert fem.gate_open("fj_g2", "b1"), "driver took ownership"
        assert fem.gate_open("fj_g2", "b2")
        # while a branch's OWN row is paused it stays blocked regardless
        fem.cas_gate("fj_g2", "b1", ("running",), "paused")
        assert not fem.gate_open("fj_g2", "b1")
        assert fem.gate_open("fj_g2", "b2")

    def test_close_is_terminal_for_resumed_pending(self):
        fem.ensure_gate_rows("fj_g3", ["b1"])
        fem.cas_gate("fj_g3", "b1", ("running",), "paused")
        fem.cas_gate("fj_g3", "b1", ("paused",), "resumed_pending")
        assert fem.cas_gate("fj_g3", "b1", ("paused", "resumed_pending"), "closed",
                            reason="eval_terminal")
        assert not fem.gate_open("fj_g3", "b1")


class TestGateAtBoundary:
    """rollout reads the DB gate at the boundary: DB wins over the cache."""

    def test_db_pause_stops_despite_live_cache_flag(self):
        calls = []
        flag = ContinueFlag()  # fast-path cache still says «continue»
        fem.ensure_gate_rows("fj_bnd", ["b1"])

        # the composition run_branch() builds for a journaled branch:
        cont = lambda: flag.get() and fem.gate_open("fj_bnd", "b1")

        def pause_at_first_boundary(cp):
            if cp["step_index"] >= 1:
                assert fem.cas_gate("fj_bnd", "b1", ("running",), "paused")

        roller = BranchRoller(None, "m", base_cfg(), time.monotonic() + 30,
                              cont=cont, checkpoint=pause_at_first_boundary,
                              chat=_script_chat([STEP_A1, STEP_A2], calls))
        trace = roller.run([], "b1", "с")
        # The cache flag stayed up; the DB pause is what stopped the branch.
        assert len(calls) == 1 and trace["stop_reason"] == "paused_by_request"
        # rollout is a pure reader: gate row left exactly as the writer set it
        assert fem.get_gate("fj_bnd", "b1")["state"] == "paused"

    def test_run_branch_believes_db_not_the_flag(self):
        # Boundary contract: the DB gate is consulted between steps, so a branch
        # whose row is closed/paused burns at most one request before standing
        # down — even though its in-memory cache flag still says «continue».
        calls = []
        flag = ContinueFlag()
        fem.ensure_gate_rows("fj_rb", ["b1"])
        fem.cas_gate("fj_rb", "b1", ("running",), "closed", reason="eval_terminal")
        trace = run_branch(None, "m", [], "b1", "с",
                           base_cfg(depth_default=2, depth_max=3),
                           time.monotonic() + 30,
                           chat=_script_chat([STEP_A1, STEP_A2], calls),
                           cont=flag, join_id="fj_rb")
        assert len(calls) == 1 and trace["stop_reason"] == "paused_by_request"

    def test_run_branch_without_join_id_keeps_flag_semantics(self):
        # No join_id → no journal → cont is the bare flag; the inter-step gate
        # halts the repeated stop-step via selective_halt after planned depth.
        calls = []
        flag = ContinueFlag()
        trace = run_branch(None, "m", [], "b1", "с", base_cfg(),
                           time.monotonic() + 30,
                           chat=_script_chat([STEP_A1], calls), cont=flag)
        assert len(calls) == 2 and trace["stop_reason"] == "no_progress"


class TestLazyRedrive:
    """Resume only queues (resumed_pending); the driver replays from the last
    boundary checkpoint and claims ownership by CAS from resumed_pending."""

    @staticmethod
    def _queued_fork(jid, session="s-redrive", steps=1, tokens=120):
        fem.write_run({"join_id": jid, "session_id": session, "strategy": "select",
                       "branches": [{"ref": "b1", "strategy_text": "с"}]})
        fem.write_checkpoint(jid, {
            "branch_ref": "b1", "step_index": steps, "tokens": tokens,
            "budget_remaining_tokens": 600 - tokens, "stop_reason": "paused_by_request",
            "cont_state": False, "steps": [STEP_A1] * steps})
        fem.ensure_gate_rows(jid, ["b1"], holder="999999:42")
        assert fem.cas_gate(jid, "b1", ("running",), "paused")
        assert fem.cas_gate(jid, "b1", ("paused",), "resumed_pending")

    def test_redrive_leg_continues_trace_and_tokens(self):
        cp = {"branch_ref": "b1", "step_index": 1, "tokens": 120,
              "steps_json": json.dumps([STEP_A1]), "cont_state": 0,
              "budget_remaining_tokens": 480, "stop_reason": "paused_by_request"}
        calls = []
        trace = redrive_from_checkpoint(
            None, "m", base_cfg(), time.monotonic() + 30, cp,
            chat=_script_chat_by_call([{"kind": "decision",
                                        "content": "второй шаг: сверяем контракт с журналом"},
                                       STEP_A2], calls))
        # seeded step re-emerges in the trace; two fresh legs join after it
        assert trace["steps"][0] == STEP_A1 and len(trace["steps"]) == 3
        assert trace["tokens"] == 220  # seeded 120 + 2 fresh steps × 50

    def test_redrive_from_checkpoint_helper_seeds_state(self):
        self._queued_fork("fj_rd1")
        cps = fem.get_checkpoints("fj_rd1", "b1")
        assert cps[-1]["step_index"] == 1
        calls = []
        trace = redrive_from_checkpoint(
            None, "m", base_cfg(depth_default=1, depth_max=2),
            time.monotonic() + 30, cps[-1],
            chat=_script_chat_by_call([STEP_A1, STEP_A2], calls))
        assert len(calls) <= 1          # only one leg fits under depth_max=2
        assert trace["tokens"] >= 120   # previous spend carried over

    def test_driver_claims_by_cas_and_double_claim_loses(self):
        self._queued_fork("fj_rd2")
        work = fem.resumed_pending_work("s-redrive")
        assert ("fj_rd2", "b1") in work
        # first driver wins the claim...
        jid, ref = work[work.index(("fj_rd2", "b1"))]
        assert fem.cas_gate(jid, ref, ("resumed_pending",), "running", holder="me")
        # ...second driver's identical claim finds nothing queued anymore
        assert ("fj_rd2", "b1") not in fem.resumed_pending_work("s-redrive")

    def test_dead_on_arrival_resume_heals_via_sweep_then_driver(self, monkeypatch):
        # fork died with its row 'running' and a dead holder: sweep → paused,
        # then the pause→resume→redrive chain works end to end without any
        # live thread or in-memory flag.
        fem.ensure_gate_rows("fj_rd3", ["b1"], holder="999999:42")
        swept = fem.sweep_crashed_gates(my_holder=fem.proc_stamp())
        assert swept >= 1
        row = fem.get_gate("fj_rd3", "b1")
        assert row["state"] == "paused" and row["reason"] == "crashed"
        assert fem.cas_gate("fj_rd3", "b1", ("paused",), "resumed_pending")
        fem.write_run({"join_id": "fj_rd3", "session_id": "s-redrive",
                       "strategy": "select", "branches": [{"ref": "b1"}]})
        fem.write_checkpoint("fj_rd3", {"branch_ref": "b1", "step_index": 1,
                                        "tokens": 50, "cont_state": 0,
                                        "steps": [STEP_A1]})
        from tools import fork_explore_tool as fet
        monkeypatch.setattr(fet, "_chat",
                            lambda *a, **kw: (json.dumps(STEP_A2), 50))
        from tools.fork_explore_tool import _redrive_session_forks
        done = _redrive_session_forks(
            "s-redrive", base_cfg(depth_default=1, depth_max=2), None, "m")
        assert done == 1
        # healed: driver took ownership and the row now reads running
        assert fem.get_gate("fj_rd3", "b1")["state"] == "running"

    def test_group_row_expands_to_branch_work(self):
        self._queued_fork("fj_rd4")
        fem.ensure_gate_rows("fj_rd4g", ["b1", "b2"], holder="9:9")
        fem.cas_gate("fj_rd4g", "", ("running",), "paused")
        fem.cas_gate("fj_rd4g", "", ("paused",), "resumed_pending")
        work = fem.resumed_pending_work("s-other-session")
        assert all(jid != "fj_rd4" for jid, _ in work)  # other sessions filtered


class TestWireVerbs:
    """fork-pause / fork-resume / fork-close: API layer over fork_gate.
    Race contracts: CAS decides, errors distinguishable, double resume no-op,
    resume-after-restart heals a dead-on-arrival fork."""

    @staticmethod
    def _fork_with_flags(jid, refs=("b1", "b2")):
        fem.write_run({"join_id": jid, "session_id": "s-verbs", "strategy": "select",
                       "branches": [{"ref": r} for r in refs]})
        fem.ensure_gate_rows(jid, list(refs))
        from tools import fork_explore_tool as fet
        flags = {r: ContinueFlag() for r in refs}
        fet._ACTIVE_FORK_FLAGS[jid] = flags
        return flags

    @classmethod
    def _teardown_flags(cls, jid):
        from tools import fork_explore_tool as fet
        fet._ACTIVE_FORK_FLAGS.pop(jid, None)

    def test_pause_result_wire_shape_and_live_flag_count(self):
        jid = "fj_w1"
        self._fork_with_flags(jid)
        try:
            out = verbs.pause_branch(jid, "b1")
            assert out["ok"] and out["result"]["marked_paused"] == 1
            assert out["result"]["stopped_inflight"] == 1
            assert out["result"]["variant"] == "soft"
            # same-process cache flipped with the DB row
            flags = _FLAGS[jid]
            assert not flags["b1"].get() and flags["b2"].get()
            # second pause of the same branch is a distinct outcome
            again = verbs.pause_branch(jid, "b1")
            assert not again["ok"] and again["error"].startswith("already_paused")
        finally:
            self._teardown_flags(jid)

    def test_pause_unknown_branch_reports_code(self):
        out = verbs.pause_branch("fj_missing_entirely")
        assert not out["ok"] and out["error"].startswith("unknown_branch")

    def test_pause_after_close_is_already_closed(self):
        jid = "fj_w3"
        self._fork_with_flags(jid)
        try:
            assert verbs.close_branch(jid, "b1")["ok"]
            out = verbs.pause_branch(jid, "b1")
            assert not out["ok"] and out["error"].startswith("already_closed")
        finally:
            self._teardown_flags(jid)

    def test_double_resume_noop(self):
        jid = "fj_w4"
        self._fork_with_flags(jid)
        try:
            assert verbs.pause_branch(jid, "b1")["ok"]
            first = verbs.resume_branch(jid, "b1")
            assert first["ok"] and first["result"]["queued_for_redrive"] == 1
            second = verbs.resume_branch(jid, "b1")
            assert second["ok"]
            assert second["result"]["queued_for_redrive"] == 0  # no new effects
            assert second["result"]["disposition"] == "deferred"
        finally:
            self._teardown_flags(jid)

    def test_resume_of_running_is_already_running_error(self):
        jid = "fj_w5"
        self._fork_with_flags(jid)
        try:
            out = verbs.resume_branch(jid, "b1")
            assert not out["ok"] and out["error"].startswith("already_running")
        finally:
            self._teardown_flags(jid)

    def test_resume_after_restart_heals_dead_on_arrival(self):
        # restart = the tool-call ended (flags registry dropped) + connection
        # closed/reopened. Resume must queue the orphaned paused row, never err.
        jid = "fj_w6"
        self._fork_with_flags(jid)
        fem.write_checkpoint(jid, {"branch_ref": "b1", "step_index": 2,
                                   "tokens": 90, "cont_state": 0,
                                   "steps": [STEP_A1, STEP_A1]})
        assert verbs.pause_branch(jid, "")["ok"]          # whole-fork pause
        self._teardown_flags(jid)                          # tool-call returned
        fem.write_run({"join_id": jid, "session_id": "s-verbs",
                       "strategy": "select", "branches": [{"ref": "b1"}]})
        reopened = sqlite3.connect(fem.journal_path(), timeout=2.0)
        reopened.execute("SELECT 1 FROM fork_gate").fetchone()
        reopened.close()
        out = verbs.resume_branch(jid, "")
        assert out["ok"], out
        assert out["result"]["queued_for_redrive"] >= 1
        work = fem.resumed_pending_work("s-verbs")
        assert ("fj_w6", "b1") in work                     # driver will replay

    def test_gate_vs_close_race_cas_decides_one_winner(self):
        # pause and close hit the same running row concurrently in-process;
        # exactly one of them may flip it, the loser must report its code.
        jid = "fj_w7"
        self._fork_with_flags(jid)
        try:
            results: list = []
            barrier = threading.Barrier(2)

            def racer(fn, args):
                barrier.wait()
                results.append(fn(*args))

            t1 = threading.Thread(target=racer, args=(verbs.pause_branch, (jid, "b1")))
            t2 = threading.Thread(target=racer, args=(verbs.close_branch, (jid, "b1")))
            t1.start(); t2.start(); t1.join(); t2.join()
            # contract: every outcome well-formed, DB consistent, exactly one
            # state transition chain survived — pause-then-close or close-then-pause
            states = {r.get("result", {}).get("ref") for r in results}
            assert states <= {"b1"}
            row = fem.get_gate(jid, "b1")
            if any(not r["ok"] for r in results):
                loser = next(r for r in results if not r["ok"])
                assert loser["error"].startswith("already_closed")
                assert row["state"] == "closed"
            else:
                assert row["state"] == "closed"  # close always ends terminal
        finally:
            self._teardown_flags(jid)

    def test_full_cycle_pause_resume_driver_settle(self):
        jid = "fj_w8"
        self._fork_with_flags(jid)
        try:
            assert verbs.pause_branch(jid, "")["ok"]
            assert verbs.resume_branch(jid, "")["ok"]
            assert fem.get_gate(jid, "")["state"] == "resumed_pending"
            done = driver("s-verbs", base_cfg(), None, "m")
            assert done == 0  # nothing to replay onto (no checkpoints): settled
            assert fem.get_gate(jid, "")["state"] == "running"
            assert fem.get_gate(jid, "b1")["state"] == "closed"  # fan-out ended
        finally:
            self._teardown_flags(jid)

