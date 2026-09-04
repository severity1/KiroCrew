"""The handoff-offer trigger: maybe_arm_handoff_offer + the one-shot flag.

Mirrors ``test_session_autocompact_override``'s ``_manager()`` shape. The
trigger arms a one-shot nudge when usage enters the band
``handoff_offer_pct <= pct < effective_autocompact_pct``, latches so it fires
once per band-entry (not every turn), and re-arms only after usage drops back
below the offer threshold (which a compaction or a fresh handoff does).
"""

from __future__ import annotations

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.session import SessionManager, _Session


class _Provider:
    """Minimal provider double reporting a fixed, KNOWN context percentage.

    No ``context_usage_unknown`` method, so ``_context_pct_is_unknown`` reads it
    as trustworthy — the arming path must not skip on a known reading.
    """

    def __init__(self, pct: float) -> None:
        self._pct = pct

    def context_usage_pct(self) -> float:
        return self._pct


def _manager(**session_over: object) -> SessionManager:
    cfg = KiroCrewConfig()
    for k, v in session_over.items():
        setattr(cfg.session, k, v)
    return SessionManager(cfg, provider_factory=lambda *a, **k: object())


def _register(mgr: SessionManager, key: str, provider: _Provider) -> _Session:
    sess = _Session(provider=provider)  # type: ignore[arg-type]
    mgr._sessions[mgr._fold_key(key)] = sess
    return sess


def _band_pct(mgr: SessionManager) -> float:
    """A percentage strictly inside the default offer band."""
    return (mgr._cfg.session.handoff_offer_pct + mgr._cfg.session.autocompact_pct) / 2.0


class TestArmHandoffOffer:
    def test_arms_the_one_shot_flag_inside_the_band(self) -> None:
        mgr = _manager()
        prov = _Provider(_band_pct(mgr))
        sess = _register(mgr, "s", prov)
        mgr.maybe_arm_handoff_offer("s", prov)
        assert sess.handoff_offer_pending is True
        assert sess.handoff_offered is True

    def test_does_not_re_arm_while_still_in_band(self) -> None:
        # The latch keeps the nudge from firing every turn: after the first arm
        # is consumed, a second call in-band must NOT re-arm.
        mgr = _manager()
        prov = _Provider(_band_pct(mgr))
        sess = _register(mgr, "s", prov)
        mgr.maybe_arm_handoff_offer("s", prov)
        assert mgr.consume_handoff_offer_pending("s") is True  # consumed one-shot
        mgr.maybe_arm_handoff_offer("s", prov)
        assert sess.handoff_offer_pending is False

    def test_re_arms_after_usage_drops_below_offer_threshold(self) -> None:
        # A compaction/handoff resets context below the offer threshold; the
        # latch clears there so the next band-entry offers again.
        mgr = _manager()
        sess = _register(mgr, "s", _Provider(_band_pct(mgr)))
        mgr.maybe_arm_handoff_offer("s", _Provider(_band_pct(mgr)))
        assert mgr.consume_handoff_offer_pending("s") is True
        # Drop below the offer floor — clears the latch.
        mgr.maybe_arm_handoff_offer("s", _Provider(mgr._cfg.session.handoff_offer_pct - 5.0))
        assert sess.handoff_offered is False
        # Re-enter the band — arms again.
        mgr.maybe_arm_handoff_offer("s", _Provider(_band_pct(mgr)))
        assert sess.handoff_offer_pending is True

    def test_does_not_arm_below_the_offer_threshold(self) -> None:
        mgr = _manager()
        sess = _register(mgr, "s", _Provider(mgr._cfg.session.handoff_offer_pct - 1.0))
        mgr.maybe_arm_handoff_offer("s", _Provider(mgr._cfg.session.handoff_offer_pct - 1.0))
        assert sess.handoff_offer_pending is False

    def test_does_not_arm_at_or_above_autocompact_threshold(self) -> None:
        # In compaction territory the autocompactor owns the band; no offer.
        mgr = _manager()
        pct = mgr._cfg.session.autocompact_pct + 1.0
        sess = _register(mgr, "s", _Provider(pct))
        mgr.maybe_arm_handoff_offer("s", _Provider(pct))
        assert sess.handoff_offer_pending is False

    def test_arms_exactly_at_the_offer_threshold(self) -> None:
        # Lower boundary: pct == offer_pct is INSIDE the band (falls past the
        # `pct < offer_pct` clear). Locks the `>=` half of the complementarity.
        mgr = _manager()
        pct = mgr._cfg.session.handoff_offer_pct
        sess = _register(mgr, "s", _Provider(pct))
        mgr.maybe_arm_handoff_offer("s", _Provider(pct))
        assert sess.handoff_offer_pending is True

    def test_does_not_arm_exactly_at_autocompact_threshold(self) -> None:
        # Upper boundary: pct == autocompact_pct is OWNED by the compaction gate
        # (its check is `pct < autocompact -> below_threshold`, so it compacts at
        # ==). The offer must NOT arm there — exactly one subsystem acts at the
        # shared boundary. Locks the `<` half of the complementarity so a refactor
        # flipping `>=`/`<` is caught.
        mgr = _manager()
        pct = mgr._cfg.session.autocompact_pct
        sess = _register(mgr, "s", _Provider(pct))
        mgr.maybe_arm_handoff_offer("s", _Provider(pct))
        assert sess.handoff_offer_pending is False

    def test_offer_disabled_when_pct_is_zero(self) -> None:
        # handoff_offer_pct == 0 disables the offer entirely.
        mgr = _manager(handoff_offer_pct=0.0)
        sess = _register(mgr, "s", _Provider(50.0))
        mgr.maybe_arm_handoff_offer("s", _Provider(50.0))
        assert sess.handoff_offer_pending is False

    def test_does_not_arm_on_zero_or_unknown_pct(self) -> None:
        # A 0/unknown reading must not arm an offer on a possibly-empty session.
        mgr = _manager()
        sess = _register(mgr, "s", _Provider(0.0))
        mgr.maybe_arm_handoff_offer("s", _Provider(0.0))
        assert sess.handoff_offer_pending is False

    def test_absent_session_is_a_noop(self) -> None:
        mgr = _manager()
        # No session registered — must not raise.
        mgr.maybe_arm_handoff_offer("ghost", _Provider(_band_pct(mgr)))


class TestOneShotConsume:
    def test_consume_reads_and_clears(self) -> None:
        mgr = _manager()
        sess = _register(mgr, "s", _Provider(_band_pct(mgr)))
        mgr.mark_handoff_offer_pending("s")
        assert sess.handoff_offer_pending is True
        assert mgr.consume_handoff_offer_pending("s") is True
        assert sess.handoff_offer_pending is False
        # Second consume returns False — it was one-shot.
        assert mgr.consume_handoff_offer_pending("s") is False

    def test_consume_on_absent_session_is_false(self) -> None:
        mgr = _manager()
        assert mgr.consume_handoff_offer_pending("ghost") is False

    def test_adopt_provider_resets_both_flags(self) -> None:
        # The runtime warm-pool re-bind must scrub the offer state, like it does
        # for needs_context_reinjection (issue #2932 shape).
        mgr = _manager()
        sess = _register(mgr, "s", _Provider(_band_pct(mgr)))
        sess.handoff_offer_pending = True
        sess.handoff_offered = True
        sess.adopt_provider(_Provider(0.0))  # type: ignore[arg-type]
        assert sess.handoff_offer_pending is False
        assert sess.handoff_offered is False
