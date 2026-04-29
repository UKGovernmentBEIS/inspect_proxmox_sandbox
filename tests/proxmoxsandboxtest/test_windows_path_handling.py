"""Tests for Windows path handling in ProxmoxSandboxEnvironment.

These are unit tests that verify path construction logic without
requiring a real Proxmox instance or Windows VM.
"""

from pathlib import PureWindowsPath
from unittest.mock import AsyncMock, MagicMock

import pytest

from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment


def _make_sandbox(os_type=None) -> ProxmoxSandboxEnvironment:
    """Create a minimal ProxmoxSandboxEnvironment for unit testing."""
    return ProxmoxSandboxEnvironment(
        infra_commands=MagicMock(),
        agent_commands=MagicMock(),
        ipam_mappings=(),
        vm_id=100,
        all_vm_ids=(100,),
        sdn_zone_id=None,
        instance=None,
        pool_id=None,
        os_type=os_type,
    )


class TestIsWindows:
    def test_linux_os_type(self):
        env = _make_sandbox(os_type="l26")
        assert not env._is_windows()

    def test_none_os_type(self):
        env = _make_sandbox(os_type=None)
        assert not env._is_windows()

    def test_win11_os_type(self):
        env = _make_sandbox(os_type="win11")
        assert env._is_windows()

    def test_win10_os_type(self):
        env = _make_sandbox(os_type="win10")
        assert env._is_windows()

    def test_w2k8_os_type(self):
        env = _make_sandbox(os_type="w2k8")
        assert env._is_windows()

    def test_solaris_not_windows(self):
        env = _make_sandbox(os_type="solaris")
        assert not env._is_windows()


class TestWriteFileWindowsPaths:
    """Verify that write_file uses PureWindowsPath (not Path) for Windows VMs.

    On a Linux host, pathlib.Path treats backslashes as literal characters,
    so Path("C:\\Users\\test\\file.txt").parent returns "." instead of
    "C:\\Users\\test". The code must use PureWindowsPath for Windows paths.
    """

    @pytest.mark.asyncio
    async def test_write_file_small_creates_correct_parent_dir(self):
        """write_file should mkdir the correct Windows parent directory."""
        env = _make_sandbox(os_type="win11")

        # Track what commands exec() is called with
        exec_calls = []

        async def fake_exec(cmd, **kwargs):
            exec_calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")

        env.exec = fake_exec
        env._write_file_only = AsyncMock()

        windows_path = "C:\\Users\\agent\\Desktop\\output.txt"
        await env.write_file(windows_path, b"hello")

        # The first exec call should be the mkdir for the parent dir
        assert len(exec_calls) >= 1
        mkdir_cmd = exec_calls[0]

        expected_parent = str(PureWindowsPath(windows_path).parent)
        # expected_parent == "C:\\Users\\agent\\Desktop"

        # The mkdir command must contain the correct parent directory
        mkdir_str = " ".join(mkdir_cmd)
        assert expected_parent in mkdir_str, (
            f"Expected parent dir '{expected_parent}' in mkdir command, "
            f"got: {mkdir_cmd}"
        )

        # Specifically, it must NOT contain just "." which is what
        # pathlib.Path produces for Windows paths on Linux
        assert '"."' not in mkdir_str, (
            "mkdir command contains '.' — Path() was used instead of "
            "PureWindowsPath() for a Windows path"
        )


class TestSampleCleanupWarningMessage:
    """Verify the warning message in sample_cleanup is correctly formatted."""

    def test_warning_fstring_has_no_missing_separator(self):
        """Regression test: the f-string in sample_cleanup's warning
        must not concatenate pool_id and cleanup_succeeded without a
        newline between them.
        """
        # Grep the actual source to verify the f-string lines are
        # separated by a newline, rather than duplicating the format here.
        import inspect
        source = inspect.getsource(ProxmoxSandboxEnvironment.sample_cleanup)

        # The buggy pattern was two adjacent f-string lines:
        #   f"pool_id={pool_id}"
        #   f"cleanup_succeeded={cleanup_succeeded}"
        # which Python concatenates into "pool_id=Xcleanup_succeeded=Y".
        # The fix adds \n at the end of the pool_id line.
        assert 'pool_id}\\n"' in source or "pool_id}\n" in source, (
            "pool_id line in sample_cleanup warning is missing a trailing "
            "newline — it will run into cleanup_succeeded with no separator"
        )
