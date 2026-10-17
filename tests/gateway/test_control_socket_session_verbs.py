"""PR-A control-socket verbs: interrupt-session / steer-session (out-of-band preemption).

Covers the scheduler-facing contract: a process without a chat platform stops or
redirects a live session turn over the gateway control socket, the preempt is
attributed as SYSTEM (``controller_preemption``, never a user stop), a handoff line is
returned + staged for a cheap resume, and old gateways (no verb) degrade to None so
callers keep their legacy path.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from gateway.control_socket import (
    GatewayControlServer,
    interrupt_gateway_session,
    query_gateway_control,
    steer_gateway_session,
)
from gateway.run_session_verbs import interrupt_session_verb, steer_session_verb
from tools.interrupt import set_interrupt


def _bare_agent():
    from run_agent import AIAgent

    agent = AIAgent.__new__(AIAgent)
    agent._interrupt_requested = False
    agent._interrupt_message = None
    agent._tool_interrupt_reason = None
    agent._hard_interrupt_requested = threading.Event()
    agent._execution_thread_id = None
    agent._interrupt_thread_signal_pending = False
    agent._active_children = []
    agent._active_children_lock = threading.Lock()
    agent._pending_steer_lock = threading.Lock()
    agent._pending_steer = None
    agent._turn_user_intervened = False
    agent._last_activity_desc = "editing src/app.py"
    agent._current_tool = "patch"
    agent.quiet_mode = True
    return agent


class _Turn:
    def __init__(self, agent):
        self.agent = agent


class _Conv:
    def __init__(self):
        self.sidecar_notes = []


class _State:
    def __init__(self, agent):
        self.turn = _Turn(agent)
        self.conversation = _Conv()


class _Runner:
    """Duck-typed gateway runner: only the peek/items/sidecar seams are used."""

    def __init__(self, **states):
        self._states = states
        self.sidecar_sets = []

    def _peek_session_state(self, key):
        return self._states.get(key)

    def _running_agent_items(self):
        return [(k, s.turn.agent) for k, s in self._states.items()]

    def _set_pending_turn_sidecar_notes(self, key, notes):
        self.sidecar_sets.append((key, list(notes)))
        self._states[key].conversation.sidecar_notes = list(notes)


def _server(tmp_path, runner, *, with_verbs=True):
    handlers = {}
    if with_verbs:
        handlers = {"interrupt-session": interrupt_session_verb(runner),
                    "steer-session": steer_session_verb(runner)}
    return GatewayControlServer(home=tmp_path, verb_handlers=handlers)


def _dispatch(server, verb, params):
    line = json.dumps({"id": 1, "verb": verb, "params": params}).encode() + b"\n"
    return json.loads(server.handle_request_line(line))


# ---------------------------------------------------------------- verbs (direct)

def test_interrupt_session_attributed_system_not_user(tmp_path):
    """The #112647 invariant on the new surface: the preempt publishes
    ``controller_preemption`` — interrupt_issuer names it, and it is NOT one of the
    user-stop categories."""
    from agent.interrupt_control import USER_INTERRUPT_REASONS, interrupt_issuer

    agent = _bare_agent()
    runner = _Runner(**{"agent:default:telegram:dm": _State(agent)})
    server = _server(tmp_path, runner)
    try:
        resp = _dispatch(server, "interrupt-session",
                         {"session": "agent:default:telegram:dm"})
        assert resp["ok"] is True and resp["result"]["interrupted"] is True
        assert resp["result"]["tool_reason"] == "controller_preemption"
        assert interrupt_issuer(agent) == "controller_preemption"
        assert interrupt_issuer(agent) not in USER_INTERRUPT_REASONS
        # Handoff: what it was doing, which tool it stopped on — returned AND staged.
        handoff = resp["result"]["handoff"]
        assert "editing src/app.py" in handoff and "patch" in handoff
        assert runner.sidecar_sets and "patch" in runner.sidecar_sets[0][1][0]
    finally:
        agent.clear_interrupt()
        set_interrupt(False)


def test_interrupt_session_message_does_not_become_user_stop(tmp_path):
    """A preempt that carries text must STILL be system-attributed (#130207 shape)."""
    from agent.interrupt_control import interrupt_issuer

    agent = _bare_agent()
    runner = _Runner(s=_State(agent))
    server = _server(tmp_path, runner)
    try:
        resp = _dispatch(server, "interrupt-session",
                         {"session": "s", "reason": "priority_task", "message": "switch now"})
        assert resp["ok"] is True and resp["result"]["interrupted"] is True
        assert resp["result"]["tool_reason"] == "priority_task"
        assert interrupt_issuer(agent) == "priority_task"
        assert agent._interrupt_message == "switch now"
    finally:
        agent.clear_interrupt()
        set_interrupt(False)


def test_interrupt_session_hard_variant(tmp_path):
    agent = _bare_agent()
    runner = _Runner(s=_State(agent))
    server = _server(tmp_path, runner)
    try:
        resp = _dispatch(server, "interrupt-session", {"session": "s", "hard": True})
        assert resp["ok"] is True and resp["result"]["interrupted"] is True
        assert resp["result"]["hard"] is True
        assert agent._interrupt_requested is True
    finally:
        agent.clear_interrupt(hard_cancel=True)
        set_interrupt(False)


def test_interrupt_session_no_running_turn(tmp_path):
    runner = _Runner()
    server = _server(tmp_path, runner)
    resp = _dispatch(server, "interrupt-session", {"session": "missing:key"})
    assert resp["ok"] is True and resp["result"]["interrupted"] is False


def test_interrupt_session_unique_substring_resolution(tmp_path):
    agent = _bare_agent()
    runner = _Runner(**{"agent:default:matrix:!room123": _State(agent)})
    server = _server(tmp_path, runner)
    try:
        resp = _dispatch(server, "interrupt-session", {"session": "room123"})
        assert resp["ok"] is True and resp["result"]["interrupted"] is True
        assert resp["result"]["session"] == "agent:default:matrix:!room123"
    finally:
        agent.clear_interrupt()
        set_interrupt(False)


def test_interrupt_session_ambiguous_substring(tmp_path):
    runner = _Runner(**{"k:roomA": _State(_bare_agent()), "k:roomB1": _State(_bare_agent())})
    server = _server(tmp_path, runner)
    resp = _dispatch(server, "interrupt-session", {"session": "room"})
    assert resp["ok"] is True and resp["result"]["interrupted"] is False
    assert "ambiguous" in resp["result"].get("error", "")


def test_steer_session_delivers_and_fans_out(tmp_path):
    agent, kid = _bare_agent(), _bare_agent()
    agent._active_children = [kid]
    runner = _Runner(s=_State(agent))
    server = _server(tmp_path, runner)
    resp = _dispatch(server, "steer-session", {"session": "s", "message": "use venv"})
    assert resp["ok"] is True and resp["result"]["accepted"] is True
    assert resp["result"]["children"] == 1
    assert agent._pending_steer == "use venv"
    assert kid._pending_steer == "use venv"


def test_steer_session_no_running_turn_is_not_error(tmp_path):
    runner = _Runner()
    server = _server(tmp_path, runner)
    resp = _dispatch(server, "steer-session", {"session": "gone", "message": "x"})
    assert resp["ok"] is True
    assert resp["result"]["accepted"] is False
    assert resp["result"]["reason"] == "no-running-turn"


# ------------------------------------------------------------ client + fallback

@pytest.mark.platforms("posix")  # unix socket transport
def test_clients_roundtrip_over_real_socket(tmp_path):
    agent = _bare_agent()
    runner = _Runner(**{"telegram:chat1": _State(agent)})

    async def scenario():
        server = _server(tmp_path, runner)
        assert await server.start()
        try:
            loop = asyncio.get_running_loop()
            i = await loop.run_in_executor(
                None, lambda: interrupt_gateway_session(tmp_path, "chat1", timeout=5.0))
            s = await loop.run_in_executor(
                None, lambda: steer_gateway_session(tmp_path, "telegram:chat1", "hold on", timeout=5.0))
            return i, s
        finally:
            await server.stop()

    intr, st = asyncio.run(scenario())
    assert intr is not None and intr["interrupted"] is True
    assert intr["tool_reason"] == "controller_preemption"
    assert "handoff" in intr
    assert st is not None and st["accepted"] is True
    agent.clear_interrupt()
    set_interrupt(False)


@pytest.mark.platforms("posix")  # unix socket transport
def test_clients_none_when_gateway_lacks_verbs(tmp_path):
    """Back-compat (старый гейтвей): a gateway without the verbs answers ok:false →
    both clients return None so the scheduler keeps its legacy path."""

    async def scenario():
        server = _server(tmp_path, _Runner(), with_verbs=False)  # only identify/status built in
        assert await server.start()
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, lambda: (
                    interrupt_gateway_session(tmp_path, "s", timeout=5.0),
                    steer_gateway_session(tmp_path, "s", "x", timeout=5.0)))
        finally:
            await server.stop()

    i, s = asyncio.run(scenario())
    assert i is None and s is None


def test_clients_none_when_no_socket(tmp_path):
    assert interrupt_gateway_session(tmp_path, "s", timeout=0.5) is None
    assert steer_gateway_session(tmp_path, "s", "x", timeout=0.5) is None
