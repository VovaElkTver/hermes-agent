"""PR-A socket canon: preempt + fork verbs are origin-attributed at the wire ROOT.

Covers the registration shape (``preempt_verb_handlers``), transport-level
``missing_origin`` / ``unknown_origin`` before any handler side effect, the
visible-origin steer label, the default tool_reason carrying origin, and
fork.pause/fork.resume riding the fork_gate CAS hub over the real socket-line
dispatcher. The handler-level behaviours (attribution, handoff staging, fan-out)
are pinned by ``test_control_socket_session_verbs.py``; this file is the wire.
"""
from __future__ import annotations

import json
import threading

from gateway.control_socket import GatewayControlServer, query_gateway_control
from gateway.run_control_handlers import preempt_verb_handlers


class _StubAgent:
    """Enough AIAgent surface for both verbs; records the exact wire."""

    def __init__(self):
        self.interrupted: list[tuple] = []
        self.steered: list[str] = []
        self._pending_steer_lock = threading.Lock()
        self._pending_steer = None
        self._turn_user_intervened = False
        self._last_activity_desc = "running tests"
        self._current_tool = "terminal"
        self._interrupt_requested = False

    def interrupt(self, message=None, **kw):
        self.interrupted.append((message, kw))
        return True

    def clear_interrupt(self):
        self._interrupt_requested = False

    def steer(self, text):
        self.steered.append(text)
        return bool(text and text.strip())


class _Conv:
    def __init__(self):
        self.sidecar_notes = []


class _State:
    def __init__(self, agent):
        self.turn = type("T", (), {"agent": agent})()
        self.conversation = _Conv()


class _Runner:
    def __init__(self, **states):
        self._states = states
        self.sidecar_sets: list[tuple] = []

    def _peek_session_state(self, key):
        return self._states.get(key)

    def _running_agent_items(self):
        return [(k, s.turn.agent) for k, s in self._states.items()]

    def _set_pending_turn_sidecar_notes(self, key, notes):
        self.sidecar_sets.append((key, list(notes)))
        self._states[key].conversation.sidecar_notes = list(notes)


KEY = "agent:main:matrix:dm:!preempt"


def _server(tmp_path, runner):
    handlers, origin_required = preempt_verb_handlers(runner)
    assert sorted(origin_required) == ["fork.close", "fork.pause", "fork.resume",
                                       "interrupt-session", "steer-session"]
    return GatewayControlServer(home=tmp_path, verb_handlers=handlers,
                                origin_required_verbs=origin_required)


def _raw(server, payload):
    return json.loads(server.handle_request_line(json.dumps(payload).encode()).decode())


def test_wire_origin_validation_before_any_handler(tmp_path):
    agent = _StubAgent()
    server = _server(tmp_path, _Runner(**{KEY: _State(agent)}))

    resp = _raw(server, {"verb": "interrupt-session", "params": {"session": KEY}})
    assert resp["ok"] is False and resp["error"].startswith("missing_origin")
    resp = _raw(server, {"verb": "steer-session", "origin": "",
                         "params": {"session": KEY, "message": "x"}})
    assert resp["ok"] is False and resp["error"].startswith("missing_origin")
    resp = _raw(server, {"verb": "fork.pause", "origin": "spam",
                         "params": {"join_id": "fj_x"}})
    assert resp["ok"] is False and resp["error"].startswith("unknown_origin")
    assert agent.interrupted == [] and agent.steered == []

    # A params-level origin does NOT satisfy the root requirement...
    resp = _raw(server, {"verb": "interrupt-session",
                         "params": {"session": KEY, "origin": "cli"}})
    assert resp["ok"] is False and resp["error"].startswith("missing_origin")
    # ...but a well-formed root one rides through, overriding any params duplicate.
    resp = _raw(server, {"verb": "steer-session", "origin": "human:telegram",
                         "params": {"session": KEY, "message": "go left", "origin": "cli"}})
    assert resp["ok"] is True
    assert agent.steered == ["[from human:telegram] go left"]
    assert resp["result"]["origin"] == "human:telegram"

    # Non-preempt verbs keep the historical no-origin wire.
    assert _raw(server, {"verb": "identify"})["ok"] is True


def test_default_tool_reason_carries_origin(tmp_path):
    from agent.interrupt_control import USER_INTERRUPT_REASONS, interrupt_issuer

    agent = _StubAgent()
    server = _server(tmp_path, _Runner(**{KEY: _State(agent)}))
    resp = _raw(server, {"verb": "interrupt-session", "origin": "agent:scheduler",
                         "params": {"session": KEY}})
    assert resp["ok"] is True and resp["result"]["interrupted"] is True
    # Attribution is visible on the agent side too: the tool_reason slug names the sender.
    assert resp["result"]["tool_reason"] == "preempted_by_agent_scheduler"
    assert interrupt_issuer(agent) not in USER_INTERRUPT_REASONS
    # Explicit reason still wins over the origin-derived default.
    resp = _raw(server, {"verb": "interrupt-session", "origin": "cli",
                         "params": {"session": KEY, "reason": "controller_preemption"}})
    assert resp["result"]["tool_reason"] == "controller_preemption"
    agent.clear_interrupt()


def test_supported_verbs_list_advertises_all_five(tmp_path):
    server = _server(tmp_path, _Runner(**{KEY: _State(_StubAgent())}))
    resp = json.loads(server.handle_request_line(b'{"verb": "nope"}').decode())
    assert resp["ok"] is False
    for verb in ("interrupt-session", "steer-session", "fork.pause", "fork.resume", "fork.close"):
        assert verb in resp["supported_verbs"]


def test_old_gateway_fallback_contract(tmp_path):
    """Pre-PR-A gateway: unknown verb + no preempt verbs listed; the client helper
    collapses to None so callers keep the legacy path."""
    server = GatewayControlServer(home=tmp_path, verb_handlers={})
    raw = json.dumps({"verb": "interrupt-session", "origin": "cli",
                      "params": {"session": "x"}}).encode()
    resp = json.loads(server.handle_request_line(raw).decode())
    assert resp["ok"] is False and resp["error"].startswith("unknown verb")
    assert query_gateway_control(tmp_path, "interrupt-session", origin="cli",
                                 params={"session": "x"}) is None


def test_fork_pause_resume_over_the_gate(tmp_path, monkeypatch):
    """fork.* verbs dispatch through the socket and drive the CAS hub directly (the
    soft-DB-gate path is the ONLY machine path; §VERDICT-2)."""
    from tools import fork_explore_journal as fem

    monkeypatch.setattr(fem, "get_hermes_home", lambda *a, **k: tmp_path)
    fem.ensure_gate_rows("fj_preempt", ["b1"])
    server = _server(tmp_path, _Runner())

    resp = _raw(server, {"verb": "fork.pause", "origin": "cli",
                         "params": {"join_id": "fj_preempt", "ref": "b1"}})
    assert resp["ok"] is True
    inner = resp["result"].get("result", resp["result"])
    assert inner.get("marked_paused") or "already_paused" in json.dumps(inner)
    assert fem.get_gate("fj_preempt", "b1")["state"] == "paused"

    resp = _raw(server, {"verb": "fork.resume", "origin": "agent:scheduler",
                         "params": {"join_id": "fj_preempt", "ref": "b1"}})
    assert resp["ok"] is True
    assert fem.get_gate("fj_preempt", "b1")["state"] == "resumed_pending"

    resp = _raw(server, {"verb": "fork.pause", "params": {"join_id": "fj_preempt"}})
    assert resp["ok"] is False and resp["error"].startswith("missing_origin")
