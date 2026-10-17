"""Behavior contracts for the PR-2b continuation gate (fork_gate table),
lazy re-drive, and wire verbs (STITCH-DESIGN-8012 §3/§5).

Invariants under contract:
- every fork_gate transition is a CAS; a racing loser sees rowcount==0,
- the branch boundary believes the DB over any in-memory cache,
- resume only queues (resumed_pending); the driver owns actual replay,
- double resume is a no-op, close is soft and terminal.
"""

import json
import time

from tools import fork_explore_journal as fem
from tools.fork_explore_rollout import BranchRoller, ContinueFlag
from tools.fork_explore_tool import run_branch


def base_cfg(**over):
    cfg = {
        "depth_default": 2, "depth_max": 3, "tokens_per_step_max": 200,
        "temperature": 0.7, "bad_json_retry": False, "forbidden_tools": [],
        "read_roots": [],
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
