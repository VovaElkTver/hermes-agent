"""Tests for reply-ownership gating (local patch 27.09).

A group-room reply whose m.in_reply_to.event_id names an event THIS adapter
sent counts as addressing the bot (skips the mention gate).  Thread
fall-back relations (is_falling_back) never impersonate ownership.  Feature
is gated by reply_ownership / MATRIX_REPLY_OWNERSHIP, default off.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig


def _make_adapter():
    from plugins.platforms.matrix.adapter import MatrixAdapter

    config = PlatformConfig(
        enabled=True,
        token="syt_test_token",
        extra={
            "homeserver": "https://matrix.example.org",
            "user_id": "@hermes:example.org",
        },
    )
    adapter = MatrixAdapter(config)
    adapter._text_batch_delay_seconds = 0
    adapter.handle_message = AsyncMock()
    adapter._startup_ts = time.time() - 10
    # stubs the real dispatcher fills in
    adapter._resolve_room_identity = AsyncMock(
        return_value=SimpleNamespace(
            display_name="room", room_topic="", server_name="", chat_type="group"
        )
    )
    adapter._get_display_name = AsyncMock(return_value="alice")
    adapter._background_read_receipt = lambda *a, **k: None
    adapter._user_id = "@hermes:example.org"
    return adapter


def _ctx(adapter, *, body, relates_to=None, ownership=True, sent_ids=(), room="!room1:example.org"):
    adapter._require_mention = True
    adapter._reply_ownership = ownership
    adapter._free_rooms = set()
    adapter._allowed_rooms = set()
    adapter._sent_event_ids.clear()
    adapter._sent_event_ids_set.clear()
    for eid in sent_ids:
        adapter._mark_sent_event(eid)
    return adapter._resolve_message_context(
        room,
        "@alice:example.org",
        "$incoming",
        body,
        {"body": body, "msgtype": "m.text"},
        relates_to or {},
    )


@pytest.mark.asyncio
async def test_reply_to_own_event_wakes_without_mention():
    a = _make_adapter()
    out = await _ctx(
        a, body="ок, сделаю", relates_to={"m.in_reply_to": {"event_id": "$own1"}},
        sent_ids=["$own1"],
    )
    assert out is not None


@pytest.mark.asyncio
async def test_reply_to_foreign_event_stays_silent():
    a = _make_adapter()
    out = await _ctx(
        a, body="а не мне отвечаете", relates_to={"m.in_reply_to": {"event_id": "$other"}},
        sent_ids=["$own1"],
    )
    assert out is None


@pytest.mark.asyncio
async def test_feature_off_reply_to_own_still_gated():
    a = _make_adapter()
    out = await _ctx(
        a, body="ок", relates_to={"m.in_reply_to": {"event_id": "$own1"}},
        ownership=False, sent_ids=["$own1"],
    )
    assert out is None


@pytest.mark.asyncio
async def test_thread_fallback_relation_does_not_impersonate():
    a = _make_adapter()
    out = await _ctx(
        a,
        body="сообщение в чужом треде",
        relates_to={
            "rel_type": "m.thread",
            "event_id": "$own1",
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": "$own1"},
        },
        sent_ids=["$own1"],
    )
    assert out is None


@pytest.mark.asyncio
async def test_evicted_id_forgets_ownership():
    a = _make_adapter()
    ids = [f"$e{i}" for i in range(4097)]
    out = await _ctx(
        a, body="ранний реплай", relates_to={"m.in_reply_to": {"event_id": ids[0]}},
        sent_ids=ids,
    )
    assert out is None  # ids[0] evicted by the bounded window


@pytest.mark.asyncio
async def test_explicit_reply_to_own_in_thread_wakes():
    """Human Reply inside a thread (no is_falling_back) = ownership sticks."""
    a = _make_adapter()
    out = await _ctx(
        a,
        body="уточнение",
        relates_to={
            "rel_type": "m.thread",
            "event_id": "$root",
            "m.in_reply_to": {"event_id": "$own1"},
        },
        sent_ids=["$own1"],
    )
    assert out is not None


@pytest.mark.asyncio
async def test_dm_unaffected_by_ownership_flag():
    a = _make_adapter()
    a._resolve_room_identity = AsyncMock(
        return_value=SimpleNamespace(
            display_name="dm", room_topic="", server_name="", chat_type="dm"
        )
    )
    out = await _ctx(
        a, body="привет", relates_to={"m.in_reply_to": {"event_id": "$x"}},
        sent_ids=[], room="!dm:example.org",
    )
    # DM never gates on mention at all
    assert out is not None


def test_parse_reply_ownership_defaults_off(monkeypatch):
    from plugins.platforms.matrix.adapter import MatrixAdapter
    monkeypatch.delenv("MATRIX_REPLY_OWNERSHIP", raising=False)
    cfg = SimpleNamespace(extra={})
    assert MatrixAdapter._parse_reply_ownership(cfg) is False
    cfg = SimpleNamespace(extra={"reply_ownership": "true"})
    assert MatrixAdapter._parse_reply_ownership(cfg) is True
    monkeypatch.setenv("MATRIX_REPLY_OWNERSHIP", "on")
    cfg = SimpleNamespace(extra={})
    assert MatrixAdapter._parse_reply_ownership(cfg) is True
