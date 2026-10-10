"""E2E-ish smoke for the fork/join slash handlers against real store + DB.

Drives the REAL ``_handle_fork_command`` / ``_handle_join_command`` (gateway
mixins) against a real SessionStore/SessionDB in tmp_path — the same pattern as
tests/gateway/test_branch_routing_columns.py. No mocks on DB/store layers.
"""

from __future__ import annotations

import json

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, SessionStore
from hermes_state import AsyncSessionDB


@pytest.fixture()
def store(tmp_path, monkeypatch):
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    config = GatewayConfig()
    return SessionStore(sessions_dir=tmp_path, config=config)


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="170829464",
        chat_id="170829464",
        chat_type="dm",
        thread_id="544520",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(), message_id="m1")


def _make_fork_runner(store: SessionStore):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = {}
    runner._background_tasks = set()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_approvals = {}
    runner._update_prompt_pending = {}
    runner._agent_cache_lock = None
    runner.session_store = store
    runner._session_db = AsyncSessionDB(store._db)
    runner._pending_skills_reload_notes = {}
    # no-op eviction: agent cache internals are not under test here
    runner._evict_cached_agent = lambda key: None
    runner._clear_session_boundary_security_state = lambda key: None
    return runner


class TestForkJoinHandlers:
    @pytest.mark.asyncio
    async def test_fork_creates_active_branch_and_switches(self, store):
        source = _make_source()
        parent_entry = store.get_or_create_session(source)
        db = store._db
        db.append_message(parent_entry.session_id, role="user", content="hello")
        db.append_message(parent_entry.session_id, role="assistant", content="world")

        runner = _make_fork_runner(store)
        reply = await runner._handle_fork_command(_make_event("/fork side"))
        assert "⑂" in reply or "Forked" in reply

        branches = db.list_active_branches(parent_entry.session_id)
        assert len(branches) == 1
        branch = branches[0]
        assert branch["status"] == "active"
        base_uid = branch["base_message_uid"]
        assert base_uid  # pinned at the parent tail

        # the fork switched this chat onto the child session
        entry_after = store.get_or_create_session(source)
        assert entry_after.session_id == branch["child_session_id"]

        # child transcript overlays parent history
        history = db.get_messages_as_conversation(entry_after.session_id)
        contents = [str(m["content"]) for m in history]
        assert any("hello" in c for c in contents)

    @pytest.mark.asyncio
    async def test_join_note_appends_work_note_and_closes(self, store):
        source = _make_source()
        parent_entry = store.get_or_create_session(source)
        db = store._db
        db.append_message(parent_entry.session_id, role="user", content="hello")
        db.append_message(parent_entry.session_id, role="assistant", content="world")

        runner = _make_fork_runner(store)
        await runner._handle_fork_command(_make_event("/fork work"))

        # from the fork: one branch-local message, then join back
        child_entry = store.get_or_create_session(source)
        db.append_message(child_entry.session_id, role="user", content="side-channel finding")

        branch_row = db.list_active_branches(parent_entry.session_id)[0]
        # join handler runs on the PARENT chat: point the event back at it
        runner2 = _make_fork_runner(store)
        # switch routing row back to the parent for the join event
        from gateway.session import build_session_key
        store.switch_session(build_session_key(source), parent_entry.session_id)
        reply = await runner2._handle_join_command(_make_event(f"/join {branch_row['branch_id']}"))
        assert "Joined branch" in reply or "joined" in reply.lower()

        assert not db.has_active_branches(parent_entry.session_id)
        # note strategy: parent gained a work-note mentioning the branch
        hp = db.get_messages_as_conversation(parent_entry.session_id)
        notes = [m for m in hp if "work-note" in str(m.get("content", ""))]
        assert notes, "work-note must be appended to the parent"

    @pytest.mark.asyncio
    async def test_join_lists_when_ambiguous(self, store):
        source = _make_source()
        parent_entry = store.get_or_create_session(source)
        db = store._db
        db.append_message(parent_entry.session_id, role="user", content="hello")
        runner = _make_fork_runner(store)
        # cannot create two forks from same parent through the handler twice
        # because the second call would run on the switched child; drive DB directly
        base_uid = db.get_last_message_uid(parent_entry.session_id)
        for n in ("a", "b"):
            child_id = f"{parent_entry.session_id}-c{n}"
            db.ensure_session(child_id, source="test",
                              session_key=f"k-amb-{n}", parent_session_id=parent_entry.session_id)
            db.create_branch_row(db.new_branch_id(), parent_entry.session_id, child_id, base_uid)
        reply = await runner._handle_join_command(_make_event("/join"))
        assert "branch" in reply.lower() and "," in reply  # ambiguous listing
