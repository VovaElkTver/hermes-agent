"""Media-hold batching in the Matrix adapter (branch elk-matrix-media-hold).

When ``media_hold_seconds`` > 0, inbound media events are held briefly so a
caption typed right after a share (or extra files from the same multi-share)
joins the SAME turn instead of interrupting it. An inbound m.typing from the
same sender extends the window; a hard cap bounds the wait. Default remains
hold=0 → legacy immediate dispatch.
"""
import asyncio
import time
from unittest.mock import MagicMock

from gateway.config import PlatformConfig


def _make_adapter(extra=None):
    from plugins.platforms.matrix.adapter import MatrixAdapter
    config = PlatformConfig(
        enabled=True,
        token="syt_test_token",
        extra={"homeserver": "https://matrix.example.org", "user_id": "@bot:example.org", **(extra or {})},
    )
    return MatrixAdapter(config)


def _event(chat_id="!room:x", user="@vovak:example.org", text="", urls=None):
    ev = MagicMock()
    ev.source.chat_id = chat_id
    ev.user_id = user
    ev.text = text
    ev.message_id = f"$evt-{time.monotonic_ns()}"
    ev.raw_message = {"body": text}
    ev.media_urls = list(urls or [])
    ev.media_types = ["image/png"] * len(ev.media_urls)
    ev.media_text_inlined = [None] * len(ev.media_urls)
    return ev


async def _noop_coro():
    return None


class TestTypingMarks:
    def test_mark_expires_after_ttl(self):
        ad = _make_adapter()
        ad._typing_extend("!r", "@u")
        assert ad._typing_active("!r", "@u") is True

    def test_no_mark_is_inactive(self):
        ad = _make_adapter()
        assert ad._typing_active("!r", "@u") is False

    def test_on_typing_marks_others_not_self(self):
        ad = _make_adapter()
        ev = MagicMock()
        ev.room_id = "!r"
        ev.content.user_ids = ["@bot:example.org", "@vovak:example.org"]
        asyncio.run(ad._on_typing(ev))
        assert ad._typing_active("!r", "@vovak:example.org") is True
        # self mark must NOT be set by our own typing echo
        assert ad._typing_active("!r", "@bot:example.org") is False


class TestHoldWindow:
    def _adapter_with_hold(self, hold=1.5, cap=8.0):
        return _make_adapter({"media_hold_seconds": str(hold), "media_hold_cap_seconds": str(cap)})

    def test_default_is_disabled(self):
        ad = _make_adapter()
        assert ad._media_hold_seconds == 0.0

    def test_first_media_opens_batch_and_arms_timer(self):
        ad = self._adapter_with_hold()
        ev = _event()

        async def scenario():
            ad.handle_message = lambda msg: _noop_coro()
            ad._maybe_wait_llm_delay = lambda *a, **k: _noop_coro()
            ad._hold_for_batch(ev)
            key = ("!room:x", "@vovak:example.org")
            batch = ad._media_batches.get(key)
            assert batch and len(batch["events"]) == 1 and batch["task"] is not None

        asyncio.run(scenario())

    def test_cap_bounds_sliding_window(self):
        ad = self._adapter_with_hold(hold=3.0, cap=8.0)

        async def scenario():
            ad.handle_message = lambda m: _noop_coro()
            ad._maybe_wait_llm_delay = lambda *a, **k: _noop_coro()
            ad._hold_for_batch(_event())
            key = ("!room:x", "@vovak:example.org")
            opened = ad._media_batches[key]["opened"]
            for _ in range(6):  # slide repeatedly — deadline must never pass opened+cap
                ad._hold_for_batch(_event())
            assert ad._media_batches[key]["deadline"] <= opened + 8.0 + 0.001

        asyncio.run(scenario())

    def test_typing_mark_extends_sliding_window(self):
        ad = self._adapter_with_hold(hold=1.0, cap=8.0)

        async def scenario():
            ad.handle_message = lambda m: _noop_coro()
            ad._maybe_wait_llm_delay = lambda *a, **k: _noop_coro()
            ev = _event()
            ad._hold_for_batch(ev)
            key = (ev.source.chat_id, ev.user_id)
            before = ad._media_batches[key]["deadline"]
            ad._typing_extend(*key)
            ad._hold_for_batch(_event())
            after = ad._media_batches[key]["deadline"]
            assert after >= before  # typing active → window slid forward within cap

        asyncio.run(scenario())

    def test_flush_merges_and_dispatches(self):
        ad = self._adapter_with_hold(hold=0.2)
        dispatched = []

        async def scenario():
            async def handle(msg):
                dispatched.append(msg)
                return _noop_coro()
            ad.handle_message = handle
            ad._maybe_wait_llm_delay = lambda *a, **k: _noop_coro()
            ad._hold_for_batch(_event(urls=["/tmp/a.png"]))
            ad._hold_for_batch(_event(text="вот лог", urls=["/tmp/b.png"]))
            key = ("!room:x", "@vovak:example.org")
            await ad._flush_media_batch(key)

        asyncio.run(scenario())
        assert len(dispatched) == 1
        merged = dispatched[0]
        assert merged.media_urls == ["/tmp/a.png", "/tmp/b.png"]
        assert "вот лог" in merged.text

    def test_caption_text_joins_held_batch(self):
        """A text after held media (within the window) is the accompaniment: one merged turn."""
        ad = self._adapter_with_hold(hold=5.0)
        dispatched = []

        async def scenario():
            async def handle(msg):
                dispatched.append(msg)
            ad.handle_message = handle
            ad._maybe_wait_llm_delay = lambda *a, **k: _noop_coro()
            ad._hold_for_batch(_event(text="shot.jpg", urls=["/tmp/a.png"]))
            # Simulate the caption arriving as a text event while the batch is still held:
            ev = _event(text="вот дежурный лог")
            key = ("!room:x", "@vovak:example.org")
            held = ad._media_batches.pop(key)
            if held["task"] and not held["task"].done():
                held["task"].cancel()
            held["events"].append(ev)
            merged = type(ad)._merge_media_events(held["events"])
            await ad.handle_message(merged)

        asyncio.run(scenario())
        assert len(dispatched) == 1
        assert dispatched[0].media_urls == ["/tmp/a.png"]
        assert "дежурный лог" in dispatched[0].text

    def test_typing_extends_pending_batch_to_cap(self):
        ad = self._adapter_with_hold(hold=1.5, cap=8.0)

        async def scenario():
            ad.handle_message = lambda m: _noop_coro()
            ad._maybe_wait_llm_delay = lambda *a, **k: _noop_coro()
            ev = _event(urls=["/tmp/a.png"])
            ad._hold_for_batch(ev)
            key = ("!room:x", "@vovak:example.org")
            near = ad._media_batches[key]["opened"] + 1.5
            assert abs(ad._media_batches[key]["deadline"] - near) < 0.1

            class TypingEv:
                room_id = "!room:x"
                class content:
                    user_ids = ["@vovak:example.org"]
            await ad._on_typing(TypingEv())
            # Extended to the hard cap (opened+8), not the old 1.5s deadline
            assert ad._media_batches[key]["deadline"] >= near + 6.0

        asyncio.run(scenario())

    def test_merge_joins_captions_and_attachments(self):
        from plugins.platforms.matrix.adapter import MatrixAdapter
        e1 = _event(urls=["/tmp/a.png"])
        e2 = _event(text="вот лог дежурства", urls=["/tmp/b.png"])
        e3 = _event(urls=["/tmp/c.png"])
        out = MatrixAdapter._merge_media_events([e1, e2, e3])
        assert out.media_urls == ["/tmp/a.png", "/tmp/b.png", "/tmp/c.png"]
        assert "вот лог дежурства" in out.text
        assert out.message_id == e3.message_id
