"""``kirocrew-cron`` resolves the calling session from the injected caller block.

The server used to read identity from its own process environment. On a pooled
backend one process serves many sessions, so process environment can only ever
name one of them -- and gatewayd forwards no session-identifying variable to a
shared backend at all, so what it actually read there was EMPTY. Every
session-scoped path then took the empty branch, and those branches disagreed with
each other: the per-job ownership gate allowed, ``cron_list`` skipped its filter,
``cron_add`` stored an ownerless row, and only ``cron_remove_all`` refused. Two of
those are fail-open, which made the ownership gate dead code for exactly the
callers it exists to separate.

What these tests pin, in the order the fix depends on them:

1. The caller block WINS over the process environment -- asserted with the
   environment naming a DIFFERENT session, because a test that merely reads the
   block back would also pass if the block were being ignored in favour of an
   environment that happened to agree.
2. With no block, the environment still resolves, so a non-gateway launch is not
   regressed.
3. The forgeable source is no longer consulted for an authorization decision.
4. One rule for an unidentifiable caller: reads work, writes refuse.
5. Ownerless rows written before the fix stay reachable, and no new one can be
   created -- which is what lets the grandfather clause drain.
"""

from __future__ import annotations

import uuid

import pytest

from kiro_crew import mcp_cron, session_pid_sig
from kiro_crew.cron import CronService
from kiro_crew.mcp_caller import CallerContext, set_current_caller
from kiro_crew.mcp_cron import _authz_session_key, _call_tool_inner

_MUTATING_TOOLS = ("cron_update", "cron_remove", "cron_pause", "cron_resume", "cron_trigger")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """A private cron store, and no ambient identity of any kind.

    Identity is granted per test rather than by a shared fixture: half of this
    module is about what happens when there is none.
    """
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    monkeypatch.setattr("kiro_crew.mcp_cron.config_dir", lambda: tmp_path)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    monkeypatch.delenv("KIROCREW_CHANNEL_ID", raising=False)
    monkeypatch.delenv("KIROCREW_CLI", raising=False)
    set_current_caller(None)
    yield tmp_path
    set_current_caller(None)


def _as_session(key: str, *, channel: str = "") -> None:
    """Arrive the way a pooled forwarded call does: carrying a caller block."""
    set_current_caller(
        CallerContext(session_key=key, session_type="dashboard", channel_id=channel)
    )


def _add_job(name: str) -> str:
    result = _call_tool_inner("cron_add", {"name": name, "message": "go", "every": 120})
    assert "Added job" in result, result
    return result.split()[2]


# --- 1..3: where identity comes from ---------------------------------------


def test_the_caller_block_beats_the_process_environment(monkeypatch) -> None:
    """The whole bug in one assertion.

    The environment names a DIFFERENT session than the block. A pooled backend's
    environment can only ever name one session (or, in practice, none), so the
    block has to outrank it rather than merely be consulted.
    """
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:from-env")
    _as_session("dashboard:from-block")

    assert _authz_session_key() == "dashboard:from-block"


def test_no_caller_block_falls_back_to_the_process_environment(monkeypatch) -> None:
    """A non-gateway launch has no block to read and must not be regressed."""
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:from-env")

    assert _authz_session_key() == "dashboard:from-env"


def test_the_forgeable_pid_walk_is_not_an_authorization_source(monkeypatch) -> None:
    """``session_pid_<pid>.txt`` must not decide who may delete whose job.

    ``mcp_core`` documents that file as "agent-writable and therefore forgeable",
    and the LENIENT resolver ends its fallback chain by walking ``/proc``
    ancestors over it. Labelling an audit row from it is tolerable; an
    authorization decision is not. Proven by making the walk succeed loudly and
    asserting the answer is still empty.
    """
    monkeypatch.setattr(
        session_pid_sig, "read_session_pid_txt", lambda *a, **k: "dashboard:forged"
    )

    assert _authz_session_key() == ""


# --- 4: one rule for a caller the gateway cannot name ----------------------


@pytest.mark.parametrize("tool", _MUTATING_TOOLS)
def test_an_unidentified_caller_cannot_write(tool: str, monkeypatch) -> None:
    """Every mutating tool gives the same refusal.

    An unidentified caller can still be sharing a pooled backend with identified
    ones: gatewayd forwards with ``caller=None`` when the stub's Register carries
    no session key and peer resolution fails, so "no identity" does NOT imply a
    1:1 transport. Granting it authority over stored rows would reach another
    session's jobs.
    """
    _as_session("dashboard:owner")
    job_id = _add_job(f"unident-{uuid.uuid4().hex[:8]}")
    set_current_caller(None)

    result = _call_tool_inner(tool, {"job_id": job_id})

    assert "cannot determine which session is calling" in result
    # The row is untouched -- a refusal, not a partial write.
    assert CronService(base_dir=mcp_cron.config_dir()).get_job(job_id) is not None


def test_an_unidentified_caller_cannot_create() -> None:
    """``cron_add`` refuses rather than minting another ownerless row."""
    result = _call_tool_inner(
        "cron_add", {"name": f"anon-{uuid.uuid4().hex[:8]}", "message": "go", "every": 120}
    )

    assert "cannot determine which session is calling" in result
    assert CronService(base_dir=mcp_cron.config_dir()).list_jobs(include_disabled=True) == []


def test_an_unidentified_caller_can_still_read() -> None:
    """Reads stay open: an unnameable caller is not a reason to hide the jobs.

    This is the one path that keeps working, and deliberately -- refusing here
    would leave an operator unable to even see what is scheduled.
    """
    _as_session("dashboard:owner")
    name = f"readable-{uuid.uuid4().hex[:8]}"
    _add_job(name)
    set_current_caller(None)

    assert name in _call_tool_inner("cron_list", {})


def test_the_cli_keeps_its_admin_bypass(monkeypatch) -> None:
    """The refusal never strands an operator; it names this route."""
    _as_session("dashboard:owner")
    job_id = _add_job(f"cli-{uuid.uuid4().hex[:8]}")
    set_current_caller(None)
    monkeypatch.setenv("KIROCREW_CLI", "1")

    assert "cannot determine which session is calling" not in _call_tool_inner(
        "cron_pause", {"job_id": job_id}
    )


# --- 4b: the gate the fix makes reachable at all --------------------------


def test_one_session_cannot_see_or_touch_another_sessions_job() -> None:
    """The separation the ownership gate exists for, now that identity arrives.

    Before the fix both sessions resolved an empty key, so ``cron_list`` skipped
    its filter and the per-job gate returned "allow" -- this assertion could not
    have held for any pair of callers.
    """
    _as_session("dashboard:alice")
    name = f"alice-{uuid.uuid4().hex[:8]}"
    job_id = _add_job(name)

    _as_session("dashboard:bob")
    assert name not in _call_tool_inner("cron_list", {})
    # Anti-enumeration: Bob is told it does not exist, not that it is not his.
    assert "job not found" in _call_tool_inner("cron_pause", {"job_id": job_id}).lower()


def test_the_channel_default_also_comes_from_the_caller_block() -> None:
    """``KIROCREW_CHANNEL_ID`` had the same defect as the session key.

    Process environment can only name one session's channel, and gatewayd forwards
    none to a shared backend -- so a pooled ``cron_add`` defaulted the delivery
    channel to nothing. The block carries ``channelId`` in the same envelope.
    """
    _as_session("slack:thread", channel="C0FROMBLOCK")
    job_id = _add_job(f"chan-{uuid.uuid4().hex[:8]}")

    job = CronService(base_dir=mcp_cron.config_dir()).get_job(job_id)
    assert job is not None and job.channel == "C0FROMBLOCK"


# --- 5: the rows written before the fix -----------------------------------


def test_a_job_created_now_always_records_an_owner() -> None:
    """Why the grandfather clause can drain rather than live forever."""
    _as_session("dashboard:alice")
    job_id = _add_job(f"owned-{uuid.uuid4().hex[:8]}")

    job = CronService(base_dir=mcp_cron.config_dir()).get_job(job_id)
    assert job is not None and job.session_key == "dashboard:alice"


def test_an_ownerless_legacy_row_stays_visible_and_manageable() -> None:
    """Rows a pooled backend wrote before it could name the caller.

    Their owner is not unknown-to-us so much as never-recorded, and there is no
    honest way to guess which session should inherit one. Stranding them behind
    the CLI would silently break every cron a user created from chat, so they keep
    the access they have today -- and ``cron_add`` refusing an unidentified caller
    means no new one can appear.
    """
    svc = CronService(base_dir=mcp_cron.config_dir())
    name = f"legacy-{uuid.uuid4().hex[:8]}"
    legacy = svc.add_job(name=name, message="go", every_secs=120, session_key="")

    _as_session("dashboard:alice")
    assert name in _call_tool_inner("cron_list", {})
    assert "Paused job" in _call_tool_inner("cron_pause", {"job_id": legacy.id})
