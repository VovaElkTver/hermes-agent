"""Fork/Join conversation branches: DB layer + transcript overlay tests.

Covers hermes_state_branches mixin and the fork-overlay read path in
get_messages_as_conversation. Design: ~/hermes-scheduler-study/fork-join-design.md.
"""

from __future__ import annotations

import pytest

from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    instance = SessionDB(db_path=tmp_path / "state.db")
    yield instance
    instance.close()


def _parent_with_history(db: SessionDB, n: int = 3) -> str:
    db.ensure_session("sess-parent", source="test", session_key="k-parent")
    for i in range(1, n + 1):
        role = "user" if i % 2 else "assistant"
        db.append_messages_batch("sess-parent", [dict(role=role, content=f"msg{i}")])
    return "sess-parent"


def _fork(db: SessionDB, parent_id: str, base_offset: int = 0):
    """Fork at ``base_offset`` rows from the end of the parent history."""
    rows = db._read_all(
        "SELECT message_uid FROM messages WHERE session_id = ? ORDER BY id",
        (parent_id,))
    base_uid = rows[len(rows) - 1 - base_offset]["message_uid"]
    child_key = db.session_key_with_branch_suffix("k-parent") \
        if hasattr(db, "session_key_with_branch_suffix") else "k-parent@btest"
    db.ensure_session(f"{parent_id}-child", source="test", session_key=child_key,
                      parent_session_id=parent_id)
    branch_id = db.new_branch_id()
    db.create_branch_row(branch_id, parent_id, f"{parent_id}-child", base_uid)
    return branch_id, f"{parent_id}-child"


class TestBranchCrud:
    def test_create_and_get(self, db: SessionDB):
        _parent_with_history(db)
        db.ensure_session("child", source="test", session_key="k@b1",
                          parent_session_id="sess-parent")
        bid = db.new_branch_id()
        db.create_branch_row(bid, "sess-parent", "child", None)
        row = db.get_branch(bid)
        assert row["status"] == "active"
        assert row["parent_session_id"] == "sess-parent"
        assert db.get_branch_for_child("child")["branch_id"] == bid
        assert db.get_branch_for_child("nonexistent") is None

    def test_create_requires_sessions_rows(self, db: SessionDB):
        # FK enforcement: both sessions must exist first
        db.ensure_session("p", source="t", session_key="kp")
        db.ensure_session("c", source="t", session_key="kc", parent_session_id="p")
        bid = db.new_branch_id()
        db.create_branch_row(bid, "p", "c", None)
        assert db.get_branch(bid) is not None

    def test_status_lifecycle(self, db: SessionDB):
        _parent_with_history(db)
        db.ensure_session("child", source="test", session_key="k@b2",
                          parent_session_id="sess-parent")
        bid = db.new_branch_id()
        db.create_branch_row(bid, "sess-parent", "child", None)
        assert db.has_active_branches("sess-parent")
        assert db.set_branch_status(bid, "joined", join_strategy="note")
        assert not db.has_active_branches("sess-parent")
        # second transition from joined is refused
        assert not db.set_branch_status(bid, "abandoned")

    def test_invalid_status_rejected(self, db: SessionDB):
        _parent_with_history(db)
        db.ensure_session("child", source="test", session_key="k@b3",
                          parent_session_id="sess-parent")
        bid = db.new_branch_id()
        db.create_branch_row(bid, "sess-parent", "child", None)
        with pytest.raises(ValueError):
            db.set_branch_status(bid, "bogus")

    def test_multiple_active_listed_oldest_first(self, db: SessionDB):
        _parent_with_history(db)
        for n in ("a", "b"):
            db.ensure_session(f"child-{n}", source="test", session_key=f"k@b{n}",
                              parent_session_id="sess-parent")
            db.create_branch_row(db.new_branch_id(), "sess-parent",
                                 f"child-{n}", None)
        active = db.list_active_branches("sess-parent")
        assert [a["child_session_id"] for a in active] == ["child-a", "child-b"]


class TestWatermarkFloor:
    def test_floor_is_min_of_active_bases(self, db: SessionDB):
        _parent_with_history(db, n=5)
        floor_ids = []
        for offset in (4, 1):  # two branches pinned at different depth
            rows = db._read_all(
                "SELECT id, message_uid FROM messages WHERE session_id = ? ORDER BY id",
                ("sess-parent",))
            base = rows[len(rows) - 1 - offset]["message_uid"]
            db.ensure_session(f"cw{offset}", source="t", session_key=f"kcw{offset}",
                              parent_session_id="sess-parent")
            db.create_branch_row(db.new_branch_id(), "sess-parent", f"cw{offset}", base)
            floor_ids.append(rows[len(rows) - 1 - offset]["id"])
        assert db.min_active_base_rowid("sess-parent") == min(floor_ids)

    def test_no_active_branches_no_floor(self, db: SessionDB):
        _parent_with_history(db)
        assert db.min_active_base_rowid("sess-parent") is None

    def test_joined_branch_releases_floor(self, db: SessionDB):
        _parent_with_history(db, n=3)
        rows = db._read_all(
            "SELECT id, message_uid FROM messages WHERE session_id = ? ORDER BY id",
            ("sess-parent",))
        base = rows[0]["message_uid"]
        db.ensure_session("cwj", source="t", session_key="kcwj",
                          parent_session_id="sess-parent")
        bid = db.new_branch_id()
        db.create_branch_row(bid, "sess-parent", "cwj", base)
        assert db.min_active_base_rowid("sess-parent") == rows[0]["id"]
        db.set_branch_status(bid, "abandoned")
        assert db.min_active_base_rowid("sess-parent") is None


class TestTranscriptOverlay:
    def test_child_reads_parent_prefix_plus_own(self, db: SessionDB):
        _parent_with_history(db)
        _, child = _fork(db, "sess-parent")
        db.append_messages_batch(child, [dict(role="user", content="branch-only Q")])
        history = db.get_messages_as_conversation(child)
        contents = [str(m["content"]) for m in history]
        assert any("msg1" in c for c in contents), "parent prefix missing"
        assert any("branch-only" in c for c in contents)
        # ordering: parent prefix strictly before own rows
        assert contents.index("branch-only Q") > max(
            i for i, c in enumerate(contents) if "msg" in c)

    def test_parent_does_not_see_branch_rows(self, db: SessionDB):
        _parent_with_history(db)
        _, child = _fork(db, "sess-parent")
        db.append_messages_batch(child, [dict(role="user", content="branch-only Q")])
        hp = db.get_messages_as_conversation("sess-parent")
        assert not any("branch-only" in str(m["content"]) for m in hp)

    def test_join_turns_off_overlay(self, db: SessionDB):
        _parent_with_history(db)
        bid, child = _fork(db, "sess-parent")
        db.append_messages_batch(child, [dict(role="user", content="branch-only Q")])
        db.set_branch_status(bid, "joined", join_strategy="note")
        h = db.get_messages_as_conversation(child)
        assert not any("msg1" in str(m["content"]) for m in h)

    def test_base_lost_degrades_to_plain(self, db: SessionDB):
        _parent_with_history(db)
        bid, child = _fork(db, "sess-parent")
        # simulate compaction eating the parent prefix
        db._execute_write(lambda conn: conn.execute(
            "UPDATE messages SET message_uid = NULL WHERE session_id = 'sess-parent'"),
            patience_s=5)
        _, base_uid_after_loss = db._fork_overlay_parent(child)
        assert base_uid_after_loss  # still registered
        h = db.get_messages_as_conversation(child)
        assert not any("msg" in str(m["content"]) for m in h), \
            "lost base must not resurrect parent history"
        assert len(h) == 0  # child appended nothing of its own
