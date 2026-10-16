"""/att — human soft-pause of a live turn (freeze at the next safe boundary, continue-or-cancel fork)."""

import types

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import EphemeralReply
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from hermes_cli.commands import resolve_command


def _make_event(text: str) -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM, user_id="u1", chat_id="c1", user_name="tester", chat_type="dm",
    )
    return MessageEvent(text=text, source=source)


def _runner_with_agent(agent):
    runner = object.__new__(gateway_run.GatewayRunner)
    state = types.SimpleNamespace(turn=types.SimpleNamespace(agent=agent))
    runner._peek_session_state = lambda session_key: state
    return runner


class TestAttCommandDef:
    def test_resolves_with_att_busy_handler(self):
        cmd_def = resolve_command("att")
        assert cmd_def is not None and cmd_def.name == "att"
        assert cmd_def.busy_handler == "att"
        assert cmd_def.busy_policy == "interrupt_then_dispatch"
        assert getattr(cmd_def, "gateway_only", False) is True


class TestBusyAtt:
    @pytest.mark.asyncio
    async def test_soft_interrupts_running_agent_and_keeps_session(self):
        calls = []

        class FakeAgent:
            def interrupt(self, message=None, **kw):
                calls.append((message, kw))
                return True

        runner = _runner_with_agent(FakeAgent())
        result = await runner._busy_att_command(_make_event("/att wrong repo"), "k", None)
        assert isinstance(result, EphemeralReply)
        assert "continue" in result.text.lower()  # paused notice, not a kill notice
        assert calls == [("wrong repo", {})]  # soft: no hard_cancel kwargs

    @pytest.mark.asyncio
    async def test_noteless_uses_plain_soft_interrupt(self):
        calls = []

        class FakeAgent:
            def interrupt(self, message=None, **kw):
                calls.append(message)
                return True

        runner = _runner_with_agent(FakeAgent())
        await runner._busy_att_command(_make_event("/att"), "k", None)
        assert calls == [None]

    @pytest.mark.asyncio
    async def test_pending_sentinel_reports_none(self):
        from gateway.run import _AGENT_PENDING_SENTINEL
        runner = _runner_with_agent(_AGENT_PENDING_SENTINEL)
        result = await runner._busy_att_command(_make_event("/att"), "k", None)
        assert isinstance(result, EphemeralReply)
        assert "nothing is running" in result.text.lower()

    @pytest.mark.asyncio
    async def test_no_turn_reports_none(self):
        runner = object.__new__(gateway_run.GatewayRunner)
        runner._peek_session_state = lambda session_key: None
        result = await runner._busy_att_command(_make_event("/att"), "k", None)
        assert isinstance(result, EphemeralReply)
        assert "nothing is running" in result.text.lower()

    @pytest.mark.asyncio
    async def test_interrupt_failure_is_contained(self, monkeypatch):
        class Boom:
            def interrupt(self, message=None, **kw):
                raise RuntimeError("dead lock")

        runner = _runner_with_agent(Boom())
        result = await runner._busy_att_command(_make_event("/att"), "k", None)
        assert isinstance(result, EphemeralReply)  # warning logged, no crash, none notice


class TestIdleAtt:
    @pytest.mark.asyncio
    async def test_idle_att_says_nothing_running(self):
        runner = object.__new__(gateway_run.GatewayRunner)
        result = await runner._handle_att_command(_make_event("/att"))
        assert isinstance(result, EphemeralReply)
        assert "nothing is running" in result.text.lower()
