"""Windows provider-CLI trust policy.

Two layers are tested separately, on purpose:

* the **policy** in ``github_runner.check_provider_path_component_windows``,
  driven by synthetic :class:`ComponentSecurity` values so it runs on every
  runner including the Ubuntu one that gates CI, and
* the **ACL read** in ``windows_acl.describe``, which needs a real security
  descriptor and is therefore Windows-only.

The fixtures are synthetic rather than "whatever `gh` install the runner
happens to have", so the suite does not silently change meaning with host
state.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from kiro_crew import github_runner as runner
from kiro_crew import platform_compat, windows_acl

ME = "S-1-5-21-1-2-3-1001"
SYSTEM = "S-1-5-18"
ADMINS = "S-1-5-32-544"
EVERYONE = "S-1-1-0"
AUTHENTICATED_USERS = "S-1-5-11"

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs a Windows ACL")


def _security(
    *,
    owner: str = SYSTEM,
    writers: tuple[windows_acl.Writer, ...] = (),
    null_dacl: bool = False,
    unparsable: tuple[int, ...] = (),
) -> windows_acl.ComponentSecurity:
    return windows_acl.ComponentSecurity(
        owner_sid=owner,
        owner_name="test-principal",
        null_dacl=null_dacl,
        writers=writers,
        unparsable_ace_types=unparsable,
    )


def _writer(sid: str, *rights: str) -> windows_acl.Writer:
    return windows_acl.Writer(sid=sid, name=f"name-of-{sid}", rights=rights or ("DELETE",))


@pytest.fixture
def described(monkeypatch):
    """Feed ``check_provider_path_component_windows`` a synthetic descriptor."""

    def _install(security: windows_acl.ComponentSecurity):
        monkeypatch.setattr(windows_acl, "describe", lambda path: security)

    return _install


# ── the substitution-right table ─────────────────────────────────────────────
#
# The regression that motivates this module: C:\ grants Authenticated Users the
# ADD_SUBDIRECTORY right by default, so reading bit 0x4 as a write grant on a
# directory refuses every stock Windows install once the walk reaches the root.


class TestSubstitutionRights:
    def test_add_subdirectory_is_not_a_substitution_right_on_a_directory(self) -> None:
        assert windows_acl._substitution_rights(0x00000004, is_dir=True) == ()

    def test_add_file_is_not_a_substitution_right_on_a_directory(self) -> None:
        assert windows_acl._substitution_rights(0x00000002, is_dir=True) == ()

    def test_the_same_bits_are_substitution_rights_on_a_file(self) -> None:
        assert windows_acl._substitution_rights(0x00000002, is_dir=False) == ("WRITE_DATA",)
        assert windows_acl._substitution_rights(0x00000004, is_dir=False) == ("APPEND_DATA",)

    def test_delete_child_is_a_substitution_right_on_a_directory(self) -> None:
        assert windows_acl._substitution_rights(0x00000040, is_dir=True) == ("FILE_DELETE_CHILD",)

    def test_taking_the_dacl_or_the_owner_counts_on_both(self) -> None:
        for is_dir in (True, False):
            assert "WRITE_DAC" in windows_acl._substitution_rights(0x00040000, is_dir=is_dir)
            assert "WRITE_OWNER" in windows_acl._substitution_rights(0x00080000, is_dir=is_dir)

    def test_cosmetic_write_rights_are_not_substitution_rights(self) -> None:
        """WRITE_EA / WRITE_ATTRIBUTES cannot change what the binary does."""
        assert windows_acl._substitution_rights(0x00000010, is_dir=False) == ()
        assert windows_acl._substitution_rights(0x00000100, is_dir=False) == ()


# ── the policy ───────────────────────────────────────────────────────────────


class TestRelaxedPolicy:
    def test_a_system_owned_component_with_only_trusted_writers_passes(self, described) -> None:
        described(_security(owner=SYSTEM, writers=(_writer(SYSTEM), _writer(ADMINS))))
        runner.check_provider_path_component_windows(
            Path("C:/anything"), label="executable", me_sid=ME, strict=False
        )

    def test_a_component_the_gateway_user_owns_passes(self, described) -> None:
        """The Windows analog of accepting a stock ``brew install gh``."""
        described(_security(owner=ME, writers=(_writer(ME),)))
        runner.check_provider_path_component_windows(
            Path("C:/anything"), label="executable", me_sid=ME, strict=False
        )

    def test_authenticated_users_holding_only_add_subdir_passes(self, described) -> None:
        """The stock C:\\ ACL must not refuse the walk.

        ``describe`` filters that ACE out because ADD_SUBDIRECTORY is not a
        substitution right, so the policy sees no writer at all.
        """
        described(_security(owner=SYSTEM, writers=(_writer(SYSTEM),)))
        runner.check_provider_path_component_windows(
            Path("C:/"), label="executable parent", me_sid=ME, strict=False
        )

    def test_a_third_account_owning_it_is_refused(self, described) -> None:
        described(_security(owner="S-1-5-21-9-9-9-1002"))
        with pytest.raises(ValueError, match="owned by another account"):
            runner.check_provider_path_component_windows(
                Path("C:/anything"), label="executable", me_sid=ME, strict=False
            )

    def test_an_untrusted_writer_is_refused_and_named(self, described) -> None:
        described(_security(owner=SYSTEM, writers=(_writer(EVERYONE, "WRITE_DATA", "DELETE"),)))
        with pytest.raises(ValueError, match="can be replaced by") as excinfo:
            runner.check_provider_path_component_windows(
                Path("C:/anything"), label="executable", me_sid=ME, strict=False
            )
        # The message must identify the offender: an operator cannot fix
        # "permission denied".
        assert EVERYONE in str(excinfo.value)
        assert "WRITE_DATA" in str(excinfo.value)

    def test_authenticated_users_holding_delete_child_is_still_refused(self, described) -> None:
        """The C:\\ carve-out is about which RIGHT, never about which principal."""
        described(
            _security(owner=SYSTEM, writers=(_writer(AUTHENTICATED_USERS, "FILE_DELETE_CHILD"),))
        )
        with pytest.raises(ValueError, match="can be replaced by"):
            runner.check_provider_path_component_windows(
                Path("C:/"), label="executable parent", me_sid=ME, strict=False
            )


class TestStrictPolicy:
    def test_a_user_owned_component_is_refused(self, described) -> None:
        """Strict mode is the "root-owned" analog: the machine must own it."""
        described(_security(owner=ME))
        with pytest.raises(ValueError, match="not owned by the system"):
            runner.check_provider_path_component_windows(
                Path("C:/anything"), label="executable", me_sid=ME, strict=True
            )

    def test_the_gateway_user_holding_a_write_right_is_refused(self, described) -> None:
        described(_security(owner=SYSTEM, writers=(_writer(ME, "WRITE_DATA"),)))
        with pytest.raises(ValueError, match="can be replaced by"):
            runner.check_provider_path_component_windows(
                Path("C:/anything"), label="executable", me_sid=ME, strict=True
            )

    def test_a_system_owned_component_passes(self, described) -> None:
        described(_security(owner=SYSTEM, writers=(_writer(SYSTEM), _writer(ADMINS))))
        runner.check_provider_path_component_windows(
            Path("C:/anything"), label="executable", me_sid=ME, strict=True
        )


class TestFailClosed:
    def test_a_null_dacl_is_refused(self, described) -> None:
        """A NULL DACL grants everyone full control, and reports no writers."""
        described(_security(owner=SYSTEM, null_dacl=True))
        with pytest.raises(ValueError, match="NULL DACL"):
            runner.check_provider_path_component_windows(
                Path("C:/anything"), label="executable", me_sid=ME, strict=False
            )

    def test_an_unparsable_ace_type_is_refused(self, described) -> None:
        """Object/callback ACEs place the SID elsewhere, so an unrecognised type
        means the writers tuple is incomplete -- refuse rather than trust it."""
        described(_security(owner=SYSTEM, unparsable=(9,)))
        with pytest.raises(ValueError, match="cannot evaluate"):
            runner.check_provider_path_component_windows(
                Path("C:/anything"), label="executable", me_sid=ME, strict=False
            )

    def test_an_unreadable_descriptor_is_refused(self, monkeypatch) -> None:
        def _boom(path):
            raise windows_acl.AclUnavailable("access denied")

        monkeypatch.setattr(windows_acl, "describe", _boom)
        with pytest.raises(ValueError, match="security descriptor is unreadable"):
            runner.check_provider_path_component_windows(
                Path("C:/anything"), label="executable", me_sid=ME, strict=False
            )


# ── candidate discovery ──────────────────────────────────────────────────────


class TestWellknownWindowsDirs:
    def test_posix_has_none(self, monkeypatch) -> None:
        monkeypatch.setattr(runner.sys, "platform", "linux")
        assert runner._wellknown_windows_dirs("gh") == ()

    def test_windows_names_the_program_files_subdirs(self, monkeypatch) -> None:
        """Built with ``os.path.join``, not literal separators.

        These tests run on the Ubuntu CI runner too, where `sys.platform` is
        monkeypatched but `os.path.join` is still posixpath and joins with `/`.
        A literal backslash in the expectation would assert the runner's path
        flavour rather than the function's behaviour.
        """
        monkeypatch.setattr(runner.sys, "platform", "win32")
        monkeypatch.setenv("ProgramFiles", r"C:\PF")
        monkeypatch.delenv("ProgramW6432", raising=False)
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        root = r"C:\PF"
        assert runner._wellknown_windows_dirs("gh") == (
            os.path.join(root, "GitHub CLI"),
            os.path.join(root, "GitHub CLI", "bin"),
        )

    def test_an_unset_root_is_skipped_rather_than_joined_as_empty(self, monkeypatch) -> None:
        monkeypatch.setattr(runner.sys, "platform", "win32")
        monkeypatch.delenv("ProgramFiles", raising=False)
        monkeypatch.delenv("ProgramW6432", raising=False)
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        assert runner._wellknown_windows_dirs("gh") == ()


class TestCandidateResolutionUsesTheStdlib:
    """Resolution inside a directory is ``shutil.which``'s job.

    The first draft of this change hand-rolled it and joined the bare name, so
    the scan looked for ``gh`` and never matched ``gh.exe`` -- on Windows it
    found nothing at all, before the trust policy was even consulted. It also
    split ``PATHEXT`` on ``os.pathsep``, which is ``:`` off Windows. Delegating
    removes both mistakes, so the test that matters is that we delegate.
    """

    @pytest.fixture(autouse=True)
    def _only_path_is_searched(self, monkeypatch):
        """Neutralise the well-known dirs so only the fixture's own dir is seen.

        Without this the Windows runner finds its REAL
        ``C:\\Program Files\\GitHub CLI\\gh.EXE`` and the assertions describe the
        host instead of the code.
        """
        monkeypatch.setattr(runner, "PROVIDER_EXECUTABLE_CANDIDATES", {"gh": (), "glab": ()})
        for variable in runner.WINDOWS_PROGRAM_ROOT_VARS:
            monkeypatch.delenv(variable, raising=False)

    def _stub(self, directory: Path) -> Path:
        target = directory / ("gh.exe" if sys.platform == "win32" else "gh")
        target.write_text("stub")
        if sys.platform != "win32":
            target.chmod(0o755)
        return target

    @staticmethod
    def _same(found: tuple[str, ...], expected: Path) -> bool:
        """Compare with the platform's own path semantics.

        On Windows ``shutil.which`` returns the name cased as ``PATHEXT`` spells
        it (``gh.EXE``), not as the file is spelled on disk (``gh.exe``). Both
        name the same file, because the filesystem is case-insensitive -- and
        that is precisely why ``validate_provider_executable`` compares
        casefolded before deciding a path is non-canonical.
        """
        if sys.platform == "win32":
            return tuple(p.casefold() for p in found) == (str(expected).casefold(),)
        return found == (str(expected),)

    def test_a_path_entry_hit_is_returned_absolute(self, monkeypatch, tmp_path: Path) -> None:
        target = self._stub(tmp_path)
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv(runner.STRICT_PROVIDER_BIN_ENV, raising=False)

        assert self._same(runner.provider_executable_candidates("gh"), target)

    def test_strict_mode_ignores_path(self, monkeypatch, tmp_path: Path) -> None:
        self._stub(tmp_path)
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setenv(runner.STRICT_PROVIDER_BIN_ENV, "1")

        assert runner.provider_executable_candidates("gh") == ()

    @windows_only
    def test_a_bare_name_resolves_to_the_dot_exe_on_windows(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """The regression the replaced code caused: PATHEXT must be applied."""
        target = tmp_path / "gh.exe"
        target.write_text("stub")
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.delenv(runner.STRICT_PROVIDER_BIN_ENV, raising=False)

        found = runner.provider_executable_candidates("gh")
        assert len(found) == 1
        assert found[0].casefold().endswith("gh.exe")


# ── the real ACL read ────────────────────────────────────────────────────────


@windows_only
class TestDescribeAgainstRealAcls:
    def test_a_user_owned_tree_reports_the_user_as_owner(self, tmp_path: Path) -> None:
        binary = tmp_path / "gh.exe"
        binary.write_text("stub")
        security = windows_acl.describe(binary)
        assert security.owner_sid == platform_compat.current_user_sid()
        assert not security.null_dacl
        assert security.unparsable_ace_types == ()

    def test_granting_everyone_full_control_surfaces_everyone_as_a_writer(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "evil"
        target.mkdir()
        binary = target / "gh.exe"
        binary.write_text("stub")
        subprocess.run(
            ["icacls", str(target), "/grant", "Everyone:(OI)(CI)F"],
            check=True,
            capture_output=True,
        )
        sids = {writer.sid for writer in windows_acl.describe(binary).writers}
        assert EVERYONE in sids

        # and the policy must refuse it
        with pytest.raises(ValueError, match="can be replaced by"):
            runner.check_provider_path_component_windows(
                binary,
                label="executable",
                me_sid=platform_compat.current_user_sid(),
                strict=False,
            )

    def test_the_drive_root_does_not_refuse_the_walk(self) -> None:
        """Regression: the stock C:\\ ACL grants Authenticated Users
        ADD_SUBDIRECTORY, which is not a substitution right."""
        root = Path(os.environ.get("SystemDrive", "C:") + os.sep)
        runner.check_provider_path_component_windows(
            root,
            label="executable parent",
            me_sid=platform_compat.current_user_sid(),
            strict=False,
        )

    def test_the_current_user_sid_is_a_well_formed_sid(self) -> None:
        assert platform_compat.current_user_sid().startswith("S-1-")

    def test_elevation_is_reported_as_a_bool(self) -> None:
        assert isinstance(windows_acl.is_token_elevated(), bool)


class TestLoadRefusesOffWindows:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX behaviour")
    def test_describe_refuses_rather_than_returning_a_permissive_answer(self) -> None:
        with pytest.raises(windows_acl.AclUnavailable):
            windows_acl.describe(Path("/etc/hosts"))
