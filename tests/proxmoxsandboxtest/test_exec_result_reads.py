"""exec reads stdout, stderr and the exit code concurrently, reporting in that order."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from inspect_ai.util import OutputLimitExceededError

from proxmoxsandbox._impl.qga_responses import ExecStatus, GuestAgentTamperError
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import VmConfig, VmSourceConfig


def _env(read_file) -> ProxmoxSandboxEnvironment:
    agent = MagicMock()
    agent.write_file = AsyncMock()
    agent.exec_command = AsyncMock(return_value=7)
    agent.get_agent_exec_status = AsyncMock(return_value=ExecStatus(exited=1))
    agent.read_file_capped_or_blank = read_file
    return ProxmoxSandboxEnvironment(
        infra_commands=MagicMock(),
        agent_commands=agent,
        ipam_mappings=(),
        vm_id=100,
        name="vm",
        all_vms={
            100: VmConfig(
                vm_source_config=VmSourceConfig(built_in="ubuntu24.04"), name="vm"
            )
        },
        sdn_zone_id=None,
        instance=None,
        pool_id=None,
        os_type="l26",
    )


def _stream(filepath: str) -> str:
    return filepath.rsplit(".", 1)[-1]


async def test_the_three_reads_overlap():
    in_flight = 0
    peak = 0

    async def read_file(vm_id, filepath, count):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return (b"0" if _stream(filepath) == "returncode" else b"out", False)

    result = await _env(read_file).exec(["true"], timeout=5)

    assert peak == 3
    assert (result.returncode, result.stdout, result.stderr) == (0, "out", "out")


async def test_a_failed_stdout_read_is_raised_without_waiting_for_the_others():
    cancelled: list[str] = []

    async def read_file(vm_id, filepath, count):
        if _stream(filepath) == "stdout":
            raise GuestAgentTamperError(vm_id, "file-read", "bad")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(_stream(filepath))
            raise

    with pytest.raises(GuestAgentTamperError):
        await asyncio.wait_for(_env(read_file).exec(["true"], timeout=5), timeout=2)

    assert sorted(cancelled) == ["returncode", "stderr"]


async def test_a_stdout_failure_is_reported_ahead_of_an_earlier_stderr_failure():
    async def read_file(vm_id, filepath, count):
        if _stream(filepath) == "stderr":
            raise GuestAgentTamperError(vm_id, "file-read", "bad")
        if _stream(filepath) == "stdout":
            await asyncio.sleep(0.05)
            return (b"x" * count, True)
        return (b"0", False)

    with pytest.raises(OutputLimitExceededError):
        await _env(read_file).exec(["true"], timeout=5)
