"""Backend coverage for session handoff/fork lineage.

Stage 2 added a ``handoff`` bool to ``_ChatSlot`` beside the existing
``forked_from`` parent key, so the sessions lineage view can label an edge as a
tangent/handoff vs a plain fork. These tests pin the load-bearing, deterministic
pieces: the slot default, the API projection, and the persistence round-trip —
each mirroring how ``forked_from`` is already treated.
"""

from __future__ import annotations

from kiro_crew.dashboard.state import _ChatSlot


def test_handoff_defaults_false() -> None:
    slot = _ChatSlot("s")
    assert slot.handoff is False
    assert slot.forked_from is None


def test_projection_emits_handoff() -> None:
    slot = _ChatSlot("child")
    slot.forked_from = "dashboard:parent"
    slot.handoff = True
    payload = slot.to_dict()
    assert payload["forked_from"] == "dashboard:parent"
    assert payload["handoff"] is True


def test_projection_emits_handoff_false_for_a_plain_fork() -> None:
    slot = _ChatSlot("child")
    slot.forked_from = "dashboard:parent"
    # A plain fork leaves handoff at its default.
    payload = slot.to_dict()
    assert payload["forked_from"] == "dashboard:parent"
    assert payload["handoff"] is False


def test_rehydrate_restores_handoff_from_meta() -> None:
    # Drive the REAL read site (_rehydrate_slot_from_history) rather than an
    # inline copy of its gate: a regression at chat_persistence.py's read must
    # fail this test. _prefetched_meta feeds the same dict the write side emits.
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.history import ConversationLog
    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history
    from kiro_crew.dashboard.state import DashboardState

    def _make_state(tmp: Path) -> DashboardState:
        sessions = MagicMock(count=0)
        sessions.get_pid = MagicMock(return_value=None)
        sessions.remove = AsyncMock()
        sessions.channel_key_for_stem = MagicMock(return_value=None)
        return DashboardState(
            sessions=sessions,
            crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp),
        )

    import tempfile

    with tempfile.TemporaryDirectory() as d:
        state = _make_state(Path(d))
        restored = _rehydrate_slot_from_history(
            state,
            "child",
            _prefetched_meta={"forked_from": "parent", "handoff": True},
            _prefetched_messages=[],
        )
        assert restored is not None
        assert restored.forked_from == "parent"
        assert restored.handoff is True


def test_rehydrate_defaults_handoff_false_for_meta_without_the_key() -> None:
    # Back-compat: meta predating the field (a plain fork, or an old transcript)
    # has no handoff key, so the real read leaves the slot at its default.
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.history import ConversationLog
    from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history
    from kiro_crew.dashboard.state import DashboardState

    def _make_state(tmp: Path) -> DashboardState:
        sessions = MagicMock(count=0)
        sessions.get_pid = MagicMock(return_value=None)
        sessions.remove = AsyncMock()
        sessions.channel_key_for_stem = MagicMock(return_value=None)
        return DashboardState(
            sessions=sessions,
            crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
            lessons=MagicMock(load_all=MagicMock(return_value=[])),
            start_time=0.0,
            conversation_log=ConversationLog(base_dir=tmp),
        )

    import tempfile

    with tempfile.TemporaryDirectory() as d:
        state = _make_state(Path(d))
        restored = _rehydrate_slot_from_history(
            state,
            "fork",
            _prefetched_meta={"forked_from": "parent"},
            _prefetched_messages=[],
        )
        assert restored is not None
        assert restored.forked_from == "parent"
        assert restored.handoff is False
