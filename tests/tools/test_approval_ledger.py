"""Durable pending-approval ledger (#133514).

Covers the dead-letter fix: a detached child's post-turn approval is
persisted, answerable through the SAME gateway entry points a live approval
uses (has_blocking_approval → /approve → resolve_gateway_approval), GC'd at
TTL, cancelled on session teardown — and completely inert (dict-only, no DB
writes) when the feature flag is off.
"""

import time

import pytest

import tools.approval as A
import tools.approval_ledger as L


@pytest.fixture
def ledger_on(monkeypatch):
    monkeypatch.setattr(L, "_enabled", lambda: True)
    monkeypatch.setattr(L, "_ttl_seconds", lambda: 1800)
    yield


def _rows(session_key):
    import sqlite3

    from hermes_constants import get_hermes_home

    conn = sqlite3.connect(get_hermes_home() / "state.db")
    try:
        return conn.execute(
            "SELECT state, resolution, command FROM pending_approvals"
            " WHERE session_key=? ORDER BY created_at",
            (session_key,),
        ).fetchall()
    finally:
        conn.close()


def _reset_process():
    A._pending.clear()
    A._gateway_queues.clear()
    A._session_approved.clear()


class TestRecord:
    def test_flag_off_writes_nothing_and_dict_still_works(self, monkeypatch):
        monkeypatch.setattr(L, "_enabled", lambda: False)
        _reset_process()
        A.submit_pending("s-off", {"command": "rm -rf /", "pattern_key": "pk"})
        assert "s-off" in A._pending  # legacy cache intact
        assert L.pending_count("s-off") == 0  # no DB touch

    def test_submit_persists_row(self, ledger_on):
        _reset_process()
        A.submit_pending(
            "s-1",
            {
                "command": "git push --force",
                "description": "force push",
                "pattern_key": "pk:force",
            },
        )
        assert _rows("s-1") == [("pending", None, "git push --force")]

    def test_multiple_rows_accumulate_in_db_despite_dict_overwrite(self, ledger_on):
        """The dict keeps its one-slot-per-session behavior (out of scope);
        the ledger must NOT lose the earlier request."""
        _reset_process()
        A.submit_pending("s-2", {"command": "cmd-A", "pattern_key": "pkA"})
        A.submit_pending("s-2", {"command": "cmd-B", "pattern_key": "pkB"})
        assert [r[2] for r in _rows("s-2")] == ["cmd-A", "cmd-B"]
        assert A._pending["s-2"]["command"] == "cmd-B"


class TestAnswerable:
    def test_gateway_entry_points_see_durable_rows(self, ledger_on):
        _reset_process()
        A.submit_pending(
            "s-3", {"command": "docker rm x", "description": "d", "pattern_key": "pk"}
        )
        # The exact chain /approve uses: gate -> restore snapshot -> resolve.
        assert A.has_blocking_approval("s-3") is True
        snap = A.get_pending_gateway_approval("s-3")
        assert snap["command"] == "docker rm x" and snap["durable"] is True
        assert A.pending_gateway_approval_count() >= 1

    def test_resolve_without_live_queue_answers_oldest_fifo(self, ledger_on):
        _reset_process()
        A.submit_pending("s-4", {"command": "first", "pattern_key": "pk1"})
        A.submit_pending("s-4", {"command": "second", "pattern_key": "pk2"})
        assert A.resolve_gateway_approval("s-4", "once") == 1
        assert _rows("s-4") == [
            ("approved", "once", "first"),
            ("pending", None, "second"),
        ]
        # ...then the second, then nothing left.
        assert A.resolve_gateway_approval("s-4", "once") == 1
        assert A.resolve_gateway_approval("s-4", "once") == 0

    def test_resolve_all_denies_every_pending(self, ledger_on):
        _reset_process()
        A.submit_pending("s-5", {"command": "a", "pattern_key": "pk"})
        A.submit_pending("s-5", {"command": "b", "pattern_key": "pk"})
        assert (
            A.resolve_gateway_approval("s-5", "deny", resolve_all=True, reason="nope")
            == 2
        )
        assert {r[0] for r in _rows("s-5")} == {"denied"}

    def test_session_choice_preapproves_pattern(self, ledger_on):
        _reset_process()
        A.submit_pending(
            "s-6", {"command": "kubectl apply -f x", "pattern_key": "pk:kubectl"}
        )
        assert A.resolve_gateway_approval("s-6", "session") == 1
        assert "pk:kubectl" in A._session_approved.get("s-6", set())

    def test_live_queue_wins_and_is_counted_together(self, ledger_on):
        _reset_process()
        A.submit_pending("s-7", {"command": "durable", "pattern_key": "pk"})

        class _Entry:
            def __init__(self):
                import threading

                self.data = {"command": "live", "request_id": "r1"}
                self.event = threading.Event()
                self.result = None
                self.reason = None

        A._gateway_queues["s-7"] = [_Entry()]
        assert A.resolve_gateway_approval("s-7", "once") == 2  # 1 live + 1 durable


class TestGc:
    def test_expired_rows_are_not_answerable(self, monkeypatch):
        monkeypatch.setattr(L, "_enabled", lambda: True)
        monkeypatch.setattr(L, "_ttl_seconds", lambda: 30)
        _reset_process()
        A.submit_pending("s-8", {"command": "old", "pattern_key": "pk"})
        import sqlite3

        from hermes_constants import get_hermes_home

        conn = sqlite3.connect(get_hermes_home() / "state.db")
        with conn:
            conn.execute(
                "UPDATE pending_approvals SET expires_at=? WHERE session_key='s-8'",
                (time.time() - 1,),
            )
        conn.close()
        assert A.has_blocking_approval("s-8") is False
        assert A.resolve_gateway_approval("s-8", "once") == 0
        assert _rows("s-8") == [("expired", None, "old")]

    def test_clear_session_cancels_pending_subtree(self, ledger_on):
        _reset_process()
        A.submit_pending("s-9", {"command": "x", "pattern_key": "pk"})
        A.clear_session("s-9")
        assert _rows("s-9") == [("cancelled", None, "x")]
        assert A.has_blocking_approval("s-9") is False


class TestDurability:
    def test_row_survives_connection_close(self, ledger_on):
        """Every op opens a fresh connection; persistence is the point."""
        _reset_process()
        A.submit_pending("s-10", {"command": "persist me", "pattern_key": "pk"})
        # Fresh read path (new conns, same as a restarted process would get).
        assert L.list_pending("s-10")[0]["command"] == "persist me"


class TestDegradation:
    def test_db_errors_never_break_the_tool_call(self, ledger_on, monkeypatch):
        _reset_process()

        def _boom():
            raise OSError("disk gone")

        monkeypatch.setattr(L, "_connect", _boom)
        A.submit_pending(
            "s-11", {"command": "c", "pattern_key": "pk"}
        )  # must not raise
        assert A._pending["s-11"]["command"] == "c"  # dict fallback served
        assert A.has_blocking_approval("s-11") is False  # ledger side degrades silently
