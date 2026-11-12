"""exec() raises GuestExecNotStartedError only when the launch was never requested."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from inspect_ai.util import SandboxUnavailableError

from proxmoxsandbox._impl.agent_commands import GuestExecNotStartedError
from proxmoxsandbox._impl.qga_responses import ExecStatus, GuestAgentTamperError
from proxmoxsandbox._proxmox_sandbox_environment import ProxmoxSandboxEnvironment
from proxmoxsandbox.schema import VmConfig, VmSourceConfig

from .proxmox_sandbox_utils import http_500


def _agent() -> MagicMock:
    agent = MagicMock()
    agent.write_file = AsyncMock()
    agent.exec_command = AsyncMock(return_value=7)
    agent.get_agent_exec_status = AsyncMock(return_value=ExecStatus(exited=1))
    agent.read_file_capped_or_blank = AsyncMock(return_value=(b"0", False))
    return agent


def _env(agent: MagicMock) -> ProxmoxSandboxEnvironment:
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


@pytest.mark.parametrize(
    "error",
    [http_500("VM 100 is not running"), httpx.ConnectError("All connection attempts")],
    ids=["transient-500", "transport"],
)
async def test_script_write_that_exhausts_its_retries_means_not_started(error):
    agent = _agent()
    agent.write_file.side_effect = error

    with pytest.raises(GuestExecNotStartedError) as raised:
        await _env(agent).exec(["true"], timeout=5)

    assert isinstance(raised.value, SandboxUnavailableError)
    assert raised.value.__cause__ is error
    # Inspect passes this text to the model
    assert str(raised.value) == (
        "The sandbox is not running or cannot be reached, "
        "so the command was not launched."
    )
    agent.exec_command.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        http_500("failed to open file '/tmp/x.sh'"),
        GuestAgentTamperError(100, "file-write", "bad"),
    ],
    ids=["non-transient-500", "tamper"],
)
async def test_script_write_the_agent_rejects_propagates_unchanged(error):
    agent = _agent()
    agent.write_file.side_effect = error

    with pytest.raises(type(error)) as raised:
        await _env(agent).exec(["true"], timeout=5)

    assert raised.value is error


@pytest.mark.parametrize("stage", ["exec_command", "get_agent_exec_status"])
async def test_failure_once_the_launch_is_requested_keeps_its_error(stage):
    """A failed launch request can still have started the command in the guest."""
    error = http_500("guest-exec failed - got timeout")
    agent = _agent()
    getattr(agent, stage).side_effect = error

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await _env(agent).exec(["true"], timeout=5)

    assert raised.value is error
