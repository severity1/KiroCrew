"""Pins the session.handoff_offer_pct default and its load/write bound parity.

The knob decides when the agent is nudged to OFFER a session handoff, in the
band ``handoff_offer_pct <= pct < autocompact_pct``. Three things are pinned,
mirroring ``test_autocompact_default`` because the number alone is not the
invariant:

- the value reached by the path a real install takes (``load()`` reading a
  config file, NOT ``SessionConfig()``);
- that the default sits strictly BELOW the compaction default, since an offer at
  or above the compaction threshold never gets a turn to fire before the
  autocompactor takes over;
- that the load-time clamp and the dashboard write gate share one constant pair,
  so a hand-edited config.json and an API write cannot disagree.
"""

from __future__ import annotations

import json
import tempfile
import unittest.mock
from pathlib import Path

from kiro_crew.config.loader import (
    DEFAULT_AUTOCOMPACT_PCT,
    DEFAULT_HANDOFF_OFFER_PCT,
    HANDOFF_OFFER_PCT_MAX,
    HANDOFF_OFFER_PCT_MIN,
    KiroCrewConfig,
    SessionConfig,
)
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG


def _load_with_session(session_block: dict) -> KiroCrewConfig:
    """Load config from a temp file holding *session_block* (real install path)."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "config.json"
        path.write_text(json.dumps({"session": session_block}))
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=path):
            return KiroCrewConfig.load()


def test_default_is_reached_by_the_load_path() -> None:
    cfg = _load_with_session({})
    assert cfg.session.handoff_offer_pct == DEFAULT_HANDOFF_OFFER_PCT


def test_dataclass_default_matches_the_constant() -> None:
    assert SessionConfig().handoff_offer_pct == DEFAULT_HANDOFF_OFFER_PCT


def test_default_sits_strictly_below_the_compaction_default() -> None:
    # An offer band at or above the compaction threshold can never fire: the
    # autocompactor takes the turn first. The two defaults must not collide.
    assert DEFAULT_HANDOFF_OFFER_PCT < DEFAULT_AUTOCOMPACT_PCT


def test_default_is_inside_its_validated_range() -> None:
    assert HANDOFF_OFFER_PCT_MIN <= DEFAULT_HANDOFF_OFFER_PCT <= HANDOFF_OFFER_PCT_MAX


def test_ceiling_is_below_the_autocompact_ceiling() -> None:
    # The static bound cannot see a per-install autocompact_pct, so the ceiling
    # is one point below the autocompact maximum as a coarse guard; the exact
    # cross-field "< autocompact_pct" check lives in the consumer.
    assert HANDOFF_OFFER_PCT_MAX < 90.0


def test_zero_disables_and_survives_the_load_clamp() -> None:
    # 0 is a valid value (disables the offer), so the floor is 0, not 5.
    cfg = _load_with_session({"handoff_offer_pct": 0})
    assert cfg.session.handoff_offer_pct == 0.0


def test_out_of_range_high_clamps_on_load() -> None:
    cfg = _load_with_session({"handoff_offer_pct": 500})
    assert cfg.session.handoff_offer_pct == HANDOFF_OFFER_PCT_MAX


def test_negative_clamps_to_floor_on_load() -> None:
    cfg = _load_with_session({"handoff_offer_pct": -10})
    assert cfg.session.handoff_offer_pct == HANDOFF_OFFER_PCT_MIN


def test_write_gate_shares_the_load_clamp_bounds() -> None:
    # One constant pair for both gates, so a hand-edited file and an API write
    # cannot drift (the invariant test_autocompact_default guards for its knob).
    spec = _EDITABLE_CONFIG["session.handoff_offer_pct"]
    assert spec["type"] == "float"
    assert spec["min"] == HANDOFF_OFFER_PCT_MIN
    assert spec["max"] == HANDOFF_OFFER_PCT_MAX


def test_warns_when_offer_pct_is_at_or_above_autocompact(caplog) -> None:
    # Safe-but-silent footgun: an offer threshold >= the compaction threshold
    # makes the band empty (offer never fires). Warn so the operator sees why.
    import logging

    with caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"):
        _load_with_session({"handoff_offer_pct": 75, "autocompact_pct": 70})
    assert any("handoff_offer_pct" in r.message for r in caplog.records)


def test_no_warning_when_offer_pct_is_below_autocompact() -> None:
    # The normal case (default 55 < 70) must not warn.
    import logging

    logger = logging.getLogger("kiro_crew.config.loader")
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    sink = _Sink()
    logger.addHandler(sink)
    try:
        _load_with_session({"handoff_offer_pct": 55, "autocompact_pct": 70})
    finally:
        logger.removeHandler(sink)
    assert not any("will never fire because the band is empty" in r.getMessage() for r in records)


def test_no_warning_when_offer_disabled_at_zero() -> None:
    # 0 is the deliberate "disabled" value, not a misconfiguration — no warning
    # even though 0 < autocompact.
    import logging

    logger = logging.getLogger("kiro_crew.config.loader")
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    sink = _Sink()
    logger.addHandler(sink)
    try:
        _load_with_session({"handoff_offer_pct": 0, "autocompact_pct": 70})
    finally:
        logger.removeHandler(sink)
    assert not any("will never fire because the band is empty" in r.getMessage() for r in records)
