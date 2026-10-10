"""RECEPT-reply-context-v2: Matrix adapter fetches the full original of a quoted event.

Covers the spec's table: inline-only (no GET), GET path, media labels (C4), E2EE decrypt (C3),
crypto-missing/timeout silence (C2), is_own (C6), and the 2000-char helper slice (C5).
Fake client is injected as ``adapter._client`` — the helper's single source by contract.
"""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("mautrix", reason="adapter test uses the real mautrix types")

from plugins.platforms.matrix.adapter import MatrixAdapter  # noqa: E402

MY_MXID = "@bot:example.org"
OTHER = "@alice:example.org"


class FakeClient:
    def __init__(self, event=None, raises=None, crypto=None):
        self._event, self._raises, self.crypto = event, raises, crypto
        self.get_event_calls = 0

    async def get_event(self, room_id, event_id):
        self.get_event_calls += 1
        if self._raises:
            raise self._raises
        return self._event

    state_store = None


def _adapter(fake, user_id=MY_MXID):
    a = MatrixAdapter.__new__(MatrixAdapter)  # field injection only; no transport needed
    a._client = fake
    a._user_id = user_id
    return a


def _event(msgtype="m.text", body="original text", sender=OTHER, type_="m.room.message"):
    return SimpleNamespace(type=type_, sender=sender,
                           content=SimpleNamespace(msgtype=msgtype, body=body))  # type: ignore[arg-type]


async def _fetch(a, room="!r:example.org", evid="$orig"):
    return await a._fetch_reply_origin(room, evid)


# 1. inline quote present -> _extract_reply_context must not GET at all (C1)
@pytest.mark.asyncio
async def test_inline_quote_uses_no_get():
    fake = FakeClient(event=_event(body="should never be fetched"))
    a = _adapter(fake)
    async def dn(room_id, user_id):
        return "Alice"
    a._get_display_name = dn
    body, rid, text, author, name, is_own = await a._extract_reply_context(
        "!r:example.org", "> <@alice:example.org> inline quote\n\nmy reply",
        {"m.in_reply_to": {"event_id": "$orig"}})
    assert fake.get_event_calls == 0
    assert text == "inline quote" and author == OTHER and not is_own
    assert body == "my reply"  # C7: fallback stripped from the body


@pytest.mark.asyncio
async def test_no_quote_plain_text_fetch():
    a = _adapter(FakeClient(event=_event(body="the original")))
    text, author, name, is_own = await _fetch(a)
    assert text == "the original" and author == OTHER and not is_own


# 3. media originals (C4)
@pytest.mark.asyncio
async def test_media_caption_label():
    a = _adapter(FakeClient(event=_event(msgtype="m.image", body="Счёт 12")))
    text, *_ = await _fetch(a)
    assert text == "[m.image] Счёт 12"


@pytest.mark.asyncio
async def test_media_junk_filename_label_only():
    a = _adapter(FakeClient(event=_event(msgtype="m.image", body="IMG_2045.JPG")))
    text, *_ = await _fetch(a)
    assert text == "[m.image]"


# 4/5. E2EE (C3): decrypt via our crypto; no crypto -> silence (C2)
@pytest.mark.asyncio
async def test_encrypted_with_crypto_decrypts():
    plain = _event(body="secret original")
    enc = _event(type_="m.room.encrypted", body=None, msgtype=None)
    class FakeCrypto:
        async def decrypt_megolm_event(self, evt):
            return plain
    a = _adapter(FakeClient(event=enc, crypto=FakeCrypto()))
    text, author, *_ = await _fetch(a)
    assert text == "secret original" and author == OTHER


@pytest.mark.asyncio
async def test_encrypted_without_crypto_is_quiet():
    enc = _event(type_="m.room.encrypted", body=None, msgtype=None)
    a = _adapter(FakeClient(event=enc, crypto=None))
    text, author, name, is_own = await _fetch(a)
    assert (text, author, name, is_own) == (None, None, None, False)


# 6. GET failure never propagates (C2)
@pytest.mark.asyncio
async def test_timeout_is_quiet():
    a = _adapter(FakeClient(raises=asyncio.TimeoutError()))
    text, author, name, is_own = await _fetch(a)
    assert (text, author, name, is_own) == (None, None, None, False)


# 7. own original (C6)
@pytest.mark.asyncio
async def test_is_own_when_sender_is_me():
    a = _adapter(FakeClient(event=_event(body="my earlier reply", sender=MY_MXID)))
    text, author, name, is_own = await _fetch(a)
    assert is_own is True and author == MY_MXID


# 8. helper slice at 2000 (C5; the 500 render slice is run_inbound's)
@pytest.mark.asyncio
async def test_helper_slices_at_2000():
    a = _adapter(FakeClient(event=_event(body="x" * 5000)))
    text, *_ = await _fetch(a)
    assert len(text) == 2000


# inline branch also feeds is_own (C6 on the inline path)
@pytest.mark.asyncio
async def test_inline_own_quote_flags_is_own():
    fake = FakeClient()
    a = _adapter(fake)
    async def dn(room, uid):
        return "Bot"
    a._get_display_name = dn
    *_, is_own = await a._extract_reply_context(
        "!r:example.org", f"> <{MY_MXID}> bot said hi\n\nok", {"m.in_reply_to": {"event_id": "$o"}})
    assert is_own is True
