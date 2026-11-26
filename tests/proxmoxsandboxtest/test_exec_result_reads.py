"""exec's result reads: a failed read fails exec at once, and stdout's error wins."""

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


async def test_a_failed_stdout_read_is_raised_without_waiting_for_the_others():
    async def read_file(vm_id, filepath, count):
        if _stream(filepath) == "stdout":
            raise GuestAgentTamperError(vm_id, "file-read", "bad")
        await asyncio.sleep(3600)

    with pytest.raises(GuestAgentTamperError):
        await asyncio.wait_for(_env(read_file).exec(["true"], timeout=5), timeout=2)


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
